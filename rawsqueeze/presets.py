"""Encoding presets and parameter resolution (DESIGN.md 4).

A preset gives the quality knob for each engine (``d`` for half3/gat4, ``f`` for nlq);
:func:`resolve` merges a preset with explicit overrides into :class:`EncodeParams`, which
the pipeline (:mod:`rawsqueeze.pipeline`) consumes.

Quality semantics (DESIGN.md 4.2):

* ``--d`` / ``--f`` override the per-engine value of the preset.
* ``-q/--quality`` is engine-relative (d for half3/gat4, f for nlq) and therefore only
  allowed with a forced engine; with ``engine=auto`` it raises ``ValueError`` (give the pair
  ``--d`` and ``--f`` instead).
* ``engine=lossless`` (or presets ``lossless``/``archival``) is nlq with ``f = 0``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .select import DEFAULT_SNR_THRESHOLD

ENGINE_CHOICES: tuple[str, ...] = ("auto", "half3", "nlq", "gat4", "lossless")
KEEP_PREVIEW_CHOICES: tuple[str, ...] = ("none", "small", "full")
LAYOUT_CHOICES: tuple[str, ...] = ("planes", "stack4")
RECON_CHOICES: tuple[str, ...] = ("auto", "mid", "centroid")


@dataclass(frozen=True)
class Preset:
    """A named quality preset.  ``d``: half3/gat4 JXL distance; ``f``: nlq step in sigma."""

    name: str
    d: float
    f: float
    keep_preview: str = "none"
    lossless: bool = False
    description: str = ""


PRESETS: dict[str, Preset] = {
    "lossless": Preset("lossless", d=0.2, f=0.0, lossless=True, description="bit-exact mosaic (nlq f=0, JXL lossless e3)"),
    "archival": Preset(
        "archival", d=0.2, f=0.0, keep_preview="small", lossless=True,
        description="lossless + camera JPEG (JpgFromRaw) kept as lossless JXL transcode",
    ),
    "high": Preset("high", d=0.1, f=0.5, description="higher fidelity: half3 d0.1 / nlq f0.5"),
    "vl": Preset("vl", d=0.2, f=1.0, description="default, near visually lossless after +2..+3EV: half3 d0.2 / nlq f1"),
    "compact": Preset("compact", d=0.3, f=2.0, description="high compression: half3 d0.3 / nlq f2"),
}
"""DESIGN.md 4.1."""

DEFAULT_PRESET = "vl"


@dataclass
class EncodeParams:
    """Fully resolved encoder parameters (one file).

    ``engine``: auto|half3|nlq|gat4|lossless.  ``d``/``f``: quality per engine family (the
    pipeline picks the one matching the selected engine).  ``effort=None``: engine default
    (nlq/lossless 3, half3/gat4 5).  ``noise=False`` skips the noise estimate for a forced
    half3 (``--no-noise``; then no NoiseProfile).  ``recon_hash``: also compute
    ``mosaic.recon_sha256`` for half3/gat4 (needs an in-memory decode, ~0.2 s; nlq always
    gets it for free).
    """

    preset: str = DEFAULT_PRESET
    engine: str = "auto"
    d: float = 0.2
    f: float = 1.0
    dD: float | None = None
    effort: int | None = None
    layout: str | None = None
    recon: str = "auto"
    noise_model: str = "auto+iso_cap"
    snr_threshold: float = DEFAULT_SNR_THRESHOLD
    use_matrix: bool = True
    satmask: bool = True
    threads: int | None = None
    keep_preview: str = "none"
    store_meta: bool = True
    noise: bool = True
    recon_hash: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def lossless(self) -> bool:
        return self.engine == "lossless" or (self.engine == "nlq" and self.f == 0)

    def quality_for(self, engine: str) -> float:
        """The quality knob for ``engine`` (f for nlq/lossless, d for half3/gat4)."""
        if engine == "lossless":
            return 0.0
        return float(self.f) if engine == "nlq" else float(self.d)

    def param_string(self, engine: str) -> str:
        """Short parameter label, e.g. ``d0.2`` / ``f1.0`` / ``lossless``."""
        q = self.quality_for(engine)
        if engine in ("lossless", "nlq") and q == 0:
            return "lossless"
        return f"{'f' if engine == 'nlq' else 'd'}{q:g}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _f(v: Any, name: str) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {v!r}") from None


def resolve(opts: Mapping[str, Any] | None = None, **kw: Any) -> EncodeParams:
    """Build :class:`EncodeParams` from a preset plus overrides.

    Accepted keys (mapping and/or keywords; keywords win): ``preset``, ``engine``,
    ``quality``/``q``, ``d``, ``f``, ``dD``, ``effort``, ``layout``, ``recon``,
    ``noise_model``, ``snr_threshold``, ``use_matrix``/``matrix``, ``satmask``, ``threads``,
    ``keep_preview``, ``store_meta``/``meta``, ``noise``, ``recon_hash``.  Unknown keys go
    to ``extra``.  ``None`` values mean "not given".  Raises ``ValueError`` on invalid input.
    """
    o: dict[str, Any] = {k: v for k, v in dict(opts or {}).items() if v is not None}
    o.update({k: v for k, v in kw.items() if v is not None})

    name = str(o.pop("preset", DEFAULT_PRESET))
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; expected one of {', '.join(PRESETS)}")
    pr = PRESETS[name]
    engine = str(o.pop("engine", "auto")).lower()
    if engine not in ENGINE_CHOICES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {', '.join(ENGINE_CHOICES)}")
    if pr.lossless:
        if engine in ("half3", "gat4"):
            raise ValueError(f"preset {name!r} is lossless; engine {engine!r} is lossy")
        engine = "lossless"

    d = _f(o.pop("d", None), "d")
    f = _f(o.pop("f", None), "f")
    q = _f(o.pop("quality", o.pop("q", None)), "quality")
    if q is not None:
        if engine == "auto":
            raise ValueError("-q/--quality is engine-relative; with --engine auto give --d and --f instead")
        if engine == "lossless":
            raise ValueError("-q/--quality makes no sense with the lossless engine")
        if engine == "nlq":
            f = q
        else:
            d = q
    p = EncodeParams(preset=name, engine=engine, d=pr.d if d is None else d, f=pr.f if f is None else f,
                     keep_preview=pr.keep_preview)
    if engine == "lossless":
        p.f = 0.0
    if engine == "nlq" and p.f == 0:
        p.engine = "lossless"

    p.dD = _f(o.pop("dD", None), "dD")
    if "effort" in o:
        p.effort = int(o.pop("effort"))
        if not 1 <= p.effort <= 9:
            raise ValueError(f"effort must be in 1..9, got {p.effort}")
    if "layout" in o:
        p.layout = str(o.pop("layout"))
        if p.layout not in LAYOUT_CHOICES:
            raise ValueError(f"layout must be one of {LAYOUT_CHOICES}, got {p.layout!r}")
    if "recon" in o:
        p.recon = str(o.pop("recon"))
        if p.recon not in RECON_CHOICES:
            raise ValueError(f"recon must be one of {RECON_CHOICES}, got {p.recon!r}")
    if "noise_model" in o:
        p.noise_model = str(o.pop("noise_model"))
        from .noise import parse_noise_model

        parse_noise_model(p.noise_model)  # validate
    if "snr_threshold" in o:
        p.snr_threshold = float(o.pop("snr_threshold"))
    for key, alias in (("use_matrix", "matrix"), ("store_meta", "meta")):
        if alias in o:
            o.setdefault(key, o.pop(alias))
    for key in ("use_matrix", "satmask", "store_meta", "noise", "recon_hash"):
        if key in o:
            setattr(p, key, bool(o.pop(key)))
    if "threads" in o:
        p.threads = max(1, int(o.pop("threads")))
    if "keep_preview" in o:
        p.keep_preview = str(o.pop("keep_preview"))
        if p.keep_preview not in KEEP_PREVIEW_CHOICES:
            raise ValueError(f"keep_preview must be one of {KEEP_PREVIEW_CHOICES}, got {p.keep_preview!r}")

    if p.d <= 0:
        raise ValueError(f"d must be > 0, got {p.d}")
    if p.f < 0:
        raise ValueError(f"f must be >= 0, got {p.f}")
    if p.dD is not None and p.dD <= 0:
        raise ValueError(f"dD must be > 0, got {p.dD}")
    p.extra = o
    return p


__all__ = [
    "DEFAULT_PRESET",
    "ENGINE_CHOICES",
    "EncodeParams",
    "KEEP_PREVIEW_CHOICES",
    "LAYOUT_CHOICES",
    "PRESETS",
    "Preset",
    "RECON_CHOICES",
    "resolve",
]
