"""Per-camera ISO prior for the noise model (DESIGN.md section 2.3 step 5).

``K_CAM[(make, model)]`` gives, per CFA POSITION (order ``(0,0),(0,1),(1,0),(1,1)``), the
upper bound of the Poisson gain ``g`` per ISO unit: ``g_used = min(g_est, k[p] * ISO)``.

DC-S9 (pattern ``[[0,1],[3,2]]``, desc ``RGBG`` -> positions R, G1, G2, B): measured by
the noise-adaptive-quantizer designer on ISO100/320/4000 samples.
"""

from __future__ import annotations

K_CAM: dict[tuple[str, str], tuple[float, float, float, float]] = {
    ("Panasonic", "DC-S9"): (6.5e-4, 6.5e-4, 6.5e-4, 4e-4),
}
"""(make, model) -> per-position g upper bound per ISO unit."""

DEFAULT_G_PER_ISO: float = 1e-4
"""Fallback prior ``g = 1e-4 * ISO`` for cameras not in :data:`K_CAM` (used only when the
estimator has too few valid bins)."""


def _norm(s: str | None) -> str:
    return " ".join(str(s or "").split()).casefold()


_NORM_TABLE: dict[tuple[str, str], tuple[float, float, float, float]] = {
    (_norm(mk), _norm(md)): v for (mk, md), v in K_CAM.items()
}


def lookup_k(make: str | None, model: str | None) -> tuple[float, float, float, float] | None:
    """Per-position k for a camera (case/whitespace-insensitive match), or ``None``."""
    if not make or not model:
        return None
    return _NORM_TABLE.get((_norm(make), _norm(model)))


def has_camera(make: str | None, model: str | None) -> bool:
    """True if the camera has an ISO cap entry."""
    return lookup_k(make, model) is not None


__all__ = ["DEFAULT_G_PER_ISO", "K_CAM", "has_camera", "lookup_k"]
