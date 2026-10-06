"""Verification: deterministic development + perceptual / noise metrics (DESIGN.md 6).

Pipeline (identical for original and reconstruction):
``dng_bytes16(mosaic, head)`` -> ``rawpy.imread(BytesIO)`` -> ``postprocess(use_camera_wb,
no_auto_bright, output_bps=16, exp_shift=2**ev, exp_preserve_highlights=0, gamma=(2.4,12.92),
user_flip=0, AHD, highlight Clip)`` -> DefaultCrop.  LibRaw's exp_shift is limited to
0.25..8, so EVs outside [-2, +3] are rendered linearly (``gamma=(1,1)``) and the gain plus
LibRaw's own (2.4, 12.92) tone curve are applied in numpy.

Metrics per tile and EV: ssimulacra2 (last token), butteraugli_main ``--pnorm 3`` (line 1 =
max, ``3-norm:`` line = p3), PSNR on 16-bit, ss2 on a 2x2 (linear-light) downsample.
Tiles: centre, darkest, highest local variance, seeded random (2048^2); worst tile per EV.
Plus a FLOOR control (original + random 0/1 DN), raw-domain RMSE/sigma, +3EV bias and
high-pass noise-std ratio, and highlight clipping consistency.  :data:`ACCEPTANCE` holds
the per-engine criteria of DESIGN.md 6.4 as data, recalibrated as functions of the quality
parameter (:func:`acceptance_criteria`, :func:`nlq_criteria`, :func:`half3_criteria`).
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .dng import dng_bytes16, dng_spec_from_head
from .tools import ToolError, have, run

DEFAULT_EVS: tuple[float, ...] = (0.0, 2.0, 3.0)
DEFAULT_METRICS: tuple[str, ...] = ("psnr", "ssimulacra2", "butteraugli", "noise")
KNOWN_METRICS: tuple[str, ...] = DEFAULT_METRICS
FLOOR_METRICS: tuple[str, ...] = ("psnr", "ssimulacra2")
TILE_SIZE = 2048
FLOOR_SEED = 5
"""Seed of the FLOOR perturbation (designer B used ``default_rng(5)``)."""
LIBRAW_GAMMA = (2.4, 12.92)


# ---------------------------------------------------------------------------------------
# development


def _gamma_params(pwr: float, ts: float) -> tuple[float, float, float, float, float]:
    """dcraw/LibRaw ``gamma_curve`` parameters g[0..4] for ``gamma=(1/pwr', ts)``.

    ``pwr`` here is LibRaw's ``gamm[0]`` (= 1/2.4 for rawpy ``gamma=(2.4, 12.92)``).
    """
    g = [pwr, ts, 0.0, 0.0, 0.0]
    bnd = [0.0, 0.0]
    bnd[1 if g[1] >= 1 else 0] = 1.0
    if g[1] and (g[1] - 1) * (g[0] - 1) <= 0:
        for _ in range(48):
            g[2] = (bnd[0] + bnd[1]) / 2
            if g[0]:
                bnd[1 if ((g[2] / g[1]) ** (-g[0]) - 1) / g[0] - 1 / g[2] > -1 else 0] = g[2]
            else:
                bnd[1 if g[2] / math.exp(1 - 1 / g[2]) < g[1] else 0] = g[2]
        g[3] = g[2] / g[1]
        if g[0]:
            g[4] = g[2] * (1 / g[0] - 1)
    return g[0], g[1], g[2], g[3], g[4]


_G = _gamma_params(1 / LIBRAW_GAMMA[0], LIBRAW_GAMMA[1])


def libraw_oetf(r: np.ndarray) -> np.ndarray:
    """LibRaw's forward tone curve for ``gamma=(2.4, 12.92)`` on linear values r in [0, 1]."""
    g0, g1, _, g3, g4 = _G
    r = np.clip(r, 0.0, 1.0)
    return np.where(r < g3, r * g1, np.power(r, g0) * (1 + g4) - g4)


def libraw_eotf(v: np.ndarray) -> np.ndarray:
    """Inverse of :func:`libraw_oetf` (v in [0, 1] -> linear)."""
    g0, g1, _, g3, g4 = _G
    v = np.clip(v, 0.0, 1.0)
    return np.where(v < g3 * g1, v / g1, np.power((v + g4) / (1 + g4), 1 / g0))


def _postprocess(buf: bytes, ev: float, *, linear: bool = False) -> np.ndarray:
    import rawpy

    with rawpy.imread(io.BytesIO(buf)) as r:
        return r.postprocess(
            use_camera_wb=True,
            no_auto_bright=True,
            output_bps=16,
            exp_shift=1.0 if linear else 2.0**ev,
            exp_preserve_highlights=0.0,
            gamma=(1, 1) if linear else LIBRAW_GAMMA,
            user_flip=0,
            demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
            highlight_mode=rawpy.HighlightMode.Clip,
        )


def _crop_box(head: Mapping[str, Any], shape: tuple[int, int]) -> tuple[int, int, int, int]:
    """(y, x, h, w) of the DefaultCrop inside LibRaw's output (= active area)."""
    spec = dng_spec_from_head(head, shape=shape, bps=16, noise_profile=False)
    ox, oy = spec.crop_origin
    cw, ch = spec.crop_size
    return oy, ox, ch, cw


def develop_buf(buf: bytes, ev: float, crop: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """Develop DNG bytes at ``ev`` -> uint16 HxWx3 (cropped if ``crop=(y, x, h, w)``)."""
    if -2.0 <= ev <= 3.0:
        img = _postprocess(buf, ev)
    else:
        lin = _postprocess(buf, 0.0, linear=True).astype(np.float64)
        gain = 2.0**ev
        img = np.minimum(np.floor(65536.0 * libraw_oetf(lin * gain / 65536.0)), 65535).astype(np.uint16)
    if crop is not None:
        y, x, h, w = crop
        img = img[y : y + h, x : x + w]
    return np.ascontiguousarray(img)


def develop(mosaic: np.ndarray, head: Mapping[str, Any], ev: float = 0.0) -> np.ndarray:
    """Deterministic development of a mosaic (DESIGN.md 6.1) -> uint16 HxWx3, DefaultCrop."""
    buf = dng_bytes16(mosaic, head)
    return develop_buf(buf, ev, _crop_box(head, mosaic.shape))


def develop_many(
    mosaics: Mapping[str, np.ndarray],
    head: Mapping[str, Any],
    evs: Sequence[float],
    *,
    threads: int | None = None,
) -> dict[tuple[str, float], np.ndarray]:
    """Develop several mosaics at several EVs in a thread pool (rawpy releases the GIL)."""
    crops = {k: _crop_box(head, m.shape) for k, m in mosaics.items()}
    bufs = {k: dng_bytes16(m, head) for k, m in mosaics.items()}
    jobs = [(k, float(ev)) for k in mosaics for ev in evs]
    nt = max(1, min(len(jobs), threads or os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=nt) as ex:
        res = list(ex.map(lambda j: develop_buf(bufs[j[0]], j[1], crops[j[0]]), jobs))
    return dict(zip(jobs, res))


# ---------------------------------------------------------------------------------------
# tiles


@dataclass
class TileRect:
    name: str
    """'center' | 'darkest' | 'variance' | 'random' | 'full'."""
    y: int
    x: int
    h: int
    w: int

    def slice(self, img: np.ndarray) -> np.ndarray:
        return img[self.y : self.y + self.h, self.x : self.x + self.w]

    def overlap(self, o: TileRect) -> float:
        dy = max(0, min(self.y + self.h, o.y + o.h) - max(self.y, o.y))
        dx = max(0, min(self.x + self.w, o.x + o.w) - max(self.x, o.x))
        return dy * dx / float(self.h * self.w)


def _luma(img: np.ndarray) -> np.ndarray:
    a = img.astype(np.float32)
    return 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]


def _block_reduce(a: np.ndarray, b: int) -> tuple[np.ndarray, np.ndarray]:
    """(block mean, block variance) of a 2-D array over b x b blocks (edges dropped)."""
    h, w = (a.shape[0] // b) * b, (a.shape[1] // b) * b
    v = a[:h, :w].reshape(h // b, b, w // b, b).astype(np.float64)
    m = v.mean(axis=(1, 3))
    m2 = (v * v).mean(axis=(1, 3))
    return m, np.maximum(m2 - m * m, 0.0)


def _box_means(a: np.ndarray, kh: int, kw: int, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    ii = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
    ii[1:, 1:] = a.cumsum(0).cumsum(1)
    Y, X = np.meshgrid(ys, xs, indexing="ij")
    s = ii[Y + kh, X + kw] - ii[Y, X + kw] - ii[Y + kh, X] + ii[Y, X]
    return s / (kh * kw)


def pick_tiles(img: np.ndarray, n: int = 4, seed: int = 0, size: int = TILE_SIZE) -> list[TileRect]:
    """Choose up to ``n`` tiles of ``size``^2 on the original's 0EV rendering.

    Order: centre, darkest mean luminance (tiles darker than 8-bit level 1 are skipped
    unless all are), highest mean local (8x8) variance, seeded random.  Tiles after the
    first avoid > 25 % overlap with earlier ones when possible.  Origins are even.  If the
    image is smaller than ``size`` the tile is the whole image (single tile).
    """
    H, W = img.shape[:2]
    th, tw = min(size, H), min(size, W)
    if th == H and tw == W:
        return [TileRect("full", 0, 0, H, W)]
    out: list[TileRect] = [TileRect("center", ((H - th) // 2) & ~1, ((W - tw) // 2) & ~1, th, tw)]
    if n <= 1:
        return out
    B = 8
    lum = _luma(img)
    bm, bv = _block_reduce(lum, B)
    kh, kw = max(1, th // B), max(1, tw // B)
    step = max(1, min(kh, kw) // 8)
    ys = np.unique(np.r_[np.arange(0, bm.shape[0] - kh + 1, step), bm.shape[0] - kh])
    xs = np.unique(np.r_[np.arange(0, bm.shape[1] - kw + 1, step), bm.shape[1] - kw])
    ys, xs = ys[ys >= 0], xs[xs >= 0]
    mean_l = _box_means(bm, kh, kw, ys, xs)
    mean_v = _box_means(bv, kh, kw, ys, xs)
    Y, X = np.meshgrid(ys * B, xs * B, indexing="ij")

    def pick(order: np.ndarray, name: str) -> None:
        cands = [TileRect(name, int(Y.flat[i]) & ~1, int(X.flat[i]) & ~1, th, tw) for i in order[:4000]]
        for c in cands:
            if all(c.overlap(o) <= 0.25 for o in out):
                out.append(c)
                return
        if cands:
            out.append(cands[0])

    lvl1 = 65535.0 / 255.0
    dark_order = np.argsort(mean_l, axis=None, kind="stable")
    bright_enough = mean_l.flat[dark_order] >= lvl1
    if bright_enough.any():
        dark_order = np.r_[dark_order[bright_enough], dark_order[~bright_enough]]
    pick(dark_order, "darkest")
    if n >= 3:
        pick(np.argsort(-mean_v, axis=None, kind="stable"), "variance")
    if n >= 4:
        rng = np.random.default_rng(seed)
        for _ in range(64):
            c = TileRect("random", int(rng.integers(0, H - th + 1)) & ~1, int(rng.integers(0, W - tw + 1)) & ~1, th, tw)
            if all(c.overlap(o) <= 0.25 for o in out):
                break
        out.append(c)
    return out[:n]


# ---------------------------------------------------------------------------------------
# metrics


def write_ppm16(path: str | os.PathLike[str], img: np.ndarray) -> None:
    """Binary 16-bit PPM (P6, maxval 65535, big-endian samples)."""
    a = np.ascontiguousarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError("expected HxWx3")
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n65535\n" % (a.shape[1], a.shape[0]))
        f.write(a.astype(">u2").tobytes())


def psnr16(a: np.ndarray, b: np.ndarray) -> float:
    """PSNR in dB with peak 65535 (``inf`` for identical images)."""
    d = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(d * d))
    return math.inf if mse == 0 else 10.0 * math.log10(65535.0**2 / mse)


_FLOAT_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_ssimulacra2(out: str) -> float:
    """Score = last numeric token of ssimulacra2's stdout."""
    toks = _FLOAT_RE.findall(out)
    if not toks:
        raise ToolError(f"cannot parse ssimulacra2 output: {out!r}")
    return float(toks[-1])


def parse_butteraugli(out: str) -> tuple[float, float | None]:
    """(max, p3) from butteraugli_main output: line 1 = max, ``3-norm: x`` line = p3."""
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    if not lines:
        raise ToolError("empty butteraugli output")
    toks = _FLOAT_RE.findall(lines[0])
    if not toks:
        raise ToolError(f"cannot parse butteraugli output: {out!r}")
    mx = float(toks[0])
    p3 = None
    for ln in lines:
        if "3-norm" in ln:
            p3 = float(_FLOAT_RE.findall(ln.split(":", 1)[1])[-1])
    return mx, p3


def ssimulacra2(ref: str | os.PathLike[str], dist: str | os.PathLike[str]) -> float:
    return parse_ssimulacra2(run(["ssimulacra2", ref, dist], timeout=600).stdout.decode())


def butteraugli(ref: str | os.PathLike[str], dist: str | os.PathLike[str]) -> tuple[float, float | None]:
    return parse_butteraugli(run(["butteraugli_main", ref, dist, "--pnorm", "3"], timeout=1200).stdout.decode())


def downsample2_linear(img: np.ndarray) -> np.ndarray:
    """2x2 area downsample in linear light (through LibRaw's curve), back to uint16."""
    h, w = (img.shape[0] // 2) * 2, (img.shape[1] // 2) * 2
    lin = libraw_eotf(img[:h, :w].astype(np.float64) / 65535.0)
    lin = 0.25 * (lin[0::2, 0::2] + lin[1::2, 0::2] + lin[0::2, 1::2] + lin[1::2, 1::2])
    return np.clip(np.rint(libraw_oetf(lin) * 65535.0), 0, 65535).astype(np.uint16)


@dataclass
class TileMetrics:
    tile: str
    ev: float
    psnr: float | None = None
    ss2: float | None = None
    ss2_ds2: float | None = None
    ba_max: float | None = None
    ba_p3: float | None = None


def image_metrics(
    ref: np.ndarray,
    dist: np.ndarray,
    metrics: Iterable[str] = DEFAULT_METRICS,
    *,
    workdir: str | os.PathLike[str] | None = None,
    tag: str = "m",
    ds2: bool = True,
) -> dict[str, float | None]:
    """Full-reference metrics between two uint16 HxWx3 images.

    Returns ``{"psnr", "ss2", "ss2_ds2", "ba_max", "ba_p3"}`` (None where not requested or
    the tool is missing).  Identical inputs short-circuit to ss2=100, ba=0, psnr=inf
    without running the tools.
    """
    ms = set(metrics)
    res: dict[str, float | None] = {"psnr": None, "ss2": None, "ss2_ds2": None, "ba_max": None, "ba_p3": None}
    identical = ref.shape == dist.shape and np.array_equal(ref, dist)
    if "psnr" in ms:
        res["psnr"] = math.inf if identical else psnr16(ref, dist)
    want_ss2 = "ssimulacra2" in ms and have("ssimulacra2")
    want_ba = "butteraugli" in ms and have("butteraugli_main")
    if identical:
        if want_ss2:
            res["ss2"] = 100.0
            res["ss2_ds2"] = 100.0 if ds2 else None
        if want_ba:
            res["ba_max"], res["ba_p3"] = 0.0, 0.0
        return res
    if not (want_ss2 or want_ba):
        return res
    with tempfile.TemporaryDirectory(prefix="rsq_v_", dir=workdir) as td:
        a, b = os.path.join(td, f"{tag}_a.ppm"), os.path.join(td, f"{tag}_b.ppm")
        write_ppm16(a, ref)
        write_ppm16(b, dist)
        if want_ss2:
            res["ss2"] = ssimulacra2(a, b)
            if ds2:
                a2, b2 = os.path.join(td, f"{tag}_a2.ppm"), os.path.join(td, f"{tag}_b2.ppm")
                write_ppm16(a2, downsample2_linear(ref))
                write_ppm16(b2, downsample2_linear(dist))
                res["ss2_ds2"] = ssimulacra2(a2, b2)
        if want_ba:
            res["ba_max"], res["ba_p3"] = butteraugli(a, b)
    return res


def floor_control(m: np.ndarray, seed: int = FLOOR_SEED, *, max_value: int | None = None) -> np.ndarray:
    """FLOOR reference: ``clip(m + U{0,1}, 0, max_value)`` with a fixed seed.

    ``max_value`` defaults to ``2**bits - 1`` with ``bits = max(12, bit_length(max(m)))``.
    """
    if max_value is None:
        max_value = (1 << max(12, int(m.max()).bit_length())) - 1
    rng = np.random.default_rng(seed)
    return np.clip(m.astype(np.int32) + rng.integers(0, 2, m.shape, dtype=np.int32), 0, max_value).astype(np.uint16)


# ---------------------------------------------------------------------------------------
# noise-relative metrics


def _noise_planes(head: Mapping[str, Any], noise: Sequence[Any] | None) -> list[tuple[float, float]] | None:
    if noise is not None:
        out = []
        for p in noise:
            if isinstance(p, Mapping):
                out.append((float(p.get("g_used", p.get("g"))), float(p["s2"])))
            elif hasattr(p, "g"):
                out.append((float(p.g), float(p.s2)))
            else:
                out.append((float(p[0]), float(p[1])))
        return out
    planes = (head.get("noise") or {}).get("planes")
    if not planes or len(planes) != 4:
        return None
    try:
        return [(float(p.get("g_used", p.get("g"))), float(p["s2"])) for p in planes]
    except (TypeError, KeyError):
        return None


def raw_noise_metrics(
    orig: np.ndarray,
    rec: np.ndarray,
    head: Mapping[str, Any],
    *,
    noise: Sequence[Any] | None = None,
    nbins: int = 16,
) -> dict[str, Any]:
    """Raw-domain error relative to the noise model, per CFA position and 16 level bins.

    ``RMSE(R - M) / sigma_model(M)`` with ``sigma^2 = g*(M - blk) + s2`` (``head["noise"]``
    planes or ``noise``).  Saturated pixels (M >= white) are excluded.  Bins are
    equal-population quantiles of M per position.  Returns ``{"positions": [{"rmse_dn",
    "rmse_sigma", "bias_dn", "bins": [{"lo","hi","n","rmse_sigma"}...]}...],
    "rmse_sigma_max": ..., "model": True|False}``; without a noise model only DN values.
    """
    from .cfa import POSITIONS

    levels = head["levels"]
    blk = [float(v) for v in levels["black_per_position"]]
    wl = float(levels["white"])
    params = _noise_planes(head, noise)
    H2, W2 = (orig.shape[0] // 2) * 2, (orig.shape[1] // 2) * 2
    pos_out: list[dict[str, Any]] = []
    worst = 0.0
    for k, (dy, dx) in enumerate(POSITIONS):
        M = orig[dy:H2:2, dx:W2:2].astype(np.float64).ravel()
        R = rec[dy:H2:2, dx:W2:2].astype(np.float64).ravel()
        ok = M < wl
        M, R = M[ok], R[ok]
        if M.size == 0:
            pos_out.append({"n": 0})
            continue
        d = R - M
        entry: dict[str, Any] = {
            "n": int(M.size),
            "rmse_dn": float(np.sqrt(np.mean(d * d))),
            "bias_dn": float(d.mean()),
        }
        if params is not None:
            g, s2 = params[k]
            sig = np.sqrt(np.maximum(g * (M - blk[k]) + s2, 1e-6))
            z = d / sig
            entry["rmse_sigma"] = float(np.sqrt(np.mean(z * z)))
            edges = np.unique(np.quantile(M, np.linspace(0, 1, nbins + 1)))
            idx = np.clip(np.searchsorted(edges, M, side="right") - 1, 0, len(edges) - 2) if len(edges) > 1 else np.zeros(M.size, int)
            nb = max(1, len(edges) - 1)
            cnt = np.bincount(idx, minlength=nb)
            ss = np.bincount(idx, weights=z * z, minlength=nb)
            bins = []
            for b in range(nb):
                if cnt[b] == 0:
                    continue
                v = float(np.sqrt(ss[b] / cnt[b]))
                bins.append({"lo": float(edges[b]), "hi": float(edges[min(b + 1, len(edges) - 1)]), "n": int(cnt[b]), "rmse_sigma": v})
            entry["bins"] = bins
            worst = max(worst, entry["rmse_sigma"])
        pos_out.append(entry)
    return {
        "model": params is not None,
        "positions": pos_out,
        "rmse_sigma_max": worst if params is not None else None,
    }


def _gauss_blur(img: np.ndarray, sigma: float = 2.0) -> np.ndarray:
    from scipy.ndimage import gaussian_filter

    s = (sigma, sigma, 0) if img.ndim == 3 else (sigma, sigma)
    return gaussian_filter(img, s)


def bias8(ref: np.ndarray, dist: np.ndarray) -> float:
    """Mean (dist - ref) in sRGB-8bit levels (inputs uint16 tone-mapped)."""
    return float((dist.astype(np.float64) - ref.astype(np.float64)).mean() * 255.0 / 65535.0)


def noise_std_ratio(ref: np.ndarray, dist: np.ndarray, *, block: int = 32, flat_pct: float = 25.0) -> float | None:
    """High-pass noise std ratio dist/ref measured in flat regions.

    ``hp = x - gauss(x, 2)``.  Flat regions: ``block``^2 blocks whose low-pass texture (std of
    ``gauss(ref)``) is in the lowest ``flat_pct`` percent and that are neither clipped
    (> 98 % of full scale) nor black (< 0.5 %).  Returns None if no block qualifies.
    """
    a = ref.astype(np.float32)
    b = dist.astype(np.float32)
    la = _gauss_blur(a)
    ha, hb = a - la, b - _gauss_blur(b)
    H, W = (a.shape[0] // block) * block, (a.shape[1] // block) * block
    if H == 0 or W == 0:
        return None

    def blocks(x: np.ndarray) -> np.ndarray:
        return x[:H, :W].reshape(H // block, block, W // block, block, -1).transpose(0, 2, 1, 3, 4).reshape(H // block, W // block, -1)

    lab = blocks(la)
    tex = lab.std(axis=2)
    mean = lab.mean(axis=2)
    lo, hi = 0.005 * 65535, 0.98 * 65535
    valid = (mean > lo) & (blocks(a).max(axis=2) < hi)
    if not valid.any():
        return None
    thr = np.percentile(tex[valid], flat_pct)
    sel = valid & (tex <= thr)
    ha_s, hb_s = blocks(ha)[sel], blocks(hb)[sel]
    sa = float(ha_s.std())
    return float(hb_s.std() / sa) if sa > 0 else None


def clip_consistency(orig: np.ndarray, rec: np.ndarray, white: int) -> dict[str, int]:
    """Clipped highlights must stay clipped.

    ``inconsistent``: pixels with ``M >= white`` but ``R < white`` (DESIGN.md 6.2.6 says
    ``R == white``; for lossless, where values above white are kept, ``R >= white`` is the
    meaningful condition).  ``false_clip``: ``M < white`` but ``R >= white`` (informative).
    """
    m_sat = orig >= white
    r_sat = rec >= white
    return {
        "n_clipped": int(m_sat.sum()),
        "inconsistent": int((m_sat & ~r_sat).sum()),
        "false_clip": int((~m_sat & r_sat).sum()),
    }


# ---------------------------------------------------------------------------------------
# acceptance (DESIGN.md 6.4, recalibrated 2026-10 on 13 DC-S9 samples, ISO 100-51200)
#
# The criteria are functions of the engine's quality parameter (nlq: f, half3: d), so a
# file encoded with e.g. ``--preset vl --f 1.5`` is judged by f = 1.5.  The preset only
# supplies the parameter when the HEAD has none.  Every value is the worst of 4 x 2048^2
# tiles (centre, darkest, highest variance, random), AHD via DNG, as in DESIGN.md 6.1.
#
# nlq (noise-relative; theory for a uniform quantiser of step f*sigma):
#   raw RMSE/sigma  = f/sqrt(12)              -> limit 1.2*f/sqrt(12) + 0.03
#   noise std ratio = sqrt(1 + f^2/12)        -> limit sqrt(1 + (1.2*f)^2/12) + 0.03
#   |bias8| (+3EV, 8-bit sRGB levels, centre/darkest tile) -> limit 0.75 + 0.25*f^2
#   f <= 0.5 only, low ISO only: ss2 >= FLOOR - 3 at every EV.
#   (x1.2 / +0.03: estimator slack.  Measured at f = 1 on 13 files: RMSE/sigma 0.285-0.328
#   (integer rounding adds ~0.03 at ISO 100, where sigma is 1-4 DN), noise ratio 1.009-1.066
#   (flat-block estimator reads ~+0.025 above theory), |bias8| 0.009-0.647 (integer
#   centroid LUT: raw mean error up to -0.28 DN on the PANA0003 night sky; a Gaussian
#   control of the same error variance gives 0.14).  Limits at f = 0.5 / 1 / 2:
#   RMSE/sigma 0.203 / 0.376 / 0.723, noise ratio 1.045 / 1.088 / 1.247, |bias8| 0.81 /
#   1.00 / 1.75.  A visible nlq defect (wrong noise model, LUT off by >= 1 DN) moves these
#   by far more than the slack.)
#
# half3 (perceptual; anchors at d = 0.1 / 0.2 / 0.3 = presets high / vl / compact, linear in
# d in between and extrapolated with the outer slopes, d clamped to [0.05, 0.6]).  Measured
# populations (worst of 4 tiles, H3FX guard on):
#
#   d    | visually lossless at 4x / +3EV: P1060444,    | degraded: forced half3 at ISO >= 640 | limit
#        | PANA9831, P1060384 (ISO 100-320), worst value | (soft / flattened grain), best value |
#   -----+-----------------------------------------------+--------------------------------------+------
#   0.1  | ss2 0/+2/+3 88.1 / 85.0 / 81.8, p3 0.90, max 4.0 | ISO800 88.6 / 81.1 / 76.2, p3 1.06 | 84 / 82 / 78, p3 1.2, max 6
#   0.2  | ss2 84.3 / 79.6 / 76.8, p3 1.23, max 4.8      | ISO800 85.3 / 74.9 / 68.3, p3 1.37   | 80 / 77 / 72, p3 1.5, max 8
#   0.3  | ss2 81.2 / 75.4 / 71.7, p3 1.52, max 6.2      | ISO800 82.8 / 70.3 / 62.4, p3 1.57   | 78 / 73 / 67, p3 1.8, max 9
#
#   (At d 0.1 the ISO800 file is about as good as nlq vl -- below the bar of preset high.)
#   +3EV ss2 is the main separator (margins at d 0.2: +4.8 good / -3.7 bad), +2EV ss2 the
#   second (+2.6 / -2.1); 0EV ss2 does not separate (sanity floor only).  ba p3 is set to
#   catch gross failures (ISO640 before the gamut guard 7.56, ISO3200-4000 1.7-5.1) with
#   ~20 % headroom over the good files; the soft-grain population (ba p3 1.37-1.6) is
#   separated by ss2.  ba max catches localised artefacts (good <= 6.2, gamut clamp 22).
#   The noise std ratio is not used for half3: it reads 0.58-0.71 on P1060384 (ISO 320,
#   clean sky, invisible) but 0.83-0.97 on the degraded high-ISO files.  At vl every forced
#   half3 file at ISO >= 640 fails (ISO640 66.7, ISO800 68.3, ISO1250 57.0, ISO1600 65.9,
#   P1037920 30.6, ISO51200 39.2 +3EV ss2).

NLQ_SLACK_MUL = 1.2
NLQ_SLACK_ADD = 0.03
NLQ_FLOOR_MARGIN_MAX_F = 0.5
"""``ss2 >= FLOOR - 3`` is only meaningful for near-transparent steps (preset high)."""

HALF3_ANCHORS: dict[float, dict[str, float]] = {
    # d: criteria (ss2_min@EV, ba_p3_max@+3EV, ba_max_max@+3EV)
    0.1: {"ss2@0": 84.0, "ss2@2": 82.0, "ss2@3": 78.0, "ba_p3@3": 1.2, "ba_max@3": 6.0},
    0.2: {"ss2@0": 80.0, "ss2@2": 77.0, "ss2@3": 72.0, "ba_p3@3": 1.5, "ba_max@3": 8.0},
    0.3: {"ss2@0": 78.0, "ss2@2": 73.0, "ss2@3": 67.0, "ba_p3@3": 1.8, "ba_max@3": 9.0},
}
"""half3 limits at the calibration points (see the table above)."""
HALF3_D_RANGE = (0.05, 0.6)


def nlq_criteria(f: float) -> dict[str, Any]:
    """Noise-relative nlq criteria for step ``f`` (in units of sigma); see the comment table."""
    f = float(f)
    fe = NLQ_SLACK_MUL * f
    c: dict[str, Any] = {
        "noise_ratio_max": round(math.sqrt(1.0 + fe * fe / 12.0) + NLQ_SLACK_ADD, 4),
        "bias8_abs_max": round(0.75 + 0.25 * f * f, 4),
        "rmse_sigma_max": round(fe / math.sqrt(12.0) + NLQ_SLACK_ADD, 4),
    }
    if f <= NLQ_FLOOR_MARGIN_MAX_F:
        c["ss2_floor_margin"] = 3.0
    return c


def _interp_anchor(d: float, key: str) -> float:
    """Piecewise-linear in d through :data:`HALF3_ANCHORS`, outer segments extended."""
    ds = sorted(HALF3_ANCHORS)
    d = min(max(float(d), HALF3_D_RANGE[0]), HALF3_D_RANGE[1])
    i = int(np.clip(np.searchsorted(ds, d) - 1, 0, len(ds) - 2))
    lo, hi = ds[i], ds[i + 1]
    a, b = HALF3_ANCHORS[lo][key], HALF3_ANCHORS[hi][key]
    return a + (b - a) * (d - lo) / (hi - lo)


def half3_criteria(d: float) -> dict[str, Any]:
    """Perceptual half3 criteria for JXL distance ``d`` (interpolated from :data:`HALF3_ANCHORS`)."""
    v = {k: round(_interp_anchor(d, k), 3) for k in HALF3_ANCHORS[0.2]}
    return {
        "ss2_min": {0.0: v["ss2@0"], 2.0: v["ss2@2"], 3.0: v["ss2@3"]},
        "ba_p3_max": {3.0: v["ba_p3@3"]},
        "ba_max_max": {3.0: v["ba_max@3"]},
        "clip_inconsistent_max": 0,
    }


PRESET_PARAMS: dict[str, dict[str, float]] = {
    "high": {"d": 0.1, "f": 0.5},
    "vl": {"d": 0.2, "f": 1.0},
    "compact": {"d": 0.3, "f": 2.0},
}
"""Nominal (d, f) of the lossy presets (presets.PRESETS), used when HEAD has no parameter."""

ACCEPTANCE: dict[tuple[str, str], dict[str, Any]] = {
    ("lossless", "*"): {"mosaic_equal": True},
    ("archival", "*"): {"mosaic_equal": True},
    **{(p, "half3"): half3_criteria(v["d"]) for p, v in PRESET_PARAMS.items()},
    **{(p, "nlq"): nlq_criteria(v["f"]) for p, v in PRESET_PARAMS.items()},
}
"""(preset, engine) -> criteria at the preset's nominal parameter (reference table; the
check itself uses :func:`acceptance_criteria` with the file's actual d / f).  Keys:
``mosaic_equal``; ``ss2_min`` / ``ba_p3_max`` / ``ba_max_max`` ({ev: threshold}, worst
tile); ``clip_inconsistent_max``; ``noise_ratio_max``, ``bias8_abs_max`` (+3EV, worst of
centre/darkest tile); ``rmse_sigma_max`` (raw domain, worst position); ``ss2_floor_margin``
(ss2 >= FLOOR - margin at every EV; applied at low ISO only, i.e. ``noise.snr18 >= 40`` or
ISO <= 800)."""

LOW_ISO_SNR18 = 40.0
LOW_ISO_MAX = 800


def codec_param(engine: str | None, codec: Mapping[str, Any] | None) -> float | None:
    """The quality parameter recorded in HEAD ``codec`` (nlq: f, half3/gat4: d), or None."""
    if not engine or not isinstance(codec, Mapping):
        return None
    blk = codec.get(engine)
    if not isinstance(blk, Mapping):
        return None
    v = blk.get("f" if engine == "nlq" else "d")
    return float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else None


def acceptance_criteria(
    preset: str | None, engine: str | None, mode: str | None = None, param: float | None = None
) -> dict[str, Any] | None:
    """Criteria for ``engine`` at quality ``param`` (nlq f / half3 d; default: the preset's).

    ``mode == 'lossless'``, engine 'lossless' or preset lossless/archival -> bit-exactness.
    Returns None when nothing applies (gat4, unknown preset without a parameter).
    """
    if mode == "lossless" or engine == "lossless" or preset in ("lossless", "archival"):
        return dict(ACCEPTANCE[("lossless", "*")])
    if engine == "nlq" and param is not None and param == 0:
        return dict(ACCEPTANCE[("lossless", "*")])
    if engine not in ("nlq", "half3"):
        return None
    if param is None:
        nominal = PRESET_PARAMS.get(preset or "")
        if nominal is None:
            return None
        param = nominal["f" if engine == "nlq" else "d"]
    return nlq_criteria(param) if engine == "nlq" else half3_criteria(param)


# ---------------------------------------------------------------------------------------
# report


def _jsonable(o: Any) -> Any:
    if isinstance(o, float):
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        if math.isnan(o):
            return None
        return o
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return _jsonable(float(o))
    if isinstance(o, np.bool_):
        return bool(o)
    return o


@dataclass
class VerifyReport:
    engine: str | None
    preset: str | None
    mode: str | None
    params: dict[str, Any]
    evs: list[float]
    tiles: list[TileRect]
    worst: dict[float, TileMetrics]
    """Per EV: worst value over tiles for each metric (min ss2/psnr, max ba)."""
    per_tile: list[TileMetrics]
    floor: dict[float, TileMetrics] | None
    noise: dict[str, Any]
    clip: dict[str, int]
    mosaic_equal: bool
    sizes: dict[str, Any] = field(default_factory=dict)
    """Filled by callers: bytes, ratio_file, ratio_raw, enc_s, dec_s."""
    timings: dict[str, float] = field(default_factory=dict)
    acceptance: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.acceptance.get("passed", True))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["worst"] = {str(k): asdict(v) for k, v in self.worst.items()}
        d["floor"] = {str(k): asdict(v) for k, v in self.floor.items()} if self.floor else None
        return _jsonable(d)

    def to_json(self, indent: int | None = 1) -> str:
        """JSON (``inf`` is encoded as the string ``"inf"``)."""
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False)


def _worst(ms: Sequence[TileMetrics], ev: float) -> TileMetrics:
    sel = [m for m in ms if m.ev == ev]
    w = TileMetrics(tile="worst", ev=ev)

    def agg(attr: str, fn: Callable[[Sequence[float]], float]) -> tuple[float | None, str | None]:
        vals = [(getattr(m, attr), m.tile) for m in sel if getattr(m, attr) is not None]
        if not vals:
            return None, None
        v = fn([x for x, _ in vals])
        return v, next(t for x, t in vals if x == v)

    w.psnr, _ = agg("psnr", min)
    w.ss2, t = agg("ss2", min)
    w.ss2_ds2, _ = agg("ss2_ds2", min)
    w.ba_max, _ = agg("ba_max", max)
    w.ba_p3, _ = agg("ba_p3", max)
    if t:
        w.tile = f"worst:{t}"
    return w


def validate_metrics(metrics: Iterable[str]) -> tuple[str, ...]:
    """Check metric names against :data:`KNOWN_METRICS`; returns them as a tuple.

    Raises ``ValueError`` for an unknown name (a typo would otherwise measure nothing and
    pass) or an empty list.
    """
    ms = tuple(m.strip() for m in metrics if m and m.strip())
    if not ms:
        raise ValueError(f"no metrics given; expected some of {', '.join(KNOWN_METRICS)}")
    bad = [m for m in ms if m not in KNOWN_METRICS]
    if bad:
        import difflib

        hints = [f"{m!r} (did you mean {difflib.get_close_matches(m, KNOWN_METRICS, 1, 0.4)[0]!r}?)"
                 if difflib.get_close_matches(m, KNOWN_METRICS, 1, 0.4) else repr(m) for m in bad]
        raise ValueError(f"unknown metric {', '.join(hints)}; expected some of {', '.join(KNOWN_METRICS)}")
    return ms


def check_acceptance(report: VerifyReport, criteria: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate DESIGN.md 6.4 criteria; returns ``{"criteria", "passed", "failures", "skipped"}``.

    A criterion is *skipped* only when the caller did not ask for it (its EV is not in
    ``report.evs`` or its metric is not in ``report.params["metrics"]``, e.g. the 0EV ss2 of
    the quick verify).  A requested criterion that could not be measured (tool missing, no
    centre/darkest tile, ...) is a *failure*: the result is inconclusive and must not pass.
    """
    if criteria is None:
        criteria = acceptance_criteria(report.preset, report.engine, report.mode,
                                       codec_param(report.engine, report.params.get("codec")))
    if criteria is None:
        return {"criteria": None, "passed": True, "failures": [], "skipped": ["no criteria for preset/engine"]}
    fails: list[str] = []
    skipped: list[str] = []
    asked = set(report.params.get("metrics") or DEFAULT_METRICS)
    evs = {float(e) for e in report.evs}
    tool = {"ssimulacra2": "ssimulacra2", "butteraugli": "butteraugli_main"}
    if criteria.get("mosaic_equal") and not report.mosaic_equal:
        fails.append("mosaic not bit-exact")
    for key, metric, attr, label in (
        ("ss2_min", "ssimulacra2", "ss2", "ss2"),
        ("ba_p3_max", "butteraugli", "ba_p3", "ba_p3"),
        ("ba_max_max", "butteraugli", "ba_max", "ba_max"),
    ):
        for ev, thr in (criteria.get(key) or {}).items():
            ev = float(ev)
            if ev not in evs or metric not in asked:
                skipped.append(f"{label}@{ev:+g}EV not requested")
                continue
            w = report.worst.get(ev)
            v = None if w is None else getattr(w, attr)
            if v is None:
                why = f" ({tool[metric]} not found)" if not have(tool[metric]) else ""
                fails.append(f"{label}@{ev:+g}EV not measured{why}")
            elif key == "ss2_min" and v < thr:
                fails.append(f"{label}@{ev:+g}EV {v:.2f} < {thr}")
            elif key in ("ba_p3_max", "ba_max_max") and v > thr:
                fails.append(f"{label}@{ev:+g}EV {v:.3f} > {thr}")
    if "clip_inconsistent_max" in criteria and report.clip.get("inconsistent", 0) > criteria["clip_inconsistent_max"]:
        fails.append(f"clip inconsistent pixels {report.clip['inconsistent']}")
    nz = report.noise or {}
    for key, metric, label in (
        ("noise_ratio_max", "noise_ratio_max", "noise std ratio"),
        ("rmse_sigma_max", "rmse_sigma_max", "raw RMSE/sigma"),
        ("bias8_abs_max", "bias8_abs_max", "|bias8|"),
    ):
        if key not in criteria:
            continue
        if "noise" not in asked:
            skipped.append(f"{label} not requested")
            continue
        v = nz.get(metric)
        if v is None:
            fails.append(f"{label} not measured")
        elif v > criteria[key]:
            fails.append(f"{label} {v:.4f} > {criteria[key]}")
    if "ss2_floor_margin" in criteria:
        if not report.params.get("low_iso", False):
            skipped.append("ss2 >= FLOOR - margin applies at low ISO only")
        elif not report.floor or "ssimulacra2" not in asked:
            skipped.append("FLOOR not requested")
        else:
            for ev, fm in report.floor.items():
                w = report.worst.get(ev)
                if w is None or w.ss2 is None or fm.ss2 is None:
                    fails.append(f"ss2/FLOOR@{ev:+g}EV not measured")
                elif w.ss2 < fm.ss2 - criteria["ss2_floor_margin"]:
                    fails.append(f"ss2@{ev:+g}EV {w.ss2:.2f} < FLOOR {fm.ss2:.2f} - {criteria['ss2_floor_margin']}")
    return {"criteria": _jsonable(dict(criteria)), "passed": not fails, "failures": fails, "skipped": skipped}


# ---------------------------------------------------------------------------------------
# main entry


def _is_low_iso(head: Mapping[str, Any]) -> bool:
    noise = head.get("noise") or {}
    snr = noise.get("snr18")
    if isinstance(snr, (int, float)):
        return float(snr) >= LOW_ISO_SNR18
    iso = noise.get("iso", (head.get("source") or {}).get("iso"))
    return isinstance(iso, (int, float)) and iso <= LOW_ISO_MAX


def verify(
    orig_mosaic: np.ndarray,
    rec_mosaic: np.ndarray,
    head: Mapping[str, Any],
    evs: Sequence[float] = DEFAULT_EVS,
    tiles: int = 4,
    *,
    full: bool = False,
    metrics: Sequence[str] = DEFAULT_METRICS,
    floor: bool = True,
    floor_metrics: Sequence[str] = FLOOR_METRICS,
    floor_seed: int = FLOOR_SEED,
    tile_size: int = TILE_SIZE,
    seed: int = 0,
    noise: Sequence[Any] | None = None,
    preset: str | None = None,
    engine: str | None = None,
    threads: int | None = None,
    workdir: str | os.PathLike[str] | None = None,
    ds2: bool = True,
) -> VerifyReport:
    """Compare a reconstructed mosaic against the original (DESIGN.md 6).

    ``head``: HEAD-like dict (levels, cfa, color, geometry; optional noise/engine/preset).
    ``tiles``: number of 2048^2 tiles (``full=True`` uses the whole DefaultCrop image).
    ``metrics``: subset of ``psnr, ssimulacra2, butteraugli, noise``.  ``floor``: also run the
    FLOOR control (``floor_metrics`` only).  Tools run in a thread pool of ``threads``.
    Returns a :class:`VerifyReport` with ``acceptance`` filled from :data:`ACCEPTANCE`.
    """
    t0 = time.perf_counter()
    if orig_mosaic.shape != rec_mosaic.shape:
        raise ValueError(f"shape mismatch {orig_mosaic.shape} vs {rec_mosaic.shape}")
    evs = [float(e) for e in evs]
    if not evs:
        raise ValueError("verify needs at least one EV")
    if not full and int(tiles) < 1:
        raise ValueError(f"tiles must be >= 1, got {tiles}")
    metrics = validate_metrics(metrics)
    engine = engine or head.get("engine")
    preset = preset or head.get("preset")
    mode = head.get("mode")
    warnings: list[str] = []
    nt = threads or os.cpu_count() or 1
    equal = bool(np.array_equal(orig_mosaic, rec_mosaic))

    mos: dict[str, np.ndarray] = {"orig": orig_mosaic}
    if not equal:
        mos["rec"] = rec_mosaic
    if floor:
        mos["floor"] = floor_control(orig_mosaic, floor_seed)
    noise_evs = sorted(set(evs) | ({3.0} if "noise" in metrics else set()))
    t1 = time.perf_counter()
    imgs = develop_many(mos, head, noise_evs, threads=nt)
    if equal:
        for ev in noise_evs:
            imgs[("rec", ev)] = imgs[("orig", ev)]
    t_dev = time.perf_counter() - t1

    ref0 = imgs[("orig", noise_evs[0] if 0.0 not in noise_evs else 0.0)]
    rects = [TileRect("full", 0, 0, ref0.shape[0], ref0.shape[1])] if full else pick_tiles(ref0, tiles, seed, tile_size)

    jobs: list[tuple[str, str, float, TileRect, tuple[str, ...]]] = []
    for ev in evs:
        for r in rects:
            jobs.append(("rec", r.name, ev, r, tuple(metrics)))
            if floor:
                jobs.append(("floor", r.name, ev, r, tuple(floor_metrics)))
    if not have("ssimulacra2") and "ssimulacra2" in metrics:
        warnings.append("ssimulacra2 not found; ss2 not measured")
    if not have("butteraugli_main") and "butteraugli" in metrics:
        warnings.append("butteraugli_main not found; butteraugli not measured")

    def work(j: tuple[str, str, float, TileRect, tuple[str, ...]]) -> TileMetrics:
        kind, name, ev, r, ms = j
        a = r.slice(imgs[("orig", ev)])
        b = r.slice(imgs[(kind, ev)])
        res = image_metrics(a, b, ms, workdir=workdir, tag=f"{kind}_{name}_{ev:g}", ds2=ds2)
        return TileMetrics(tile=name, ev=ev, **res)

    t2 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, min(nt, len(jobs) or 1))) as ex:
        results = list(ex.map(work, jobs))
    t_met = time.perf_counter() - t2
    per_tile = [m for j, m in zip(jobs, results) if j[0] == "rec"]
    floor_tiles = [m for j, m in zip(jobs, results) if j[0] == "floor"]
    worst = {ev: _worst(per_tile, ev) for ev in evs}
    floor_worst = {ev: _worst(floor_tiles, ev) for ev in evs} if floor else None

    noise_res: dict[str, Any] = {}
    if "noise" in metrics:
        t3 = time.perf_counter()
        noise_res["raw"] = raw_noise_metrics(orig_mosaic, rec_mosaic, head, noise=noise)
        noise_res["rmse_sigma_max"] = noise_res["raw"]["rmse_sigma_max"]
        a3, b3 = imgs[("orig", 3.0)], imgs[("rec", 3.0)]
        # bias / noise ratio on centre and darkest tiles (2048^2 tiles; full image if single)
        sel = [r for r in (rects if not full else pick_tiles(a3, 2, seed, tile_size)) if r.name in ("center", "darkest", "full")]
        per: dict[str, dict[str, float | None]] = {}
        for r in sel:
            A, Bm = r.slice(a3), r.slice(b3)
            per[r.name] = {"bias8": bias8(A, Bm), "noise_ratio": noise_std_ratio(A, Bm)}
        noise_res["ev3"] = per
        bs = [v["bias8"] for v in per.values() if v["bias8"] is not None]
        nr = [v["noise_ratio"] for v in per.values() if v["noise_ratio"] is not None]
        noise_res["bias8_abs_max"] = max((abs(x) for x in bs), default=None)
        noise_res["noise_ratio_max"] = max(nr, default=None)
        noise_res["t"] = round(time.perf_counter() - t3, 3)

    white = int(head["levels"]["white"])
    clip = clip_consistency(orig_mosaic, rec_mosaic, white)
    params: dict[str, Any] = {
        "tile_size": tile_size,
        "n_tiles": len(rects),
        "seed": seed,
        "floor_seed": floor_seed if floor else None,
        "metrics": list(metrics),
        "low_iso": _is_low_iso(head),
        "codec": head.get("codec"),
    }
    rep = VerifyReport(
        engine=engine,
        preset=preset,
        mode=mode,
        params=params,
        evs=evs,
        tiles=rects,
        worst=worst,
        per_tile=per_tile,
        floor=floor_worst,
        noise=noise_res,
        clip=clip,
        mosaic_equal=equal,
        timings={"develop": round(t_dev, 3), "metrics": round(t_met, 3), "total": round(time.perf_counter() - t0, 3)},
        warnings=warnings,
    )
    rep.acceptance = check_acceptance(rep)
    return rep


def format_report(rep: VerifyReport) -> str:
    """Human-readable multi-line summary."""

    def f(v: float | None, p: int = 2) -> str:
        if v is None:
            return "-"
        if isinstance(v, float) and math.isinf(v):
            return "inf"
        return f"{v:.{p}f}"

    lines = [f"engine={rep.engine} preset={rep.preset} mode={rep.mode} tiles={[t.name for t in rep.tiles]} equal={rep.mosaic_equal}"]
    for ev in rep.evs:
        w = rep.worst[ev]
        s = f"{ev:+g}EV ss2 {f(w.ss2)} ds2 {f(w.ss2_ds2)} ba {f(w.ba_max)}/{f(w.ba_p3, 3)} psnr {f(w.psnr)}"
        if rep.floor:
            s += f" | FLOOR ss2 {f(rep.floor[ev].ss2)} psnr {f(rep.floor[ev].psnr)}"
        lines.append(s)
    if rep.noise:
        lines.append(
            f"noise: rmse/sigma max {f(rep.noise.get('rmse_sigma_max'), 3)} |bias8| {f(rep.noise.get('bias8_abs_max'), 3)} "
            f"noise ratio {f(rep.noise.get('noise_ratio_max'), 4)}"
        )
    lines.append(f"clip: {rep.clip}")
    acc = rep.acceptance
    lines.append(f"acceptance: {'PASS' if acc.get('passed', True) else 'FAIL'} {acc.get('failures', [])}")
    return "\n".join(lines)


def save_report(rep: VerifyReport, path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.write_text(rep.to_json() + "\n")
    return p


__all__ = [
    "ACCEPTANCE",
    "DEFAULT_EVS",
    "DEFAULT_METRICS",
    "FLOOR_SEED",
    "KNOWN_METRICS",
    "TileMetrics",
    "TileRect",
    "VerifyReport",
    "acceptance_criteria",
    "codec_param",
    "half3_criteria",
    "nlq_criteria",
    "bias8",
    "butteraugli",
    "check_acceptance",
    "clip_consistency",
    "develop",
    "develop_buf",
    "develop_many",
    "downsample2_linear",
    "floor_control",
    "format_report",
    "image_metrics",
    "libraw_eotf",
    "libraw_oetf",
    "noise_std_ratio",
    "parse_butteraugli",
    "parse_ssimulacra2",
    "pick_tiles",
    "psnr16",
    "raw_noise_metrics",
    "save_report",
    "ssimulacra2",
    "validate_metrics",
    "verify",
    "write_ppm16",
]
