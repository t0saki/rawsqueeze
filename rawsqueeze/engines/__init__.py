"""Codec engines and their registry.

Contract for engine modules (``rawsqueeze/engines/<name>.py``): expose a module-level object
``ENGINE`` that satisfies :class:`Engine`.  :func:`get_engine` imports the module lazily.

Division of labour with the encoder pipeline (``rawsqueeze.pipeline.encode_frame_ex``):

* The pipeline pads the mosaic to even size (``cfa.pad_even``) and passes a frame whose
  ``mosaic`` is even-sized (``dataclasses.replace(frame, mosaic=padded)``); it estimates
  noise and resolves presets into :class:`EngineParams`.
* ``Engine.encode`` returns :class:`EngineOutput`: the codec chunks (never HEAD/META/INDX)
  plus the dict that becomes ``head["codec"]`` (e.g. ``{"layout": "planes", "effort": 3,
  "nlq": {...}}``) and optional extra HEAD fields (e.g. ``{"mode": "lossy"}``), and the
  reconstructed mosaic if the engine computed it cheaply (for ``recon_sha256``).
* ``Engine.decode`` receives the full HEAD dict and the decompressed chunk payloads
  (``RsqFile.chunks``: fourcc str -> bytes) and returns the even-sized HxW uint16 mosaic
  (``head["mosaic"]["height"/"width"]``); the pipeline crops to ``orig_height/orig_width``.
  Decoders must use only HEAD + chunks (never rawpy/LibRaw).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from ..container import Chunk
    from ..rawio import RawFrame

ENGINE_MODULES: dict[str, str] = {
    "nlq": "rawsqueeze.engines.nlq",
    "lossless": "rawsqueeze.engines.nlq",  # nlq with f == 0
    "half3": "rawsqueeze.engines.half3",
    "gat4": "rawsqueeze.engines.gat4",
}
"""Engine name -> module path.  'lossless' is an alias of nlq (f=0)."""

EXPERIMENTAL: frozenset[str] = frozenset({"gat4"})
"""Engines never chosen by ``--engine auto`` (gat4 lacks the DESIGN.md 6 AHD validation)."""

HEAD_ENGINE_NAMES: frozenset[str] = frozenset({"nlq", "half3", "gat4"})
"""Values written to ``head["engine"]`` (lossless files record ``nlq`` + ``mode: lossless``)."""


@dataclass
class EngineParams:
    """Resolved per-file engine parameters (built by presets/pipeline, read by engines).

    ``quality``: nlq -> f (0 = lossless); half3/gat4 -> JXL distance d.
    ``noise``: per-position noise parameters in position order, objects with ``.g`` and
    ``.s2`` attributes (``noise.NoiseParams``); None in lossless mode.
    """

    quality: float = 0.0
    dD: float | None = None
    effort: int | None = None
    layout: str | None = None
    recon: str = "auto"
    threads: int | None = None
    noise: Sequence[Any] | None = None
    use_matrix: bool = True
    satmask: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class EngineOutput:
    """Result of :meth:`Engine.encode`."""

    chunks: list[Chunk]
    codec: dict[str, Any]
    """Becomes ``head["codec"]`` (merged over the pipeline's libjxl/imagecodecs versions)."""
    head_extra: dict[str, Any] = field(default_factory=dict)
    """Additional top-level HEAD fields set by the engine (e.g. ``{"mode": "lossy"}``)."""
    recon: np.ndarray | None = None
    """Reconstructed even-sized mosaic if available without extra cost (for recon_sha256)."""


@runtime_checkable
class Engine(Protocol):
    name: str

    def encode(self, frame: RawFrame, params: EngineParams) -> EngineOutput: ...

    def decode(
        self,
        head: Mapping[str, Any],
        chunks: Mapping[str, bytes],
        *,
        threads: int | None = None,
    ) -> np.ndarray: ...


_CACHE: dict[str, Engine] = {}


def available_engines() -> list[str]:
    """Registered engine names (modules may not be importable yet)."""
    return list(ENGINE_MODULES)


def get_engine(name: str) -> Engine:
    """Import ``rawsqueeze.engines.<module>`` lazily and return its ``ENGINE`` object.

    Raises ``ValueError`` for unknown names, ``ImportError`` if the module is missing,
    ``AttributeError`` if it does not define ``ENGINE``.
    """
    if name in _CACHE:
        return _CACHE[name]
    try:
        modname = ENGINE_MODULES[name]
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; known: {', '.join(ENGINE_MODULES)}") from None
    mod = importlib.import_module(modname)
    engine = getattr(mod, "ENGINE")
    _CACHE[name] = engine
    return engine


__all__ = [
    "ENGINE_MODULES",
    "EXPERIMENTAL",
    "HEAD_ENGINE_NAMES",
    "Engine",
    "EngineOutput",
    "EngineParams",
    "available_engines",
    "get_engine",
]
