"""Poisson-Gaussian noise model estimation per CFA plane (DESIGN.md section 2.3, appendix A.3).

Model: ``var(x) = g * x + s2`` with ``x = raw - black`` in DN (``g`` in DN, ``s2`` in DN^2).

Estimator (``estimate_noise2`` of the noise-adaptive-quantizer design):

1. residual ``r = (x - mean of the 4 same-colour neighbours) / sqrt(1.25)`` (unit gain for
   white noise);
2. 8x8 blocks of the plane: block mean and robust block sigma ``1.4826 * MAD(r)``; blocks
   touching 0 or 0.95*X (``X = white - black``) are dropped;
3. equal-population bins over block mean (>= 50 blocks per bin); per bin
   ``var = P5(sigma)^2 / cf`` where ``cf`` is the same statistic measured on pure N(0,1)
   (seed 0, 1024^2, cached) -- the percentile bias correction;
4. weighted least squares ``var = g*mean + s2`` starting from the lower envelope (lowest 40%
   of ``var/(mean+20)``), 4 iterations dropping bins with ``var > 1.3*pred``; clamp
   ``g >= 1e-4``, ``s2 >= 0.25``;
5. optional ISO cap ``g_used = min(g_est, k_cam[p] * ISO)`` (``noise_table``).

If fewer than 4 bins are valid (very dark/bright frames) the plane falls back to the ISO
prior (camera table, else ``1e-4 * ISO``) and is flagged ``fallback``.
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, overload

import numpy as np

from . import noise_table
from .cfa import split_planes

if TYPE_CHECKING:
    from .rawio import RawFrame

G_MIN = 1e-4
S2_MIN = 0.25
MIN_VALID_BINS = 4
DEFAULT_ISO = 100.0
"""ISO assumed for the fallback prior when the frame has no ISO."""

NOISE_MODELS = ("auto", "auto+iso_cap", "manual")


@dataclass
class NoiseParams:
    """Noise parameters of one CFA plane (position order).

    ``g`` is the value actually used (after the ISO cap); ``g_est`` the raw estimate.
    """

    g: float
    s2: float
    g_est: float | None = None
    capped: bool = False
    fallback: bool = False
    nbins: int = 0
    """Number of valid bins in the fit (0 for fallback / manual)."""

    def to_head(self) -> dict[str, Any]:
        """HEAD ``noise.planes[i]`` record."""
        d: dict[str, Any] = {
            "g_est": float(self.g_est if self.g_est is not None else self.g),
            "g_used": float(self.g),
            "s2": float(self.s2),
        }
        if self.capped:
            d["capped"] = True
        if self.fallback:
            d["fallback"] = True
        return d

    def sigma(self, x: np.ndarray | float) -> np.ndarray | float:
        """Model noise std at signal level ``x`` (DN above black)."""
        return np.sqrt(np.maximum(self.g * np.asarray(x, dtype=np.float64) + self.s2, 0.0))


@dataclass
class NoiseEstimate(Sequence[NoiseParams]):
    """Per-position noise parameters plus provenance; behaves like ``list[NoiseParams]``."""

    planes: list[NoiseParams]
    model: str = "auto"
    """Noise model actually applied: 'auto' | 'auto+iso_cap' | 'manual'."""
    iso: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @overload
    def __getitem__(self, i: int) -> NoiseParams: ...
    @overload
    def __getitem__(self, i: slice) -> list[NoiseParams]: ...
    def __getitem__(self, i: int | slice) -> NoiseParams | list[NoiseParams]:
        return self.planes[i]

    def __len__(self) -> int:
        return len(self.planes)

    def __iter__(self) -> Iterator[NoiseParams]:
        return iter(self.planes)

    @property
    def fallback(self) -> bool:
        return any(p.fallback for p in self.planes)

    def to_head(self, snr18: float | None = None) -> dict[str, Any]:
        """HEAD ``noise`` section (DESIGN.md 3.3)."""
        d: dict[str, Any] = {"model": self.model, "planes": [p.to_head() for p in self.planes]}
        if snr18 is not None:
            d["snr18"] = float(snr18)
        d["iso"] = _json_num(self.iso)
        if self.fallback:
            d["fallback"] = True
        d.update(self.extra)
        return d


def _json_num(v: float | None) -> float | int | None:
    if v is None:
        return None
    fv = float(v)
    return int(fv) if fv.is_integer() else fv


# ---------------------------------------------------------------------------------------
# core statistics


def block_sigma(x: np.ndarray, block: int = 8) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-block (mean, robust sigma, min, max) of plane ``x`` (float32, black-subtracted).

    Uses the 4-neighbour Laplacian residual ``r = (x - mean4) / sqrt(1.25)`` on the
    interior ``x[1:-1, 1:-1]``; blocks are ``block x block`` tiles of that interior.
    """
    x = np.asarray(x, dtype=np.float32)
    xm = x[1:-1, 1:-1]
    r = xm - 0.25 * (x[:-2, 1:-1] + x[2:, 1:-1] + x[1:-1, :-2] + x[1:-1, 2:])
    r *= np.float32(1.0 / math.sqrt(1.25))
    h = (r.shape[0] // block) * block
    w = (r.shape[1] // block) * block
    if h == 0 or w == 0:
        e = np.empty(0, np.float32)
        return e, e, e, e
    nb = block * block
    rb = r[:h, :w].reshape(h // block, block, w // block, block).transpose(0, 2, 1, 3).reshape(-1, nb)
    mb = xm[:h, :w].reshape(h // block, block, w // block, block).transpose(0, 2, 1, 3).reshape(-1, nb)
    med = np.median(rb, axis=1, keepdims=True)
    sig = np.float32(1.4826) * np.median(np.abs(rb - med), axis=1)
    return mb.mean(axis=1), sig, mb.min(axis=1), mb.max(axis=1)


_CF_CACHE: dict[tuple[int, float], float] = {}
_CF_LOCK = threading.Lock()


def percentile_bias(block: int = 8, pct: float = 5.0) -> float:
    """``cf = P_pct(block sigma)^2`` on pure N(0,1) noise (seed 0, 1024^2); cached."""
    key = (int(block), float(pct))
    with _CF_LOCK:
        cf = _CF_CACHE.get(key)
        if cf is None:
            z = np.random.default_rng(0).standard_normal((1024, 1024)).astype(np.float32)
            _, s, _, _ = block_sigma(z, block)
            cf = float(np.percentile(s, pct)) ** 2
            _CF_CACHE[key] = cf
    return cf


def _wls_fit(ms: np.ndarray, vs: np.ndarray) -> tuple[float, float]:
    """Lower-envelope initialised, iteratively reweighted LS fit of ``vs = g*ms + s2``."""
    A = np.stack([ms, np.ones_like(ms)], 1)
    ratio = vs / (ms + 20.0)
    w = np.where(ratio <= np.quantile(ratio, 0.4), 1.0 / np.maximum(vs, 0.5), 0.0)
    sol = None
    for _ in range(4):
        if np.count_nonzero(w) < 2:
            break
        sol, *_ = np.linalg.lstsq(A * w[:, None], vs * w, rcond=None)
        pred = A @ sol
        w = np.where(vs > 1.3 * np.maximum(pred, 0.5), 0.0, 1.0 / np.maximum(pred, 0.5))
    if sol is None:  # degenerate: plain LS through all bins
        sol, *_ = np.linalg.lstsq(A, vs, rcond=None)
    return float(sol[0]), float(sol[1])


def _fallback_s2(x: np.ndarray, g: float) -> float:
    """Read-noise estimate for frames where the binned fit is impossible.

    Whole-plane robust residual variance minus the shot-noise part at the median level;
    biased low (safe: a smaller s2 means a larger lossless region / finer steps).
    """
    if x.shape[0] < 3 or x.shape[1] < 3:
        return S2_MIN
    xm = x[1:-1, 1:-1]
    r = (xm - 0.25 * (x[:-2, 1:-1] + x[2:, 1:-1] + x[1:-1, :-2] + x[1:-1, 2:])) / math.sqrt(1.25)
    sig = 1.4826 * float(np.median(np.abs(r - np.median(r))))
    v = sig * sig - g * max(float(np.median(xm)), 0.0)
    return float(max(v, S2_MIN))


def estimate_noise(
    plane: np.ndarray,
    black: float,
    white: float,
    *,
    block: int = 8,
    pct: float = 5.0,
    nbins: int = 48,
    min_per_bin: int = 50,
    fallback_g: float | None = None,
) -> NoiseParams:
    """Estimate ``(g, s2)`` of one CFA plane (raw DN, black NOT subtracted).

    ``nbins`` is reduced to ``n_blocks // min_per_bin`` on small planes so every bin keeps
    >= ``min_per_bin`` blocks (identical to the reference on full frames).  With fewer
    than 4 valid bins, returns ``fallback_g`` (default ``1e-4 * 100``) and a robust read
    noise estimate, flagged ``fallback=True``.
    """
    x = np.asarray(plane, dtype=np.float32) - np.float32(black)
    X = float(white) - float(black)
    if X <= 0:
        raise ValueError(f"white ({white}) must exceed black ({black})")

    ms = vs = np.empty(0)
    mean, sig, bmin, bmax = block_sigma(x, block)
    ok = (bmax < 0.95 * X) & (bmin > 0)
    mean = mean[ok].astype(np.float64)
    sig = sig[ok].astype(np.float64)
    nb = min(int(nbins), mean.size // int(min_per_bin))
    if nb >= MIN_VALID_BINS:
        cf = percentile_bias(block, pct)
        edges = np.quantile(mean, np.linspace(0.0, 1.0, nb + 1))
        # bin index per block; last edge inclusive (reference excluded the maximum)
        idx = np.clip(np.searchsorted(edges, mean, side="right") - 1, 0, nb - 1)
        order = np.argsort(idx, kind="stable")
        counts = np.bincount(idx, minlength=nb)
        starts = np.concatenate([[0], np.cumsum(counts)])
        mlist: list[float] = []
        vlist: list[float] = []
        for b in range(nb):
            if counts[b] < min_per_bin:
                continue
            sel = order[starts[b] : starts[b + 1]]
            mlist.append(float(np.median(mean[sel])))
            vlist.append(float(np.percentile(sig[sel], pct)) ** 2 / cf)
        ms, vs = np.asarray(mlist), np.asarray(vlist)

    if ms.size < MIN_VALID_BINS:
        g = float(fallback_g) if fallback_g is not None else noise_table.DEFAULT_G_PER_ISO * DEFAULT_ISO
        return NoiseParams(g=g, s2=_fallback_s2(x, g), g_est=g, fallback=True, nbins=int(ms.size))

    g, s2 = _wls_fit(ms, vs)
    g = max(g, G_MIN)
    s2 = max(s2, S2_MIN)
    return NoiseParams(g=g, s2=s2, g_est=g, nbins=int(ms.size))


# ---------------------------------------------------------------------------------------
# ISO prior / cap


def iso_prior(make: str | None, model: str | None, iso: float | None) -> list[float] | None:
    """Per-position ``k_cam[p] * ISO`` if the camera is in the table and ISO is known."""
    k = noise_table.lookup_k(make, model)
    if k is None or iso is None or not iso > 0:
        return None
    return [float(kp) * float(iso) for kp in k]


def apply_iso_cap(
    params: Sequence[NoiseParams], make: str | None, model: str | None, iso: float | None
) -> list[NoiseParams]:
    """``g_used = min(g_est, k_cam[p]*ISO)``; unchanged copies when no table entry / ISO."""
    cap = iso_prior(make, model, iso)
    out: list[NoiseParams] = []
    for p, prm in enumerate(params):
        g_est = prm.g_est if prm.g_est is not None else prm.g
        if cap is not None and g_est > cap[p]:
            out.append(replace(prm, g=max(cap[p], G_MIN), g_est=g_est, capped=True))
        else:
            out.append(replace(prm, g_est=g_est))
    return out


def parse_noise_model(spec: str) -> tuple[str, tuple[float, float] | None]:
    """Parse ``auto`` | ``auto+iso_cap`` | ``manual:G,S2`` -> (kind, (g, s2) or None)."""
    s = spec.strip()
    if s in ("auto", "auto+iso_cap"):
        return s, None
    if s.startswith("manual:"):
        try:
            g_s, s2_s = s[len("manual:") :].split(",")
            g, s2 = float(g_s), float(s2_s)
        except ValueError:
            raise ValueError(f"bad noise model {spec!r}; expected manual:G,S2") from None
        if not (g > 0 and s2 >= 0 and math.isfinite(g) and math.isfinite(s2)):
            raise ValueError(f"bad manual noise parameters in {spec!r}")
        return "manual", (g, s2)
    raise ValueError(f"unknown noise model {spec!r}; expected auto, auto+iso_cap or manual:G,S2")


def estimate_all(
    frame: RawFrame,
    threads: int | None = None,
    *,
    noise_model: str = "auto+iso_cap",
    block: int = 8,
    pct: float = 5.0,
    nbins: int = 48,
) -> NoiseEstimate:
    """Estimate the noise model of all 4 CFA planes (position order) of an even-sized frame.

    ``noise_model``: 'auto' (estimator only), 'auto+iso_cap' (cap with the camera table;
    silently 'auto' if the camera/ISO is unknown) or 'manual:G,S2' (same values for all
    planes, no estimation).  Planes run in a thread pool of ``min(4, threads)``.
    """
    kind, manual = parse_noise_model(noise_model)
    iso = float(frame.iso) if frame.iso is not None else None
    if kind == "manual":
        assert manual is not None
        g, s2 = manual
        return NoiseEstimate([NoiseParams(g=g, s2=max(s2, 0.0), g_est=g) for _ in range(4)], "manual", iso)

    planes = split_planes(frame.mosaic)
    blk = [float(b) for b in frame.black_per_position]
    prior = iso_prior(frame.make, frame.model, iso)
    if prior is None:
        g0 = noise_table.DEFAULT_G_PER_ISO * (iso if iso and iso > 0 else DEFAULT_ISO)
        prior = [g0] * 4

    def one(k: int) -> NoiseParams:
        return estimate_noise(
            planes[k], blk[k], frame.white, block=block, pct=pct, nbins=nbins, fallback_g=prior[k]
        )

    nt = max(1, min(4, threads if threads is not None else (os.cpu_count() or 1)))
    if nt == 1:
        est = [one(k) for k in range(4)]
    else:
        with ThreadPoolExecutor(nt) as ex:
            est = list(ex.map(one, range(4)))

    used = "auto"
    if kind == "auto+iso_cap" and iso_prior(frame.make, frame.model, iso) is not None:
        est = apply_iso_cap(est, frame.make, frame.model, iso)
        used = "auto+iso_cap"
    return NoiseEstimate(est, used, iso)


def as_noise_params(noise: Sequence[Any]) -> list[NoiseParams]:
    """Coerce a sequence of objects with ``.g``/``.s2`` (or (g, s2) pairs) to NoiseParams."""
    out: list[NoiseParams] = []
    for n in noise:
        if isinstance(n, NoiseParams):
            out.append(n)
        elif hasattr(n, "g") and hasattr(n, "s2"):
            out.append(NoiseParams(g=float(n.g), s2=float(n.s2), g_est=getattr(n, "g_est", None)))
        else:
            g, s2 = n
            out.append(NoiseParams(g=float(g), s2=float(s2)))
    return out


__all__ = [
    "G_MIN",
    "NOISE_MODELS",
    "NoiseEstimate",
    "NoiseParams",
    "S2_MIN",
    "apply_iso_cap",
    "as_noise_params",
    "block_sigma",
    "estimate_all",
    "estimate_noise",
    "iso_prior",
    "parse_noise_model",
    "percentile_bias",
]
