"""DNG reconstruction (DESIGN.md 3.5) and the in-memory 16-bit DNG used by verify.

* :func:`build_tags` -- DNG IFD tags from a HEAD-like dict (or a :class:`RawFrame`).
* :class:`LJ92DNG` -- pidng ``RAW2DNG`` subclass: 256x256 LJ92 tiles encoded in a thread
  pool (each tile reshaped to ``(th/2, 2*tw)`` so the predictor's "above" neighbour has the
  same CFA colour), or uncompressed 12/16-bit strips.
* :func:`write_dng` -- atomic write (tmp + rename), optional EXIF transfer from META.
* :func:`dng_bytes16` -- uncompressed 16-bit DNG as bytes (deterministic verify pipeline).

pidng pitfalls handled here: we never let pidng add its own Software/DNGVersion (no
duplicates), always set tiles or strips explicitly, never use ``compress=True``, write
Rationals as ``[num, den]`` pairs and BlackLevel as SHORT, and never call ``convert`` with a
filename (it appends ``.dng``).  EXIF sub-IFDs are added afterwards by exiftool.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import imagecodecs
import numpy as np
from pidng.core import RAW2DNG
from pidng.defs import CalibrationIlluminant, Compression, DNGVersion, PhotometricInterpretation
from pidng.dng import DNG, DNGTags, Tag, dngIFD, dngTag

from . import __version__
from .tools import ExifTool, default_file_mode

FLIP_TO_ORIENTATION: dict[int, int] = {0: 1, 3: 3, 5: 8, 6: 6}
"""LibRaw ``sizes.flip`` -> TIFF/DNG Orientation."""

COMPRESSIONS = ("lj92", "none12", "none16")


# ---------------------------------------------------------------------------------------
# HEAD normalisation


@dataclass
class DngSpec:
    """Everything :func:`build_tags` needs, normalised from a HEAD-like dict."""

    width: int
    height: int
    bps: int
    cfa: list[int]
    """DNG CFAPattern codes in position order (0=R, 1=G, 2=B)."""
    black_per_position: list[int]
    white: int
    xyz_to_cam: list[list[float]]
    camera_wb: list[float]
    active_area: list[int]
    """[top, left, bottom, right]."""
    crop_origin: list[int]
    """DefaultCropOrigin [x, y] relative to the active area."""
    crop_size: list[int]
    """DefaultCropSize [w, h]."""
    orientation: int
    make: str
    model: str
    software: str
    noise_profile: list[float] | None = None


def _head_dict(frame_like: Any) -> Mapping[str, Any]:
    if isinstance(frame_like, Mapping):
        return frame_like
    hs = getattr(frame_like, "head_sections", None)
    if callable(hs):
        return hs()
    raise TypeError("frame_like must be a HEAD-like mapping or a RawFrame")


def software_string(head: Mapping[str, Any]) -> str:
    """``rawsqueeze <ver> (<engine> <param>)`` from HEAD (DESIGN.md 3.5, Q10)."""
    engine = head.get("engine")
    if not engine:
        return f"rawsqueeze {__version__}"
    codec = head.get("codec") or {}
    param = ""
    if head.get("mode") == "lossless":
        param = "lossless"
    elif engine == "nlq":
        f = (codec.get("nlq") or {}).get("f")
        param = "lossless" if f in (0, 0.0) else (f"f{f}" if f is not None else "")
    elif engine in ("half3", "gat4"):
        d = (codec.get(engine) or {}).get("d")
        param = f"d{d}" if d is not None else ""
    return f"rawsqueeze {__version__} ({engine} {param})".replace(" )", ")")


def noise_profile_from_head(head: Mapping[str, Any]) -> list[float] | None:
    """DNG NoiseProfile ``[S_R, O_R, S_G, O_G, S_B, O_B]`` from ``head["noise"]``.

    ``S_c = g_c / (wl - blk_c)``, ``O_c = s2_c / (wl - blk_c)**2`` with the per-position
    model averaged per colour (G = mean of G1, G2).  For nlq lossy, the quantisation
    variance ``f**2/12 * sigma**2`` is folded in (both terms scaled by ``1 + f**2/12``).
    Returns None if no noise model is present.
    """
    noise = head.get("noise") or {}
    planes = noise.get("planes")
    if not planes or len(planes) != 4:
        return None
    cfa = (head.get("cfa") or {}).get("dng_cfa")
    levels = head.get("levels") or {}
    blk = levels.get("black_per_position")
    wl = levels.get("white")
    if not cfa or blk is None or wl is None:
        return None
    factor = 1.0
    codec = head.get("codec") or {}
    if head.get("engine") == "nlq" and head.get("mode") != "lossless":
        f = float((codec.get("nlq") or {}).get("f") or 0.0)
        factor = 1.0 + f * f / 12.0
    out: list[float] = []
    for color in (0, 1, 2):
        S: list[float] = []
        O: list[float] = []
        for k in range(4):
            if int(cfa[k]) != color:
                continue
            p = planes[k]
            g = p.get("g_used", p.get("g"))
            s2 = p.get("s2")
            if g is None or s2 is None:
                return None
            X = float(wl) - float(blk[k])
            if X <= 0:
                return None
            S.append(float(g) / X)
            O.append(float(s2) / (X * X))
        if not S:
            return None
        out += [factor * float(np.mean(S)), factor * float(np.mean(O))]
    return out


def dng_spec_from_head(
    frame_like: Any,
    *,
    shape: tuple[int, int] | None = None,
    bps: int | None = None,
    mosaic_max: int | None = None,
    noise_profile: bool = True,
    software: str | None = None,
) -> DngSpec:
    """Normalise a HEAD-like dict (or RawFrame) into a :class:`DngSpec`.

    ``shape`` is the (H, W) of the mosaic actually written (default: HEAD
    ``mosaic.orig_height/orig_width``).  ``bps`` default: ``max(12, bit_length(max(white,
    mosaic_max)))``.  ActiveArea comes from ``geometry.margins`` + ``visible_hw`` (whole
    image if absent); DefaultCrop from ``geometry.crop_ltwh`` (full-sensor coordinates)
    converted to active-area coordinates and clamped.
    """
    head = _head_dict(frame_like)
    mos = head.get("mosaic") or {}
    if shape is None:
        H = int(mos.get("orig_height", mos.get("height")))
        W = int(mos.get("orig_width", mos.get("width")))
    else:
        H, W = int(shape[0]), int(shape[1])
    cfa_sec = head.get("cfa") or {}
    cfa = cfa_sec.get("dng_cfa")
    if not cfa:
        raise ValueError("HEAD has no dng_cfa (non-Bayer CFA cannot be written as DNG v1)")
    levels = head["levels"]
    blk = [int(v) for v in levels["black_per_position"]]
    white = int(levels["white"])
    if bps is None:
        mx = max(white, int(mosaic_max) if mosaic_max is not None else 0)
        bps = max(12, mx.bit_length())
    color = head.get("color") or {}
    xyz = np.asarray(color.get("xyz_to_cam", np.eye(3)), dtype=np.float64)[:3, :3]
    wb = [float(v) for v in (color.get("camera_wb") or [1.0, 1.0, 1.0, 1.0])]
    geo = head.get("geometry") or {}
    top, left = (int(v) for v in (geo.get("margins") or (0, 0)))
    vh, vw = (int(v) for v in (geo.get("visible_hw") or (0, 0)))
    if vh <= 0 or vw <= 0:
        vh, vw = H - top, W - left
    bottom, right = min(H, top + vh), min(W, left + vw)
    if not (0 <= top < bottom and 0 <= left < right):
        top, left, bottom, right = 0, 0, H, W
    aw, ah = right - left, bottom - top
    cl, ct, cw, ch = (int(v) for v in (geo.get("crop_ltwh") or (left, top, aw, ah)))
    ox, oy = cl - left, ct - top
    if cw <= 0 or ch <= 0 or ox < 0 or oy < 0 or ox + cw > aw or oy + ch > ah:
        ox, oy, cw, ch = 0, 0, aw, ah
    src = head.get("source") or {}
    make = str(src.get("make") or "Unknown")
    model = str(src.get("model") or "Unknown")
    np_list = noise_profile_from_head(head) if noise_profile else None
    return DngSpec(
        width=W,
        height=H,
        bps=int(bps),
        cfa=[int(v) for v in cfa],
        black_per_position=blk,
        white=white,
        xyz_to_cam=xyz.tolist(),
        camera_wb=wb,
        active_area=[top, left, bottom, right],
        crop_origin=[ox, oy],
        crop_size=[cw, ch],
        orientation=FLIP_TO_ORIENTATION.get(int(geo.get("flip", 0) or 0), 1),
        make=make,
        model=model,
        software=software or software_string(head),
        noise_profile=np_list,
    )


# ---------------------------------------------------------------------------------------
# tags


def srat(x: float, den: int = 10000) -> list[int]:
    """SRATIONAL ``[round(x*den), den]``."""
    return [int(round(x * den)), den]


def _ratio(num: float, den: float, limit: int = 1_000_000) -> list[int]:
    """Exact-ish unsigned rational num/den (exact for integer WB values)."""
    if den == 0 or num <= 0:
        return [1, 1]
    fr = Fraction(num) / Fraction(den)
    if fr.numerator > 0xFFFFFFFF or fr.denominator > 0xFFFFFFFF:
        fr = fr.limit_denominator(limit)
    return [fr.numerator, fr.denominator]


def _ascii(s: str) -> str:
    return s.encode("ascii", errors="replace").decode("ascii")


def xmp_packet(software: str, engine: str | None, param: str | None) -> bytes:
    """Minimal XMP packet with ``rawsqueeze:Engine`` / ``rawsqueeze:Param`` (Q10)."""
    eng = engine or ""
    par = param or ""
    body = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:rawsqueeze="http://ns.rawsqueeze.org/1.0/"\n'
        f'   xmp:CreatorTool="{software}"\n'
        f'   rawsqueeze:Engine="{eng}"\n'
        f'   rawsqueeze:Param="{par}"/>\n'
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    )
    return body.encode("utf-8")


def build_tags(
    frame_like: Any,
    bps: int | None = None,
    *,
    tile: tuple[int, int] | None = None,
    shape: tuple[int, int] | None = None,
    mosaic_max: int | None = None,
    noise_profile: bool = True,
    software: str | None = None,
    xmp: bool = True,
) -> DNGTags:
    """DNG tags for the raw IFD (DESIGN.md 3.5 step 2 / appendix A.4).

    ``frame_like``: HEAD-like dict or RawFrame.  ``tile``: (tw, th) -- sets
    TileWidth/TileLength (omit for strip layout).  Structural tags (offsets, byte counts,
    Compression, NewSubfileType, Software, DNGVersion) are added by :class:`LJ92DNG`; the
    Software string is stored on the returned object as ``tags.software``.
    """
    spec = frame_like if isinstance(frame_like, DngSpec) else dng_spec_from_head(
        frame_like, shape=shape, bps=bps, mosaic_max=mosaic_max, noise_profile=noise_profile, software=software
    )
    t = DNGTags()
    H, W = spec.height, spec.width
    t.set(Tag.ImageWidth, W)
    t.set(Tag.ImageLength, H)
    if tile is not None:
        t.set(Tag.TileWidth, int(tile[0]))
        t.set(Tag.TileLength, int(tile[1]))
    t.set(Tag.Orientation, spec.orientation)
    t.set(Tag.PhotometricInterpretation, PhotometricInterpretation.Color_Filter_Array)
    t.set(Tag.SamplesPerPixel, 1)
    t.set(Tag.BitsPerSample, spec.bps)
    t.set(Tag.PlanarConfiguration, 1)
    t.set(Tag.CFARepeatPatternDim, [2, 2])
    t.set(Tag.CFAPattern, spec.cfa)
    t.set(Tag.CFAPlaneColor, [0, 1, 2])
    t.set(Tag.CFALayout, 1)
    t.set(Tag.BlackLevelRepeatDim, [2, 2])
    t.set(Tag.BlackLevel, spec.black_per_position)
    t.set(Tag.WhiteLevel, spec.white)
    t.set(Tag.ColorMatrix1, [srat(v) for row in spec.xyz_to_cam for v in row])
    t.set(Tag.CalibrationIlluminant1, CalibrationIlluminant.D65)
    wb = spec.camera_wb
    t.set(Tag.AsShotNeutral, [_ratio(wb[1], wb[0]), [1, 1], _ratio(wb[1], wb[2])])
    t.set(Tag.BaselineExposure, [[0, 100]])
    t.set(Tag.Make, _ascii(spec.make))
    t.set(Tag.Model, _ascii(spec.model))
    t.set(Tag.UniqueCameraModel, _ascii(f"{spec.make} {spec.model}"))
    t.set(Tag.ActiveArea, spec.active_area)
    t.set(Tag.DefaultCropOrigin, spec.crop_origin)
    t.set(Tag.DefaultCropSize, spec.crop_size)
    if spec.noise_profile:
        t.set(Tag.NoiseProfile, [float(v) for v in spec.noise_profile])
    if xmp and "(" in spec.software:
        sw = spec.software
        eng = par = None
        if "(" in sw and sw.endswith(")"):
            inner = sw[sw.index("(") + 1 : -1].split(" ", 1)
            eng = inner[0]
            par = inner[1] if len(inner) > 1 else ""
        t.set(Tag.XMP_Metadata, list(xmp_packet(_ascii(sw), eng, par)))
    t.software = _ascii(spec.software)  # type: ignore[attr-defined]
    return t


# ---------------------------------------------------------------------------------------
# pixel encoding


def pack12(m: np.ndarray) -> bytes:
    """Pack uint16 samples (< 4096) as 12-bit big-endian bit order, rows byte-aligned."""
    H, W = m.shape
    a = m.astype(np.uint16, copy=False)
    if W % 2:
        a = np.concatenate([a, np.zeros((H, 1), np.uint16)], axis=1)
    a0 = a[:, 0::2]
    a1 = a[:, 1::2]
    out = np.empty((H, a0.shape[1], 3), np.uint8)
    out[..., 0] = (a0 >> 4).astype(np.uint8)
    out[..., 1] = (((a0 & 0xF) << 4) | (a1 >> 8)).astype(np.uint8)
    out[..., 2] = (a1 & 0xFF).astype(np.uint8)
    return out.tobytes()


def _lj92_tile(t: np.ndarray, bps: int) -> bytes:
    th, tw = t.shape
    t2 = np.ascontiguousarray(t.reshape(th // 2, tw * 2))
    return bytes(imagecodecs.ljpeg_encode(t2, bitspersample=bps))


def encode_lj92_tiles(raw: np.ndarray, bps: int, tile: tuple[int, int] = (256, 256), threads: int | None = None) -> list[bytes]:
    """LJ92-encode ``raw`` in row-major tiles (edge tiles zero-padded), in a thread pool."""
    tw, th = tile
    if th % 2 or tw % 16 or th % 16:
        raise ValueError(f"tile size must be a multiple of 16, got {tile}")
    H, W = raw.shape
    nx, ny = -(-W // tw), -(-H // th)
    if H % th or W % tw:
        pad = np.zeros((ny * th, nx * tw), np.uint16)
        pad[:H, :W] = raw
    else:
        pad = np.ascontiguousarray(raw, dtype=np.uint16)
    views = [pad[y * th : (y + 1) * th, x * tw : (x + 1) * tw] for y in range(ny) for x in range(nx)]
    nt = threads or os.cpu_count() or 1
    if nt <= 1:
        return [_lj92_tile(v, bps) for v in views]
    with ThreadPoolExecutor(max_workers=nt) as ex:
        return list(ex.map(lambda v: _lj92_tile(v, bps), views))


def effective_tile(width: int, tile: tuple[int, int]) -> tuple[int, int]:
    """Tile size actually used for an image of ``width``.

    LibRaw 0.22.1 mis-decodes tiled LJ92 DNGs with a single tile column wider than the
    image (rows after the first tile row come back wrong; measured).  If ``tw > width``:
    ``tw = width`` when width is a multiple of 16, else ``16 * ceil(width / 32)`` so there
    are two tile columns (the last one partial, which LibRaw handles).
    """
    tw, th = tile
    if tw > width:
        tw = width if width % 16 == 0 else max(16, 16 * -(-width // 32))
    return tw, th


class LJ92DNG(RAW2DNG):
    """pidng writer with imagecodecs LJ92 tiles (parallel) or uncompressed strips.

    Configure with attributes before ``convert``: ``compression`` ('lj92'|'none12'|'none16'),
    ``tile`` (tw, th), ``threads``.  ``options(tags, path="")`` then ``convert(mosaic)``
    returns a bytearray.
    """

    tile: tuple[int, int] = (256, 256)
    compression: str = "lj92"
    threads: int | None = None

    def __process__(self, raw: np.ndarray, tags: DNGTags, compress: bool) -> bytearray:  # noqa: D401
        W = tags.get(Tag.ImageWidth).rawValue[0]
        H = tags.get(Tag.ImageLength).rawValue[0]
        bps = tags.get(Tag.BitsPerSample).rawValue[0]
        if raw.shape != (H, W):
            raise ValueError(f"mosaic shape {raw.shape} != tags {(H, W)}")
        d = DNG()
        ifd = dngIFD()
        if self.compression == "lj92":
            strips = encode_lj92_tiles(raw, bps, self.tile, self.threads)
            off = dngTag(Tag.TileOffsets, [0] * len(strips))
            ifd.tags.append(off)
            ifd.tags.append(dngTag(Tag.TileByteCounts, [len(x) for x in strips]))
            comp = Compression.LJ92
        else:
            if self.compression == "none12":
                if bps > 12:
                    raise ValueError(f"none12 needs BitsPerSample <= 12 (got {bps})")
                strips = [pack12(raw)]
            elif self.compression == "none16":
                if bps != 16:
                    raise ValueError("none16 requires BitsPerSample=16 in the tags")
                strips = [np.ascontiguousarray(raw, dtype="<u2").tobytes()]
            else:
                raise ValueError(f"unknown compression {self.compression!r}")
            off = dngTag(Tag.StripOffsets, [0])
            ifd.tags.append(off)
            ifd.tags.append(dngTag(Tag.StripByteCounts, [len(strips[0])]))
            ifd.tags.append(dngTag(Tag.RowsPerStrip, [H]))
            comp = Compression.Uncompressed
        d.ImageDataStrips = strips
        ifd.tags.append(dngTag(Tag.NewSubfileType, [0]))
        ifd.tags.append(dngTag(Tag.Compression, [comp]))
        ifd.tags.append(dngTag(Tag.Software, getattr(tags, "software", f"rawsqueeze {__version__}")))
        ifd.tags.append(dngTag(Tag.DNGVersion, DNGVersion.V1_4))
        ifd.tags.append(dngTag(Tag.DNGBackwardVersion, DNGVersion.V1_1))
        structural = {Tag.TileOffsets[0], Tag.TileByteCounts[0], Tag.StripOffsets[0], Tag.StripByteCounts[0],
                      Tag.RowsPerStrip[0], Tag.NewSubfileType[0], Tag.Compression[0], Tag.Software[0],
                      Tag.DNGVersion[0], Tag.DNGBackwardVersion[0]}
        for tg in tags.list():
            if tg.TagId not in structural:
                ifd.tags.append(tg)
        d.IFDs.append(ifd)
        n = d.dataLen()
        off.setValue([k for _, k in d.StripOffsets.items()])
        buf = bytearray(n)
        d.setBuffer(buf)
        d.write()
        return buf


def dng_encode(
    mosaic: np.ndarray,
    head: Any,
    *,
    compression: str = "lj92",
    tile: int | tuple[int, int] = 256,
    threads: int | None = None,
    noise_profile: bool = True,
    bps: int | None = None,
) -> bytes:
    """Encode ``mosaic`` (HxW uint16) + HEAD into DNG bytes (no EXIF transfer)."""
    if compression not in COMPRESSIONS:
        raise ValueError(f"compression must be one of {COMPRESSIONS}, got {compression!r}")
    m = np.ascontiguousarray(mosaic, dtype=np.uint16)
    if m.ndim != 2:
        raise ValueError("mosaic must be 2-D")
    tl = (tile, tile) if isinstance(tile, int) else (int(tile[0]), int(tile[1]))
    if tl[0] % 16 or tl[1] % 16 or tl[0] <= 0 or tl[1] <= 0:
        raise ValueError(f"tile size must be a positive multiple of 16, got {tl}")
    tl = effective_tile(m.shape[1], tl)
    mx = int(m.max()) if m.size else 0
    if compression == "none16":
        bps = 16
    elif compression == "none12":
        bps = 12
    tags = build_tags(head, bps, tile=tl if compression == "lj92" else None, shape=m.shape, mosaic_max=mx,
                      noise_profile=noise_profile)
    used_bps = tags.get(Tag.BitsPerSample).rawValue[0]
    if mx >= (1 << used_bps):
        raise ValueError(f"mosaic max {mx} does not fit BitsPerSample={used_bps}")
    w = LJ92DNG()
    w.compression = compression
    w.tile = tl
    w.threads = threads
    w.options(tags, path="", compress=False)
    return bytes(w.convert(m))


def dng_bytes16(mosaic: np.ndarray, head: Any) -> bytes:
    """Uncompressed 16-bit DNG in memory (verify/bench development pipeline).

    No NoiseProfile/XMP; identical tags for original and reconstruction so both go
    through exactly the same LibRaw path.
    """
    m = np.ascontiguousarray(mosaic, dtype=np.uint16)
    tags = build_tags(head, 16, shape=m.shape, noise_profile=False, xmp=False, software=f"rawsqueeze {__version__}")
    w = LJ92DNG()
    w.compression = "none16"
    w.options(tags, path="", compress=False)
    return bytes(w.convert(m))


# ---------------------------------------------------------------------------------------
# file output


@dataclass
class DngReport:
    path: str
    size: int
    compression: str
    bps: int
    t_encode: float
    t_exif: float
    t_total: float
    exif: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_dng(
    mosaic: np.ndarray,
    head: Any,
    path: str | os.PathLike[str],
    *,
    compression: str = "lj92",
    tile: int | tuple[int, int] = 256,
    threads: int | None = None,
    noise_profile: bool = True,
    meta: bytes | None = None,
    et: ExifTool | None = None,
    exif: bool = True,
) -> DngReport:
    """Write a DNG atomically (tmp file in the target directory, then ``os.replace``).

    ``meta``: META payload (skeleton or exif-only); when given and ``exif`` is true the
    EXIF/MakerNotes/GPS are transferred with exiftool (``meta.transfer_exif``) into the tmp
    file before the rename.  ``path`` is used verbatim (no ``.dng`` appended).
    """
    from .meta import transfer_exif

    t0 = time.perf_counter()
    buf = dng_encode(mosaic, head, compression=compression, tile=tile, threads=threads, noise_profile=noise_profile)
    t_enc = time.perf_counter() - t0
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.stem}.", suffix=".tmp.dng", dir=dst.parent)
    warnings: list[str] = []
    exif_rep: dict[str, Any] | None = None
    t_exif = 0.0
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf)
        if meta and exif:
            t1 = time.perf_counter()
            hd = _head_dict(head)
            rep = transfer_exif(meta, tmp, et, source_name=(hd.get("source") or {}).get("name"))
            exif_rep = rep.to_dict()
            warnings += rep.warnings
            t_exif = time.perf_counter() - t1
        os.chmod(tmp, default_file_mode())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    hd = _head_dict(head)
    bps = max(12, max(int(hd["levels"]["white"]), int(np.max(mosaic)) if np.size(mosaic) else 0).bit_length())
    if compression == "none16":
        bps = 16
    elif compression == "none12":
        bps = 12
    return DngReport(
        path=os.fspath(dst),
        size=dst.stat().st_size,
        compression=compression,
        bps=bps,
        t_encode=round(t_enc, 4),
        t_exif=round(t_exif, 4),
        t_total=round(time.perf_counter() - t0, 4),
        exif=exif_rep,
        warnings=warnings,
    )


__all__ = [
    "COMPRESSIONS",
    "DngReport",
    "DngSpec",
    "FLIP_TO_ORIENTATION",
    "LJ92DNG",
    "build_tags",
    "dng_bytes16",
    "dng_encode",
    "dng_spec_from_head",
    "effective_tile",
    "encode_lj92_tiles",
    "noise_profile_from_head",
    "pack12",
    "software_string",
    "srat",
    "write_dng",
    "xmp_packet",
]
