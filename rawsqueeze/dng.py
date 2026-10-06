"""DNG reconstruction (DESIGN.md 3.5) and the in-memory 16-bit DNG used by verify.

* :func:`build_tags` -- DNG IFD tags from a HEAD-like dict (or a :class:`RawFrame`).
* :class:`LJ92DNG` -- pidng ``RAW2DNG`` subclass: 256x256 LJ92 tiles encoded in a thread
  pool in Adobe DNG Converter's layout (2 interleaved components = even/odd columns,
  predictor 1, frame ``th x tw/2``; readable by LibRaw, the Adobe SDK, Apple and rawspeed),
  or uncompressed 12/16-bit strips.
* :func:`write_dng` -- atomic write (tmp + rename), optional EXIF transfer from META.
* :func:`dng_bytes16` -- uncompressed 16-bit DNG as bytes (deterministic verify pipeline).

pidng pitfalls handled here: we never let pidng add its own Software/DNGVersion (no
duplicates), always set tiles or strips explicitly, never use ``compress=True``, write
Rationals as ``[num, den]`` pairs and BlackLevel as SHORT, and never call ``convert`` with a
filename (it appends ``.dng``).  EXIF sub-IFDs are added afterwards by exiftool.
"""

from __future__ import annotations

import base64
import os
import struct
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from pidng.core import RAW2DNG
from pidng.defs import CalibrationIlluminant, Compression, DNGVersion, PhotometricInterpretation
from pidng.dng import DNG, DNGTags, Tag, Type, dngIFD, dngTag

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
    """DNG CFAPattern codes in position order (0=R, 1=G, 2=B), relative to the ActiveArea origin."""
    black_per_position: list[int]
    """BlackLevel per position, relative to the ActiveArea origin."""
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


def rotate_cfa_phase(values: list[int], top: int, left: int) -> list[int]:
    """Re-phase a 2x2 position-order list from mosaic (0,0) to the origin (top, left).

    ``out[2*i + j] = values[2*((i + top) % 2) + (j + left) % 2]``.
    """
    dy, dx = int(top) % 2, int(left) % 2
    return [values[2 * ((i + dy) % 2) + (j + dx) % 2] for i in (0, 1) for j in (0, 1)]


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
    # HEAD's dng_cfa / black_per_position are relative to mosaic (0,0); DNG CFAPattern and
    # BlackLevel repeat patterns are relative to the ActiveArea origin (DNG 1.4 spec).
    cfa = rotate_cfa_phase([int(v) for v in cfa], top, left)
    blk = rotate_cfa_phase(blk, top, left)
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
        cfa=cfa,
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
# lens distortion: DNG opcode lists (DNG 1.4 spec, chapter 6) and the Panasonic model

OPCODE_LIST3 = (51022, Type.Undefined)
"""OpcodeList3 tag (applied to the linear, demosaiced stage-3 image = the ActiveArea)."""
OPCODE_WARP_RECTILINEAR = 1
OPCODE_FLAG_OPTIONAL = 1
OPCODE_FLAG_PREVIEW_SKIP = 2
LENS_OPCODE_DEFAULT = True
"""Write the WarpRectilinear opcode for Panasonic DistortionInfo by default (gate passed:
docs/STATUS.md; override with ``write_dng(lens_opcode=...)`` or env
``RAWSQUEEZE_DNG_LENS_OPCODE=0/1``)."""
LENS_FIT_MAX_ERR_PX = 1.0
"""Refuse the opcode when the 4-term WarpRectilinear fit deviates more than this (pixels)."""


@dataclass
class WarpRectilinear:
    """DNG opcode 1 (WarpRectilinear).

    ``planes``: one ``(kr0, kr1, kr2, kr3, kt0, kt1)`` tuple per plane (1 = all planes).
    ``cx``/``cy``: optical centre, 0..1 relative to the image (DNG SDK: ``l + cx*(r-l)`` in
    pixel-index coordinates, ``r`` exclusive).
    """

    planes: list[tuple[float, float, float, float, float, float]]
    cx: float = 0.5
    cy: float = 0.5
    version: int = 0x01030000
    flags: int = OPCODE_FLAG_OPTIONAL

    def params_bytes(self) -> bytes:
        b = struct.pack(">I", len(self.planes))
        for pl in self.planes:
            b += struct.pack(">6d", *(float(v) for v in pl))
        return b + struct.pack(">2d", float(self.cx), float(self.cy))


@dataclass
class RawOpcode:
    """An opcode this module does not interpret (kept verbatim by :func:`parse_opcode_list`)."""

    opcode_id: int
    version: int
    flags: int
    params: bytes


def opcode_list_bytes(ops: list[WarpRectilinear | RawOpcode]) -> bytes:
    """Serialise an opcode list (big-endian: count, then id/version/flags/size/params)."""
    out = struct.pack(">I", len(ops))
    for op in ops:
        if isinstance(op, WarpRectilinear):
            oid, params = OPCODE_WARP_RECTILINEAR, op.params_bytes()
        else:
            oid, params = op.opcode_id, op.params
        out += struct.pack(">4I", oid, op.version, op.flags, len(params)) + params
    return out


def parse_opcode_list(data: bytes) -> list[WarpRectilinear | RawOpcode]:
    """Inverse of :func:`opcode_list_bytes`; raises ``ValueError`` on malformed data."""
    if len(data) < 4:
        raise ValueError("opcode list shorter than 4 bytes")
    (n,) = struct.unpack_from(">I", data, 0)
    pos = 4
    ops: list[WarpRectilinear | RawOpcode] = []
    for _ in range(n):
        if pos + 16 > len(data):
            raise ValueError("truncated opcode header")
        oid, ver, flags, size = struct.unpack_from(">4I", data, pos)
        pos += 16
        params = data[pos : pos + size]
        if len(params) != size:
            raise ValueError("truncated opcode parameters")
        pos += size
        if oid == OPCODE_WARP_RECTILINEAR:
            (np_,) = struct.unpack_from(">I", params, 0)
            if size != 4 + 48 * np_ + 16 or np_ == 0:
                raise ValueError(f"bad WarpRectilinear size {size} for {np_} planes")
            planes = [tuple(struct.unpack_from(">6d", params, 4 + 48 * k)) for k in range(np_)]
            cx, cy = struct.unpack_from(">2d", params, 4 + 48 * np_)
            ops.append(WarpRectilinear(planes=planes, cx=cx, cy=cy, version=ver, flags=flags))  # type: ignore[arg-type]
        else:
            ops.append(RawOpcode(oid, ver, flags, bytes(params)))
    if pos != len(data):
        raise ValueError(f"{len(data) - pos} trailing bytes after the opcode list")
    return ops


def warp_rectilinear_norm(op: WarpRectilinear, width: int, height: int) -> tuple[float, float, float]:
    """(centre_x, centre_y, normalisation radius) in pixel-index coordinates, as the DNG SDK."""
    cx, cy = op.cx * width, op.cy * height
    nr = max(np.hypot(cx - x, cy - y) for x in (0.0, float(width)) for y in (0.0, float(height)))
    return cx, cy, float(nr)


def warp_rectilinear_src(
    x: np.ndarray, y: np.ndarray, op: WarpRectilinear, width: int, height: int, plane: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Source position for destination pixel-index coordinates ``(x, y)`` (DNG SDK
    ``dng_filter_warp::GetSrcPixelPosition``, square pixels): ``r2 = min(|d|^2/R^2, 1)``,
    ``src = c + d * (kr0 + kr1 r2 + kr2 r2^2 + kr3 r2^3) + R * tangential``."""
    k0, k1, k2, k3, t0, t1 = op.planes[min(plane, len(op.planes) - 1)]
    cx, cy, nr = warp_rectilinear_norm(op, width, height)
    dx, dy = (np.asarray(x, np.float64) - cx) / nr, (np.asarray(y, np.float64) - cy) / nr
    r2 = np.minimum(dx * dx + dy * dy, 1.0)
    ratio = k0 + r2 * (k1 + r2 * (k2 + r2 * k3))
    th = t1 * (r2 + 2.0 * dx * dx) + 2.0 * t0 * dx * dy
    tv = t0 * (r2 + 2.0 * dy * dy) + 2.0 * t1 * dx * dy
    return cx + nr * (dx * ratio + th), cy + nr * (dy * ratio + tv)


def panasonic_warp_rectilinear(
    dist: Any,
    *,
    active_hw: tuple[int, int],
    crop: tuple[int, int, int, int],
    samples: int = 2048,
) -> tuple[WarpRectilinear, float] | None:
    """WarpRectilinear equivalent of a :class:`meta.PanasonicDistortion`.

    ``active_hw``: (H, W) of the DNG ActiveArea (the stage-3 image the opcode warps).
    ``crop``: the camera output crop ``(x, y, w, h)`` in active-area coordinates (DNG
    DefaultCrop); the Panasonic model is centred on it and normalised by word 12 (pixels).
    The Panasonic polynomial maps source -> output radius; it is inverted numerically and the
    output -> source ratio is least-squares fitted with ``kr0 + kr1 r^2 + kr2 r^4 + kr3 r^6``
    over the whole stage-3 image (weights ~ radius, i.e. pixel error).  Returns
    ``(opcode, max fit error in pixels)`` or ``None`` when the correction is disabled, the
    parameters are degenerate (non-monotonic mapping) or the image is empty.
    """
    if dist is None or not getattr(dist, "enabled", False):
        return None
    H, W = int(active_hw[0]), int(active_hw[1])
    ox, oy, cw, ch = (float(v) for v in crop)
    n = float(dist.norm_radius)
    if H <= 0 or W <= 0 or cw <= 0 or ch <= 0 or n <= 0:
        return None
    # optical centre = centre of the output crop, in pixel-index coordinates
    ccx, ccy = ox + (cw - 1) / 2.0, oy + (ch - 1) / 2.0
    op = WarpRectilinear(planes=[(1.0, 0.0, 0.0, 0.0, 0.0, 0.0)], cx=ccx / W, cy=ccy / H)
    _, _, nr = warp_rectilinear_norm(op, W, H)
    u = (np.arange(1, samples + 1, dtype=np.float64) / samples)  # dst radius / nr, (0, 1]
    ru = u * nr / n  # dst radius in Panasonic units
    # invert r_out = forward(r_src) by Newton (forward is close to identity * scale)
    rd = ru / dist.scale
    for _ in range(50):
        f = dist.forward(rd) - ru
        h = 1e-7
        fp = (dist.forward(rd + h) - dist.forward(rd)) / h
        if np.any(fp <= 0):
            return None
        step = f / fp
        rd = rd - step
        if np.max(np.abs(step)) < 1e-13:
            break
    # monotonic over the used range (else the mapping folds)
    grid = np.linspace(0.0, float(rd.max()) * 1.02, 4096)
    if np.any(np.diff(dist.forward(grid)) <= 0) or np.max(np.abs(dist.forward(rd) - ru)) > 1e-9:
        return None
    ratio = rd / ru
    u2 = u * u
    A = np.stack([np.ones_like(u2), u2, u2 ** 2, u2 ** 3], axis=1)
    w = u * nr  # pixel error = ratio error * dst radius in pixels
    k, *_ = np.linalg.lstsq(A * w[:, None], ratio * w, rcond=None)
    err = float(np.max(np.abs(A @ k - ratio) * w))
    op.planes = [(float(k[0]), float(k[1]), float(k[2]), float(k[3]), 0.0, 0.0)]
    return op, err


def lens_opcode_enabled(flag: bool | None = None) -> bool:
    """Resolve the lens-opcode switch: explicit flag > env RAWSQUEEZE_DNG_LENS_OPCODE > default."""
    if flag is not None:
        return bool(flag)
    env = os.environ.get("RAWSQUEEZE_DNG_LENS_OPCODE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return LENS_OPCODE_DEFAULT


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


def _xml_attr(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def xmp_packet(
    software: str, engine: str | None, param: str | None, extra: Mapping[str, str] | None = None
) -> bytes:
    """Minimal XMP packet with ``rawsqueeze:Engine`` / ``rawsqueeze:Param`` (Q10).

    ``extra``: further ``rawsqueeze:<Name>`` attributes (e.g. ``PanasonicDistortionInfo``).
    """
    eng = engine or ""
    par = param or ""
    more = "".join(f'\n   rawsqueeze:{k}="{_xml_attr(v)}"' for k, v in (extra or {}).items())
    body = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '    xmlns:rawsqueeze="http://ns.rawsqueeze.org/1.0/"\n'
        f'   xmp:CreatorTool="{software}"\n'
        f'   rawsqueeze:Engine="{eng}"\n'
        f'   rawsqueeze:Param="{par}"{more}/>\n'
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
    xmp_extra: Mapping[str, str] | None = None,
    opcode_list3: bytes | None = None,
) -> DNGTags:
    """DNG tags for the raw IFD (DESIGN.md 3.5 step 2 / appendix A.4).

    ``frame_like``: HEAD-like dict or RawFrame.  ``tile``: (tw, th) -- sets
    TileWidth/TileLength (omit for strip layout).  Structural tags (offsets, byte counts,
    Compression, NewSubfileType, Software, DNGVersion) are added by :class:`LJ92DNG`; the
    Software string is stored on the returned object as ``tags.software``.  ``opcode_list3``:
    serialised opcode list (:func:`opcode_list_bytes`) stored as OpcodeList3 (51022).
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
    if opcode_list3:
        t.set(OPCODE_LIST3, list(opcode_list3))
    if xmp and ("(" in spec.software or xmp_extra):
        sw = spec.software
        eng = par = None
        if "(" in sw and sw.endswith(")"):
            inner = sw[sw.index("(") + 1 : -1].split(" ", 1)
            eng = inner[0]
            par = inner[1] if len(inner) > 1 else ""
        t.set(Tag.XMP_Metadata, list(xmp_packet(_ascii(sw), eng, par, xmp_extra)))
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


# Lossless JPEG (ITU T.81 process 14) tile encoder in the layout of Adobe DNG Converter:
# two interleaved components (the even / odd columns of the CFA tile), predictor 1, frame
# = th rows x tw/2 columns.  Each component's left neighbour is the same CFA colour two
# pixels to the left.  rawspeed (darktable) only accepts predictor 1 with a frame that
# tiles the DNG tile, which the single-component imagecodecs/liblj92 encoders cannot do.

_BITLEN = np.zeros(1 << 16, np.int64)
_BITLEN[1:] = np.floor(np.log2(np.arange(1, 1 << 16))).astype(np.int64) + 1


def _huffman_lengths(freq: np.ndarray, limit: int = 16) -> np.ndarray:
    """JPEG (T.81 K.2) optimal code lengths limited to ``limit`` bits; 0 for unused symbols.

    A reserved pseudo-symbol keeps any real code from being all ones.
    """
    n = len(freq)
    f = [int(v) for v in freq] + [1]  # reserved symbol n
    size = [0] * (n + 1)
    others = [-1] * (n + 1)
    while True:
        live = [(v, i) for i, v in enumerate(f) if v > 0]
        if len(live) < 2:
            break
        c1 = min(live, key=lambda t: (t[0], -t[1]))[1]
        f2 = [(v, i) for v, i in live if i != c1]
        c2 = min(f2, key=lambda t: (t[0], -t[1]))[1]
        f[c1] += f[c2]
        f[c2] = 0
        size[c1] += 1
        while others[c1] >= 0:
            c1 = others[c1]
            size[c1] += 1
        others[c1] = c2
        size[c2] += 1
        while others[c2] >= 0:
            c2 = others[c2]
            size[c2] += 1
    bits = [0] * 33
    for i in range(n + 1):
        if size[i]:
            bits[size[i]] += 1
    for i in range(32, limit, -1):  # K.3: limit code lengths
        while bits[i] > 0:
            j = i - 2
            while bits[j] == 0:
                j -= 1
            bits[i] -= 2
            bits[i - 1] += 1
            bits[j + 1] += 2
            bits[j] -= 1
    i = limit
    while bits[i] == 0:
        i -= 1
    bits[i] -= 1  # drop the reserved symbol (longest code)
    # assign lengths to symbols by decreasing frequency (ties: lower symbol first)
    order = sorted((i for i in range(n) if freq[i] > 0), key=lambda i: (-int(freq[i]), i))
    lengths = np.zeros(n, np.int64)
    k = 0
    for ln in range(1, limit + 1):
        for _ in range(bits[ln]):
            lengths[order[k]] = ln
            k += 1
    return lengths


def _lj92_tile(t: np.ndarray, bps: int) -> bytes:
    """One DNG tile as a 2-component, predictor-1 lossless JPEG (see above)."""
    th, tw = t.shape
    if tw % 2:
        raise ValueError(f"LJ92 tile width must be even, got {tw}")
    x = t.astype(np.int64)
    pred = np.empty_like(x)
    pred[:, 2:] = x[:, :-2]
    pred[1:, :2] = x[:-1, :2]
    pred[0, :2] = 1 << (bps - 1)
    d = ((x - pred) & 0xFFFF).ravel()
    d[d >= 32768] -= 65536
    ssss = _BITLEN[np.abs(d) & 0xFFFF]
    ssss[d == -32768] = 16
    freq = np.bincount(ssss, minlength=17)
    lengths = _huffman_lengths(freq)
    # canonical codes: by length, then symbol value (the DHT lists symbols in that order)
    huffval = sorted((i for i in range(17) if lengths[i]), key=lambda i: (lengths[i], i))
    codes = np.zeros(17, np.int64)
    code, prev = 0, 0
    for sym in huffval:
        code <<= int(lengths[sym]) - prev
        prev = int(lengths[sym])
        codes[sym] = code
        code += 1
    nextra = np.where(ssss == 16, 0, ssss)
    extra = np.where(d < 0, d - 1, d) & ((1 << nextra) - 1)
    L = lengths[ssss] + nextra
    v = (codes[ssss] << nextra) | extra
    end = np.cumsum(L)
    total = int(end[-1])
    pos = end - L
    word = pos >> 5
    off = pos & 31
    spill = off + L - 32
    fits = spill <= 0
    hi = np.where(fits, v << np.maximum(-spill, 0), v >> np.maximum(spill, 0))
    nw = (total + 31) // 32 + 1
    words = np.bincount(word, weights=hi.astype(np.float64), minlength=nw)
    if not fits.all():
        sp = ~fits
        lo = (v[sp] & ((1 << spill[sp]) - 1)) << (32 - spill[sp])
        words += np.bincount(word[sp] + 1, weights=lo.astype(np.float64), minlength=nw)
    w32 = words.astype(np.uint64).astype(np.uint32)
    nbytes = (total + 7) // 8
    pad = nbytes * 8 - total
    if pad:  # fill the last byte with 1-bits (T.81 F.1.2.3)
        last = total - 1 + pad  # index of the last bit
        w32[last >> 5] |= np.uint32(((1 << pad) - 1) << (31 - (last & 31)))
    data = np.frombuffer(w32.astype(">u4").tobytes()[:nbytes], np.uint8)
    ff = np.flatnonzero(data == 0xFF)
    if ff.size:
        data = np.insert(data, ff + 1, 0)  # byte stuffing
    nsym = len(huffval)
    hdr = bytearray(b"\xff\xd8")
    hdr += b"\xff\xc3" + bytes([0, 14, bps, th >> 8, th & 255, (tw // 2) >> 8, (tw // 2) & 255, 2,
                                  0, 0x11, 0, 1, 0x11, 0])
    bits_count = [sum(1 for s in huffval if lengths[s] == ln) for ln in range(1, 17)]
    hdr += b"\xff\xc4" + bytes([0, 19 + nsym, 0]) + bytes(bits_count) + bytes(huffval)
    hdr += b"\xff\xda" + bytes([0, 10, 2, 0, 0x00, 1, 0x00, 1, 0, 0])
    return bytes(hdr) + data.tobytes() + b"\xff\xd9"


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

    LibRaw also treats a 2-component LJ92 tile specially when ``2 * tw == width`` (it then
    reads two tile rows per JPEG row: ``jh.clrs * jwide == raw_width`` in
    ``lossless_dng_load_raw``), so that width is avoided by halving the tile width.
    """
    tw, th = tile
    if tw > width:
        tw = width if width % 16 == 0 else max(16, 16 * -(-width // 32))
    if 2 * tw == width:
        tw = tw // 2 if (tw // 2) % 16 == 0 else width
    return tw, th


class LJ92DNG(RAW2DNG):
    """pidng writer with LJ92 tiles (:func:`_lj92_tile`, parallel) or uncompressed strips.

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
    xmp_extra: Mapping[str, str] | None = None,
    opcodes3: list[WarpRectilinear | RawOpcode] | None = None,
) -> bytes:
    """Encode ``mosaic`` (HxW uint16) + HEAD into DNG bytes (no EXIF transfer).

    ``opcodes3``: opcodes for OpcodeList3 (e.g. the lens WarpRectilinear of
    :func:`panasonic_warp_rectilinear`); omitted when empty.
    """
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
                      noise_profile=noise_profile, xmp_extra=xmp_extra,
                      opcode_list3=opcode_list_bytes(opcodes3) if opcodes3 else None)
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


def _panasonic_lens_opcode(
    dist_raw: bytes, mosaic: np.ndarray, head: Any, flag: bool | None, parse: Any
) -> tuple[dict[str, Any], WarpRectilinear | None, str | None]:
    """(report dict, opcode or None, reason for no opcode or None when intentionally off)."""
    info: dict[str, Any] = {"source": "panasonic", "opcode": False}
    dist = parse(dist_raw)
    if dist is None:
        return info, None, "unrecognised DistortionInfo layout"
    info["checksum_ok"] = dist.checksum_ok
    if not dist.enabled:
        info["reason"] = "correction off in camera"
        return info, None, None
    if not lens_opcode_enabled(flag):
        info["reason"] = "disabled"
        return info, None, "disabled"
    if not dist.checksum_ok:
        info["reason"] = "checksum mismatch"
        return info, None, "DistortionInfo checksum mismatch"
    spec = dng_spec_from_head(head, shape=np.shape(mosaic), noise_profile=False)
    t, l, b, r = spec.active_area
    res = panasonic_warp_rectilinear(dist, active_hw=(b - t, r - l), crop=(*spec.crop_origin, *spec.crop_size))
    if res is None:
        info["reason"] = "degenerate parameters"
        return info, None, "degenerate parameters"
    op, err = res
    info.update(fit_err_px=round(err, 4), k=list(op.planes[0][:4]), cx=op.cx, cy=op.cy)
    if err > LENS_FIT_MAX_ERR_PX:
        info["reason"] = "fit error"
        return info, None, f"WarpRectilinear fit error {err:.2f} px > {LENS_FIT_MAX_ERR_PX} px"
    info["opcode"] = True
    return info, op, None


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
    lens: dict[str, Any] | None = None
    """Lens-distortion handling: ``{"source": "panasonic", "opcode": bool, "fit_err_px", "k", "cx", "cy"}``."""

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
    lens_opcode: bool | None = None,
) -> DngReport:
    """Write a DNG atomically (tmp file in the target directory, then ``os.replace``).

    ``meta``: META payload (skeleton or exif-only); when given and ``exif`` is true the
    EXIF/MakerNotes/GPS are transferred with exiftool (``meta.transfer_exif``) into the tmp
    file before the rename.  ``path`` is used verbatim (no ``.dng`` appended).

    ``lens_opcode``: convert Panasonic in-camera distortion correction (DistortionInfo in the
    META skeleton) to an OpcodeList3 WarpRectilinear opcode (None = :func:`lens_opcode_enabled`
    default).  The raw parameters are always kept in XMP ``rawsqueeze:PanasonicDistortionInfo``.
    """
    from .meta import panasonic_distortion_info, parse_panasonic_distortion, transfer_exif

    t0 = time.perf_counter()
    warnings: list[str] = []
    xmp_extra: dict[str, str] = {}
    opcodes3: list[WarpRectilinear | RawOpcode] = []
    lens_info: dict[str, Any] | None = None
    if meta:
        dist_raw = panasonic_distortion_info(meta)
        if dist_raw:
            xmp_extra["PanasonicDistortionInfo"] = base64.b64encode(dist_raw).decode("ascii")
            lens_info, op, why = _panasonic_lens_opcode(dist_raw, mosaic, head, lens_opcode, parse_panasonic_distortion)
            if op is not None:
                opcodes3.append(op)
            elif why:
                warnings.append(f"camera lens distortion correction (Panasonic DistortionInfo) not written as a DNG "
                                f"WarpRectilinear opcode ({why}): Adobe/Apple renderers show the uncorrected lens "
                                f"geometry (parameters kept in XMP rawsqueeze:PanasonicDistortionInfo)")
    buf = dng_encode(mosaic, head, compression=compression, tile=tile, threads=threads, noise_profile=noise_profile,
                     xmp_extra=xmp_extra or None, opcodes3=opcodes3 or None)
    t_enc = time.perf_counter() - t0
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.stem}.", suffix=".tmp.dng", dir=dst.parent)
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
        lens=lens_info,
    )


__all__ = [
    "COMPRESSIONS",
    "DngReport",
    "DngSpec",
    "FLIP_TO_ORIENTATION",
    "LENS_OPCODE_DEFAULT",
    "LJ92DNG",
    "OPCODE_LIST3",
    "RawOpcode",
    "WarpRectilinear",
    "build_tags",
    "dng_bytes16",
    "dng_encode",
    "dng_spec_from_head",
    "effective_tile",
    "encode_lj92_tiles",
    "lens_opcode_enabled",
    "opcode_list_bytes",
    "panasonic_warp_rectilinear",
    "parse_opcode_list",
    "noise_profile_from_head",
    "pack12",
    "rotate_cfa_phase",
    "software_string",
    "srat",
    "warp_rectilinear_norm",
    "warp_rectilinear_src",
    "write_dng",
    "xmp_packet",
]
