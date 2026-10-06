"""Noise-adaptive companding curve and reconstruction LUTs for the nlq engine (DESIGN.md 2.2).

With noise model ``sigma(x)^2 = g*x + s2`` and step factor ``f``:

* ``x0 = max((T^2/f^2 - s2)/g, 0)``, ``c0 = sqrt(g*x0 + s2)`` with the identity step
  threshold ``T = identity_step_for(f)`` (DESIGN.md 2.2 has T = 1; see below)
* ``y(x) = x`` for ``x < x0`` (identity / lossless region; extends to negative x)
* ``y(x) = x0 + (2/(g*f)) * (sqrt(g*x + s2) - c0)`` for ``x >= x0``

so ``dy/dx = 1/(f*sigma(x))``: one integer code per ``f*sigma`` DN, identity where
``f*sigma < T``.  ``f == 0`` means lossless (identity everywhere).

Identity step (integrator change, measured): ``T = 2`` DN for ``f < 1``, ``T = 1`` (spec)
otherwise.  With steps of 1-2 DN a code bin holds one or two integer raw values, and any
integer reconstruction of a two-value bin has 0/1 DN errors (RMS 0.71 DN instead of
step/sqrt(12)) that all point the same way.  On P1037920 (ISO4000) nlq f0.5 this gave a
+3EV noise std ratio of 1.027 and |bias8| 0.27 (DESIGN.md 6.4 high/nlq: <= 1.02); with
T = 2: 1.009 and 0.045 for +0.5 % size.  For f >= 1 the spec rule is kept so the validated
vl/compact results are unchanged (f >= 2 is unaffected either way).  The decoder does not
depend on T (the LUT is stored); ``codec.nlq.identity_step`` records it.

Integer codes: ``q = rint(y(x)) + offset`` where ``offset = max(0, -min(x))`` makes codes
non-negative for sensors that output values below black; the saturation code
``q_sat = rint(y(X-1)) + offset + 1`` (``X = white - black``) is reserved for raw >= white
and always reconstructs to ``white``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

RECON_MODES = ("mid", "centroid")


IDENTITY_STEP_DN = 2.0
"""Identity step threshold (DN) used for ``f < 1``; the spec value 1 DN is used for f >= 1."""


def identity_step_for(f: float) -> float:
    """Quantisation steps ``f*sigma`` below this many DN are not quantised (see module doc)."""
    return IDENTITY_STEP_DN if 0 < f < 1 else 1.0


def curve_params(g: float, s2: float, f: float, identity_step: float | None = None) -> tuple[float, float]:
    """``(x0, c0)`` for the curve; ``x0 = inf`` when ``f == 0`` (lossless).

    ``x0 = max((T^2/f^2 - s2)/g, 0)`` with ``T = identity_step`` (default
    :func:`identity_step_for`): below ``x0`` the step ``f*sigma(x)`` would be under T DN.
    """
    if f < 0:
        raise ValueError(f"f must be >= 0, got {f}")
    if g <= 0:
        raise ValueError(f"g must be > 0, got {g}")
    s2 = max(float(s2), 0.0)
    if f == 0:
        return math.inf, math.inf
    t = identity_step_for(f) if identity_step is None else float(identity_step)
    x0 = max((t * t / (f * f) - s2) / g, 0.0)
    return x0, math.sqrt(g * x0 + s2)


def curve_fwd(x: np.ndarray | float, g: float, s2: float, f: float) -> np.ndarray:
    """Forward companding ``y(x)`` (float64); ``x`` = raw - black, may be negative."""
    x = np.asarray(x, dtype=np.float64)
    x0, c0 = curve_params(g, s2, f)
    if math.isinf(x0):
        return x.copy()
    s2 = max(float(s2), 0.0)
    hi = x >= x0
    y = x.copy()
    if np.any(hi):
        y[hi] = x0 + (2.0 / (g * f)) * (np.sqrt(g * x[hi] + s2) - c0)
    return y


def curve_inv(y: np.ndarray | float, g: float, s2: float, f: float) -> np.ndarray:
    """Inverse companding ``x(y)`` (float64)."""
    y = np.asarray(y, dtype=np.float64)
    x0, c0 = curve_params(g, s2, f)
    if math.isinf(x0):
        return y.copy()
    s2 = max(float(s2), 0.0)
    hi = y >= x0
    x = y.copy()
    if np.any(hi):
        t = (y[hi] - x0) * (g * f / 2.0) + c0
        x[hi] = (t * t - s2) / g
    return x


def saturation_code(g: float, s2: float, f: float, black: int, white: int, offset: int) -> int:
    """``q_sat = rint(y(X - 1)) + offset + 1`` with ``X = white - black``."""
    X = int(white) - int(black)
    return int(np.rint(curve_fwd(float(X - 1), g, s2, f))) + int(offset) + 1


@dataclass(frozen=True)
class PlaneQuantizer:
    """Quantizer for one CFA plane: integer forward table + LUT construction.

    ``fwd`` maps ``x + offset`` (``x`` = raw - black clipped to ``[-offset, X]``) to the code
    ``q``; ``fwd[X + offset] == q_sat``.  Equivalent to evaluating the curve per pixel, but
    exact and much faster (one table lookup per pixel).
    """

    g: float
    s2: float
    f: float
    black: int
    white: int
    offset: int
    q_sat: int
    fwd: np.ndarray  # int64, length X + offset + 1

    @classmethod
    def create(cls, g: float, s2: float, f: float, black: int, white: int, offset: int = 0) -> PlaneQuantizer:
        if f <= 0:
            raise ValueError("PlaneQuantizer is for lossy f > 0 (f == 0 is the raw lossless path)")
        black, white, offset = int(black), int(white), int(offset)
        X = white - black
        if X < 2:
            raise ValueError(f"white ({white}) must exceed black ({black}) by at least 2")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        xs = np.arange(-offset, X, dtype=np.float64)  # -offset .. X-1
        q = np.rint(curve_fwd(xs, g, s2, f)).astype(np.int64) + offset
        q_sat = int(q[-1]) + 1
        fwd = np.empty(X + offset + 1, dtype=np.int64)
        fwd[:-1] = q
        fwd[-1] = q_sat
        return cls(float(g), float(s2), float(f), black, white, offset, q_sat, fwd)

    @property
    def dtype(self) -> np.dtype:
        """uint8 if ``q_sat < 256`` else uint16."""
        return np.dtype(np.uint8) if self.q_sat < 256 else np.dtype(np.uint16)

    @property
    def x0(self) -> float:
        return curve_params(self.g, self.s2, self.f)[0]

    def quantize(self, plane: np.ndarray) -> np.ndarray:
        """Codes for a raw plane (uint16 DN, black not subtracted); raw >= white -> q_sat.

        Raises ValueError if the plane has values below ``black - offset``.
        """
        idx = plane.astype(np.int32)
        idx += self.offset - self.black
        lo = int(idx.min()) if idx.size else 0
        if lo < 0:
            raise ValueError(f"plane has values below black - offset ({lo - self.offset + self.black})")
        np.minimum(idx, self.fwd.size - 1, out=idx)
        table = self.fwd.astype(self.dtype)
        return table[idx]

    def bin_edges(self) -> tuple[np.ndarray, np.ndarray]:
        """Raw-value range ``[lo[k], hi[k]]`` (DN incl. black) of every code ``k < q_sat``.

        Codes with no integer raw value (impossible for slope <= 1) get lo > hi.
        """
        n = self.q_sat + 1
        codes = self.fwd[:-1]
        ks = np.arange(n)
        lo_i = np.searchsorted(codes, ks, side="left")
        hi_i = np.searchsorted(codes, ks, side="right") - 1
        base = self.black - self.offset
        return lo_i + base, hi_i + base

    def lut_mid(self) -> np.ndarray:
        """``LUT[k] = clip(rint(curve_inv(k - offset) + black), 0, white)``; ``LUT[q_sat] = white``.

        Additionally clamped into each code's raw range: only matters for the top code
        below ``q_sat`` (its range is truncated at ``white - 1``), so non-saturated pixels
        never reconstruct to ``white``.
        """
        k = np.arange(self.q_sat + 1, dtype=np.float64)
        mid = np.rint(curve_inv(k - self.offset, self.g, self.s2, self.f) + self.black)
        lo, hi = self.bin_edges()
        valid = lo <= hi
        mid = np.where(valid, np.clip(mid, lo, np.maximum(hi, lo)), mid)
        lut = np.clip(mid, 0, self.white).astype(np.uint16)
        lut[self.q_sat] = self.white
        return lut

    def lut_centroid(self, plane: np.ndarray, q: np.ndarray | None = None) -> np.ndarray:
        """``LUT[k] = rint(mean(plane[q == k]))``; empty bins -> mid; clamped into each bin's
        raw range (keeps the LUT monotone); ``LUT[q_sat] = white``."""
        if q is None:
            q = self.quantize(plane)
        n = self.q_sat + 1
        qr = q.ravel()
        cnt = np.bincount(qr, minlength=n)[:n]
        sums = np.bincount(qr, weights=plane.ravel().astype(np.float64), minlength=n)[:n]
        mid = self.lut_mid().astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            cen = np.where(cnt > 0, sums / np.maximum(cnt, 1), mid)
        lo, hi = self.bin_edges()
        valid = lo <= hi
        cen = np.where(valid, np.clip(cen, lo, np.maximum(hi, lo)), cen)
        lut = np.clip(np.rint(cen), 0, self.white).astype(np.uint16)
        lut[self.q_sat] = self.white
        return lut

    def lut(self, recon: str, plane: np.ndarray | None = None, q: np.ndarray | None = None) -> np.ndarray:
        """Reconstruction LUT for ``recon`` in {'mid', 'centroid'}."""
        if recon == "mid":
            return self.lut_mid()
        if recon == "centroid":
            if plane is None:
                raise ValueError("centroid LUT needs the original plane")
            return self.lut_centroid(plane, q)
        raise ValueError(f"unknown recon mode {recon!r}; expected one of {RECON_MODES}")

    def head_record(self) -> dict[str, Any]:
        """``codec.nlq.planes[i]`` HEAD record."""
        return {
            "g": float(self.g),
            "s2": float(self.s2),
            "offset": int(self.offset),
            "q_sat": int(self.q_sat),
            "dtype": self.dtype.name,
            "black": int(self.black),
        }


def build_lut(
    np_params: Any,
    f: float,
    black: int,
    white: int,
    offset: int,
    q_sat: int | None,
    recon: str,
    plane: np.ndarray | None = None,
    q: np.ndarray | None = None,
) -> np.ndarray:
    """Spec-signature wrapper (DESIGN.md 5.2): reconstruction LUT (uint16, length q_sat+1).

    ``np_params``: object with ``.g``/``.s2`` (NoiseParams) or a ``(g, s2)`` pair.
    ``q_sat`` is recomputed and checked when given.
    """
    if hasattr(np_params, "g"):
        g, s2 = float(np_params.g), float(np_params.s2)
    else:
        g, s2 = (float(v) for v in np_params)
    pq = PlaneQuantizer.create(g, s2, f, black, white, offset)
    if q_sat is not None and int(q_sat) != pq.q_sat:
        raise ValueError(f"q_sat mismatch: given {q_sat}, computed {pq.q_sat}")
    return pq.lut(recon, plane, q)


def resolve_recon(recon: str | None, f: float) -> str:
    """'auto' -> 'centroid' if ``f >= 2`` else 'mid' (DESIGN.md Q5)."""
    if recon in (None, "", "auto"):
        return "centroid" if f >= 2.0 else "mid"
    if recon not in RECON_MODES:
        raise ValueError(f"unknown recon mode {recon!r}; expected auto, mid or centroid")
    return recon


def added_noise_rms(f: float) -> float:
    """Relative extra noise RMS of the quantizer: ``sqrt(1 + f^2/12) - 1``."""
    return math.sqrt(1.0 + f * f / 12.0) - 1.0


__all__ = [
    "PlaneQuantizer",
    "RECON_MODES",
    "added_noise_rms",
    "build_lut",
    "curve_fwd",
    "curve_inv",
    "IDENTITY_STEP_DN",
    "curve_params",
    "identity_step_for",
    "resolve_recon",
    "saturation_code",
]
