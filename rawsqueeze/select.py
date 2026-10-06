"""Automatic engine selection (DESIGN.md section 2.4).

``SNR18 = x18 / sqrt(g * x18 + s2)`` with ``x18 = 0.18 * X``, ``X = white - black`` and ``(g, s2)``
the mean of the two green positions' (ISO-capped) noise parameters.  ``auto`` picks ``half3``
when ``SNR18 >= snr_threshold`` (default 60) and the CFA is an RGGB-like Bayer, otherwise
``nlq``.

The default was calibrated on 13 DC-S9 files (ISO 100-51200, docs/STATUS.md "Threshold
calibration"): below SNR18 ~40 half3 is no smaller than nlq at equal quality and visibly
smooths grain; 60 (~ISO 450 on the DC-S9) leaves margin above the only sample between 40
and 70.

Noise parameters are computed elsewhere (``rawsqueeze.noise``); this module accepts any of:
objects with ``.g``/``.s2`` (``.g_used`` preferred when present), ``(g, s2)`` tuples, or
mappings with ``g``/``g_used`` and ``s2``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from . import cfa

if TYPE_CHECKING:
    from .rawio import RawFrame

DEFAULT_SNR_THRESHOLD = 60.0
ENGINES = ("auto", "half3", "nlq", "gat4", "lossless")
BAYER_ONLY = frozenset({"half3", "gat4"})


def noise_g_s2(p: Any) -> tuple[float, float]:
    """Extract ``(g, s2)`` from a noise-parameter object, tuple or mapping (``g_used`` wins over ``g``)."""
    if isinstance(p, Mapping):
        g = p.get("g_used", p.get("g"))
        s2 = p.get("s2")
    elif isinstance(p, Sequence) and not isinstance(p, (str, bytes)):
        if len(p) < 2:
            raise ValueError(f"noise tuple needs (g, s2), got {p!r}")
        g, s2 = p[0], p[1]
    else:
        g = getattr(p, "g_used", None)
        if g is None:
            g = getattr(p, "g", None)
        s2 = getattr(p, "s2", None)
    if g is None or s2 is None:
        raise ValueError(f"cannot read (g, s2) from noise parameter {p!r}")
    g, s2 = float(g), float(s2)
    if not (math.isfinite(g) and math.isfinite(s2)):
        raise ValueError(f"non-finite noise parameter g={g} s2={s2}")
    return g, s2


def snr18(
    noise: Sequence[Any],
    white: int,
    black: int | Sequence[int],
    *,
    green_positions: Sequence[int] = (1, 2),
) -> float:
    """SNR at 18 % of the usable range, from the mean of the green positions' (g, s2).

    ``noise``: 4 per-position parameters (position order).  ``black``: scalar or per-position
    list (mean over the green positions is used).  ``green_positions``: the G1/G2 position
    indices (``(1, 2)`` for RGGB/BGGR, ``(0, 3)`` for GRBG/GBRG).
    """
    if len(noise) != 4:
        raise ValueError(f"expected 4 per-position noise parameters, got {len(noise)}")
    gs = [noise_g_s2(noise[k]) for k in green_positions]
    g = sum(v[0] for v in gs) / len(gs)
    s2 = sum(v[1] for v in gs) / len(gs)
    if isinstance(black, Sequence):
        blk = sum(float(black[k]) for k in green_positions) / len(green_positions)
    else:
        blk = float(black)
    X = float(white) - blk
    if X <= 0:
        raise ValueError(f"white {white} must exceed black {blk}")
    x18 = 0.18 * X
    var = g * x18 + s2
    if var <= 0:
        return math.inf
    return x18 / math.sqrt(var)


@dataclass
class SelectOptions:
    """Engine selection options: ``engine`` ('auto' or a forced name) and the SNR18 threshold."""

    engine: str = "auto"
    snr_threshold: float = DEFAULT_SNR_THRESHOLD


@dataclass
class EngineChoice:
    """Result of :func:`select_engine` (for logging / HEAD ``noise.snr18``)."""

    engine: str
    snr18: float | None
    threshold: float
    bayer: bool
    reason: str
    extra: dict[str, Any] = field(default_factory=dict)


def _options(opts: SelectOptions | Mapping[str, Any] | float | None) -> SelectOptions:
    if opts is None:
        return SelectOptions()
    if isinstance(opts, SelectOptions):
        return opts
    if isinstance(opts, (int, float)):
        return SelectOptions(snr_threshold=float(opts))
    if isinstance(opts, Mapping):
        thr = opts.get("snr_threshold", opts.get("threshold"))
        return SelectOptions(
            engine=str(opts.get("engine") or "auto"),
            snr_threshold=DEFAULT_SNR_THRESHOLD if thr is None else float(thr),
        )
    eng = getattr(opts, "engine", "auto") or "auto"
    thr = getattr(opts, "snr_threshold", None)
    return SelectOptions(engine=str(eng), snr_threshold=DEFAULT_SNR_THRESHOLD if thr is None else float(thr))


def select_engine(
    frame: RawFrame,
    noise: Sequence[Any] | None,
    opts: SelectOptions | Mapping[str, Any] | float | None = None,
) -> EngineChoice:
    """Choose the engine for ``frame`` given its per-position ``noise`` parameters.

    * forced engine (``opts.engine != 'auto'``): returned as is; ``half3``/``gat4`` on a
      non-Bayer CFA raise ``ValueError``.  ``snr18`` is still reported when computable.
    * ``auto``: ``half3`` iff Bayer RGGB-like and ``SNR18 >= threshold``; otherwise ``nlq``
      (also when ``noise`` is None or unusable).
    """
    o = _options(opts)
    engine = o.engine.lower()
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {o.engine!r}; expected one of {', '.join(ENGINES)}")
    bayer = bool(frame.is_bayer)

    snr: float | None = None
    why_no_snr = ""
    if not bayer:
        why_no_snr = "non-Bayer CFA"
    elif noise is None:
        why_no_snr = "no noise parameters"
    else:
        roles = cfa.color_roles(frame.pattern, frame.color_desc)
        try:
            snr = snr18(
                noise, frame.white, frame.black_per_position, green_positions=(roles["G1"], roles["G2"])
            )
        except ValueError as exc:
            why_no_snr = f"snr18 unavailable: {exc}"

    if engine != "auto":
        if engine in BAYER_ONLY and not bayer:
            raise ValueError(
                f"engine {engine!r} requires an RGGB-like 2x2 Bayer CFA "
                f"(pattern {np.asarray(frame.pattern).tolist()}, desc {frame.color_desc!r})"
            )
        return EngineChoice(engine, snr, o.snr_threshold, bayer, "forced")

    if snr is None:
        return EngineChoice("nlq", None, o.snr_threshold, bayer, f"fallback to nlq: {why_no_snr}")
    if snr >= o.snr_threshold:
        return EngineChoice("half3", snr, o.snr_threshold, bayer, f"snr18 {snr:.1f} >= {o.snr_threshold:g}")
    return EngineChoice("nlq", snr, o.snr_threshold, bayer, f"snr18 {snr:.1f} < {o.snr_threshold:g}")


def choose_engine(
    frame: RawFrame,
    noise: Sequence[Any] | None,
    opts: SelectOptions | Mapping[str, Any] | float | None = None,
) -> str:
    """Engine name only (spec 5.2 signature); see :func:`select_engine`."""
    return select_engine(frame, noise, opts).engine


__all__ = [
    "DEFAULT_SNR_THRESHOLD",
    "EngineChoice",
    "SelectOptions",
    "choose_engine",
    "noise_g_s2",
    "select_engine",
    "snr18",
]
