"""Raw file input: :class:`RawFrame` (everything the HEAD/DNG needs) and loaders.

* :func:`load_raw` reads a camera raw via rawpy/LibRaw plus one exiftool call.
* :func:`save_frame_npz` / :func:`load_frame_npz` persist a frame as ``<stem>.npz``
  (arrays) + ``<stem>.json`` (metadata) so tests can use small crops.
* :func:`crop_frame` cuts a consistent sub-frame (even-aligned origin).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import cfa
from .tools import ExifTool, exiftool_json

FRAME_JSON_VERSION = 1

EXIF_TAGS: tuple[str, ...] = (
    "ISO",
    "Make",
    "Model",
    "RawDataOffset",
    "Orientation",
    "ExposureTime",
    "FNumber",
    "FocalLength",
    "DateTimeOriginal",
    "LensModel",
)
"""Tags fetched by :func:`load_raw` with ``exiftool -j -n``."""


class RawReadError(ValueError):
    """The input is missing, not a supported camera raw file, or truncated/corrupt."""


@dataclass
class RawFrame:
    """A decoded raw mosaic plus all metadata needed for HEAD and DNG reconstruction.

    Array fields: ``mosaic`` (HxW uint16, full sensor incl. margins), ``pattern`` (2x2 int64
    colour indices), ``xyz_to_cam`` (3x3 float64).  Per-channel lists are indexed by LibRaw
    colour index; per-position lists by position order (0,0),(0,1),(1,0),(1,1).
    """

    mosaic: np.ndarray
    pattern: np.ndarray
    color_desc: str
    black_per_channel: list[int]
    black_per_position: list[int]
    white: int
    camera_wb: list[float]
    """Effective as-shot WB (4 values, colour-index order); see ``wb_source``."""
    daylight_wb: list[float]
    xyz_to_cam: np.ndarray
    """``rgb_xyz_matrix[:3]`` (cam_from_XYZ, dcraw convention), float64 3x3."""
    margins: tuple[int, int] = (0, 0)
    """(top_margin, left_margin) of the visible area within the full sensor."""
    visible_hw: tuple[int, int] = (0, 0)
    """(height, width) of LibRaw's visible area (``sizes.height/width``)."""
    crop_ltwh: tuple[int, int, int, int] = (0, 0, 0, 0)
    """Default crop (left, top, width, height) in full-sensor coordinates."""
    flip: int = 0
    wb_source: str = "camera"
    """'camera' | 'daylight' | 'unity' -- where ``camera_wb`` came from."""
    camera_wb_raw: list[float] = field(default_factory=list)
    """``camera_whitebalance`` exactly as reported by LibRaw (may be invalid)."""
    raw_type: str = "Flat"
    num_colors: int = 3
    source_name: str = ""
    source_path: str | None = None
    source_size: int = 0
    source_sha256: str | None = None
    """Filled lazily by :meth:`compute_source_sha256`."""
    make: str | None = None
    model: str | None = None
    iso: float | None = None
    raw_data_offset: int | None = None
    libraw_version: str = ""
    exif: dict[str, Any] = field(default_factory=dict)
    crop_origin: tuple[int, int] = (0, 0)
    """(top, left) of this frame within the source sensor (non-zero after :func:`crop_frame`)."""

    # -- derived ---------------------------------------------------------------------
    @property
    def height(self) -> int:
        return int(self.mosaic.shape[0])

    @property
    def width(self) -> int:
        return int(self.mosaic.shape[1])

    @property
    def is_cropped(self) -> bool:
        return tuple(self.crop_origin) != (0, 0)

    @property
    def bits(self) -> int:
        """Sample bit depth for HEAD/DNG: ``max(12, bit_length(max(white, max(mosaic))))``."""
        mx = max(int(self.white), int(self.mosaic.max()) if self.mosaic.size else 0)
        return max(12, mx.bit_length())

    @property
    def is_bayer(self) -> bool:
        """True for an RGGB-like 2x2 Bayer CFA (required by half3/gat4)."""
        return self.raw_type == "Flat" and cfa.is_bayer_rggb_like(self.pattern, self.color_desc)

    def color_roles(self) -> dict[str, int]:
        """``{'R','G1','G2','B'} -> position index``; raises ValueError for non-Bayer."""
        return cfa.color_roles(self.pattern, self.color_desc)

    def dng_cfa(self) -> list[int]:
        return cfa.dng_cfa_pattern(self.pattern, self.color_desc)

    def mosaic_sha256(self) -> str:
        """sha256 hex of ``mosaic.tobytes()`` (C-order, native little-endian uint16)."""
        return hashlib.sha256(np.ascontiguousarray(self.mosaic).tobytes()).hexdigest()

    def compute_source_sha256(self) -> str | None:
        """Hash the source file (if ``source_path`` exists), store and return it."""
        if self.source_sha256 is None and self.source_path and os.path.exists(self.source_path):
            h = hashlib.sha256()
            with open(self.source_path, "rb") as f:
                for blk in iter(lambda: f.read(1 << 22), b""):
                    h.update(blk)
            self.source_sha256 = h.hexdigest()
        return self.source_sha256

    def head_sections(self) -> dict[str, Any]:
        """HEAD JSON sections derivable from the frame alone (DESIGN.md 3.3).

        Returns ``source``, ``mosaic`` (without ``recon_sha256``; height/width/orig_* are the
        frame's own size -- the encoder overrides height/width after even padding),
        ``cfa``, ``levels``, ``color``, ``geometry``.  JSON-serialisable.
        """
        dng_cfa: list[int] | None
        try:
            dng_cfa = self.dng_cfa()
        except ValueError:
            dng_cfa = None
        return {
            "source": {
                "name": self.source_name,
                "size": int(self.source_size),
                "sha256": self.source_sha256,
                "make": self.make,
                "model": self.model,
                "raw_data_offset": self.raw_data_offset,
                "libraw_version": self.libraw_version,
                "crop_origin": list(self.crop_origin),
            },
            "mosaic": {
                "height": self.height,
                "width": self.width,
                "orig_height": self.height,
                "orig_width": self.width,
                "dtype": "uint16",
                "bits": self.bits,
                "sha256": self.mosaic_sha256(),
            },
            "cfa": {
                "pattern": np.asarray(self.pattern).astype(int).tolist(),
                "color_desc": self.color_desc,
                "dng_cfa": dng_cfa,
            },
            "levels": {
                "black_per_position": [int(v) for v in self.black_per_position],
                "black_per_channel": [int(v) for v in self.black_per_channel],
                "white": int(self.white),
            },
            "color": {
                "camera_wb": [float(v) for v in self.camera_wb],
                "wb_source": self.wb_source,
                "daylight_wb": [float(v) for v in self.daylight_wb],
                "xyz_to_cam": np.asarray(self.xyz_to_cam, dtype=np.float64).tolist(),
                "illuminant": 21,
            },
            "geometry": {
                "margins": [int(v) for v in self.margins],
                "visible_hw": [int(v) for v in self.visible_hw],
                "crop_ltwh": [int(v) for v in self.crop_ltwh],
                "flip": int(self.flip),
            },
        }


# ---------------------------------------------------------------------------------------
# helpers


def _valid_wb(wb: list[float]) -> bool:
    return len(wb) >= 3 and all(math.isfinite(v) and v > 0 for v in wb[:3])


def resolve_wb(camera_wb: list[float], daylight_wb: list[float]) -> tuple[list[float], str]:
    """Effective WB with fallback camera -> daylight -> unity; returns ``(wb4, source)``.

    A 4th value of 0 (LibRaw convention for "same as G") is replaced by the 2nd value.
    """

    def norm4(wb: list[float]) -> list[float]:
        w = [float(v) for v in wb] + [0.0] * (4 - len(wb))
        w = w[:4]
        if not (math.isfinite(w[3]) and w[3] > 0):
            w[3] = w[1]
        return w

    if _valid_wb(camera_wb):
        return norm4(camera_wb), "camera"
    if _valid_wb(daylight_wb):
        return norm4(daylight_wb), "daylight"
    return [1.0, 1.0, 1.0, 1.0], "unity"


def _libraw_version_str(v: object) -> str:
    if isinstance(v, tuple):
        return ".".join(str(x) for x in v)
    return str(v)


_RAW_MAGIC = (b"II*\x00", b"MM\x00*", b"IIU\x00", b"IIRO", b"IIRS", b"MMOR", b"FUJIFILM", b"\x00MRM", b"FOVb")


def _looks_like_raw(p: Path) -> bool:
    """Cheap header sniff: TIFF-based raws, RW2, ORF, RAF, MRW, X3F, ISO-BMFF (CR3)."""
    try:
        with open(p, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return head.startswith(_RAW_MAGIC) or head[4:8] == b"ftyp"


def _libraw_reason(exc: BaseException, p: Path) -> str:
    """Readable reason for a rawpy/LibRaw exception (its messages are bytes reprs otherwise)."""
    import rawpy

    msg = exc.args[0] if exc.args else ""
    if isinstance(msg, (bytes, bytearray)):
        msg = bytes(msg).decode("utf-8", errors="replace")
    kind = type(exc).__name__
    raw_like = _looks_like_raw(p)
    if isinstance(exc, rawpy.LibRawFileUnsupportedError):
        why = "unsupported camera raw format" if raw_like else "not a camera raw file"
    elif isinstance(exc, (rawpy.LibRawIOError, rawpy.LibRawDataError)):
        why = "file truncated or corrupt" if raw_like else "not a camera raw file"
    else:
        why = "LibRaw could not decode the file"
    return f"{why} ({kind}: {msg})" if msg else f"{why} ({kind})"


def load_raw(
    path: str | os.PathLike[str],
    *,
    want_exif: bool = True,
    want_source_sha256: bool = False,
    exiftool: ExifTool | None = None,
) -> RawFrame:
    """Read a camera raw file into a :class:`RawFrame`.

    Uses rawpy for pixels and colour data, and one ``exiftool -j -n`` call (via
    ``exiftool`` if given, otherwise a subprocess) for :data:`EXIF_TAGS`.  When exiftool
    is missing/fails or ``want_exif=False``: ``iso``/``make``/``model``/``raw_data_offset``
    are ``None`` and ``exif`` is ``{}``.
    """
    import rawpy

    p = Path(path)
    if not p.exists():
        raise RawReadError(f"{p}: file not found")
    if not p.is_file():
        raise RawReadError(f"{p}: not a regular file")
    try:
        with rawpy.imread(os.fspath(p)) as r:
            mosaic = np.array(r.raw_image, dtype=np.uint16, copy=True)
            raw_pattern = r.raw_pattern
            pattern = (
                np.array(raw_pattern, dtype=np.int64)
                if raw_pattern is not None
                else np.zeros((2, 2), dtype=np.int64)
            )
            desc_raw = r.color_desc
            color_desc = (
                desc_raw.decode("ascii", errors="replace")
                if isinstance(desc_raw, (bytes, bytearray))
                else str(desc_raw)
            )
            black_ch = [int(v) for v in r.black_level_per_channel]
            white = int(r.white_level)
            cam_wb_raw = [float(v) for v in r.camera_whitebalance]
            day_wb = [float(v) for v in r.daylight_whitebalance]
            xyz = np.array(r.rgb_xyz_matrix, dtype=np.float64)[:3].copy()
            s = r.sizes
            raw_type = str(getattr(r.raw_type, "name", r.raw_type))
            num_colors = int(r.num_colors)
    except rawpy.LibRawError as exc:
        raise RawReadError(f"{p}: {_libraw_reason(exc, p)}") from exc

    if pattern.shape == (2, 2):
        blk_pos = cfa.black_per_position(pattern, black_ch)
    else:  # non-2x2 CFA (e.g. X-Trans): keep something sane; lossy engines refuse it
        blk_pos = [black_ch[0]] * 4
    cam_wb, wb_src = resolve_wb(cam_wb_raw, day_wb)

    exif: dict[str, Any] = {}
    if want_exif:
        exif = exiftool_json(p, EXIF_TAGS, numeric=True, et=exiftool) or {}

    def _num(key: str) -> float | None:
        v = exif.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    iso = _num("ISO")
    rdo = exif.get("RawDataOffset")
    frame = RawFrame(
        mosaic=mosaic,
        pattern=pattern,
        color_desc=color_desc,
        black_per_channel=black_ch,
        black_per_position=blk_pos,
        white=white,
        camera_wb=cam_wb,
        daylight_wb=day_wb,
        xyz_to_cam=xyz,
        margins=(int(s.top_margin), int(s.left_margin)),
        visible_hw=(int(s.height), int(s.width)),
        crop_ltwh=(
            int(s.crop_left_margin),
            int(s.crop_top_margin),
            int(s.crop_width),
            int(s.crop_height),
        ),
        flip=int(s.flip),
        wb_source=wb_src,
        camera_wb_raw=cam_wb_raw,
        raw_type=raw_type,
        num_colors=num_colors,
        source_name=p.name,
        source_path=os.fspath(p.resolve()),
        source_size=p.stat().st_size,
        make=str(exif["Make"]) if "Make" in exif else None,
        model=str(exif["Model"]) if "Model" in exif else None,
        iso=int(iso) if iso is not None and iso.is_integer() else iso,
        raw_data_offset=int(rdo) if isinstance(rdo, (int, float)) else None,
        libraw_version=_libraw_version_str(rawpy.libraw_version),
        exif=exif,
    )
    # LibRaw crop of 0x0 means "no crop info": use the visible area.
    if frame.crop_ltwh[2] <= 0 or frame.crop_ltwh[3] <= 0:
        frame.crop_ltwh = (frame.margins[1], frame.margins[0], frame.visible_hw[1], frame.visible_hw[0])
    if want_source_sha256:
        frame.compute_source_sha256()
    return frame


# ---------------------------------------------------------------------------------------
# cropping


def crop_frame(frame: RawFrame, top: int, left: int, h: int, w: int) -> RawFrame:
    """Return a consistent sub-frame of ``h x w`` pixels starting at (top, left).

    ``top``/``left`` are rounded DOWN to even so the CFA phase (pattern) is unchanged.
    The window is clipped to the mosaic.  ``h``/``w`` may be odd.  Margins, visible area and
    default crop are intersected with the window and expressed in the new coordinates
    (an empty intersection makes the crop the whole window).  ``crop_origin`` accumulates.
    The mosaic is copied.  Source/EXIF metadata are kept (they describe the original file).
    """
    top = max(0, int(top)) & ~1
    left = max(0, int(left)) & ~1
    H, W = frame.height, frame.width
    if top >= H or left >= W:
        raise ValueError(f"crop origin ({top},{left}) outside mosaic {H}x{W}")
    bottom = min(H, top + int(h))
    right = min(W, left + int(w))
    nh, nw = bottom - top, right - left
    if nh <= 0 or nw <= 0:
        raise ValueError("empty crop")

    def intersect(y0: int, x0: int, hh: int, ww: int) -> tuple[int, int, int, int] | None:
        a0, b0 = max(y0, top), max(x0, left)
        a1, b1 = min(y0 + hh, bottom), min(x0 + ww, right)
        if a1 <= a0 or b1 <= b0:
            return None
        return a0 - top, b0 - left, a1 - a0, b1 - b0

    vis = intersect(frame.margins[0], frame.margins[1], frame.visible_hw[0], frame.visible_hw[1])
    if vis is None:
        vis = (0, 0, nh, nw)
    cl, ct, cw, ch = frame.crop_ltwh
    cr = intersect(ct, cl, ch, cw)
    if cr is None:
        cr = vis
    exif = dict(frame.exif)
    return dataclasses.replace(
        frame,
        mosaic=np.ascontiguousarray(frame.mosaic[top:bottom, left:right]).copy(),
        pattern=np.array(frame.pattern, copy=True),
        xyz_to_cam=np.array(frame.xyz_to_cam, copy=True),
        margins=(vis[0], vis[1]),
        visible_hw=(vis[2], vis[3]),
        crop_ltwh=(cr[1], cr[0], cr[3], cr[2]),
        crop_origin=(frame.crop_origin[0] + top, frame.crop_origin[1] + left),
        exif=exif,
        black_per_channel=list(frame.black_per_channel),
        black_per_position=list(frame.black_per_position),
        camera_wb=list(frame.camera_wb),
        daylight_wb=list(frame.daylight_wb),
        camera_wb_raw=list(frame.camera_wb_raw),
    )


# ---------------------------------------------------------------------------------------
# npz + json persistence

_ARRAY_FIELDS = ("mosaic", "pattern", "xyz_to_cam")


def _frame_paths(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    p = Path(path)
    stem = p.with_suffix("") if p.suffix in (".npz", ".json") else p
    return stem.with_suffix(".npz"), stem.with_suffix(".json")


def save_frame_npz(frame: RawFrame, path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Write ``<stem>.npz`` (mosaic, pattern, xyz_to_cam; zlib-compressed) and ``<stem>.json``.

    ``path`` may end in ``.npz``, ``.json`` or have no suffix.  Returns both paths.
    """
    npz_path, json_path = _frame_paths(path)
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        npz_path,
        mosaic=np.ascontiguousarray(frame.mosaic, dtype=np.uint16),
        pattern=np.asarray(frame.pattern, dtype=np.int64),
        xyz_to_cam=np.asarray(frame.xyz_to_cam, dtype=np.float64),
    )
    meta: dict[str, Any] = {"frame_json_version": FRAME_JSON_VERSION}
    for f in dataclasses.fields(frame):
        if f.name in _ARRAY_FIELDS:
            continue
        v = getattr(frame, f.name)
        meta[f.name] = list(v) if isinstance(v, tuple) else v
    meta["mosaic_sha256"] = frame.mosaic_sha256()
    json_path.write_text(json.dumps(meta, indent=1, sort_keys=True, default=_json_default) + "\n")
    return npz_path, json_path


def _json_default(o: object) -> object:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (bytes, bytearray)):
        return bytes(o).decode("latin-1")
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def load_frame_npz(path: str | os.PathLike[str], *, verify: bool = True) -> RawFrame:
    """Load a frame written by :func:`save_frame_npz` (``path``: stem, .npz or .json).

    With ``verify=True`` the stored mosaic sha256 is checked (ValueError on mismatch).
    """
    npz_path, json_path = _frame_paths(path)
    meta = json.loads(json_path.read_text())
    with np.load(npz_path) as z:
        arrays = {k: np.array(z[k]) for k in _ARRAY_FIELDS}
    names = {f.name for f in dataclasses.fields(RawFrame)}
    kwargs: dict[str, Any] = {k: v for k, v in meta.items() if k in names}
    for k in ("margins", "visible_hw", "crop_ltwh", "crop_origin"):
        if k in kwargs and kwargs[k] is not None:
            kwargs[k] = tuple(int(x) for x in kwargs[k])
    kwargs.update(arrays)
    kwargs["mosaic"] = kwargs["mosaic"].astype(np.uint16, copy=False)
    frame = RawFrame(**kwargs)
    if verify and "mosaic_sha256" in meta and meta["mosaic_sha256"] != frame.mosaic_sha256():
        raise ValueError(f"mosaic sha256 mismatch in {npz_path}")
    return frame
