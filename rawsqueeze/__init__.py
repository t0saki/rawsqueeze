"""rawsqueeze: adaptive dual-engine camera RAW compressor (.rsq container, DNG output).

Public API (DESIGN.md 5.2), loaded lazily so that ``import rawsqueeze`` stays cheap::

    encode_file(src, dst, *, preset="vl", engine="auto", quality=None, d=None, f=None,
                effort=None, threads=None, keep_preview=None, store_meta=True, verify=False,
                **opts) -> EncodeReport
    decode_file(src, dst, *, fmt="dng", dng_compression="lj92", dng_tile=256, exif=True,
                threads=None, extract_preview=False) -> DecodeReport
    encode_frame(frame: RawFrame, params: EncodeParams) -> list[Chunk]
    decode_mosaic(rsq: RsqFile | path | bytes, threads=None) -> np.ndarray
    verify_file(rsq_path, original_path, *, evs=(0, 2, 3), tiles=4, full=False,
                metrics=("psnr", "ssimulacra2", "butteraugli", "noise")) -> VerifyReport
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

_LAZY: dict[str, str] = {
    "encode_file": "rawsqueeze.pipeline",
    "decode_file": "rawsqueeze.pipeline",
    "encode_frame": "rawsqueeze.pipeline",
    "encode_frame_ex": "rawsqueeze.pipeline",
    "decode_mosaic": "rawsqueeze.pipeline",
    "decode_chunks": "rawsqueeze.pipeline",
    "verify_file": "rawsqueeze.pipeline",
    "EncodeReport": "rawsqueeze.pipeline",
    "DecodeReport": "rawsqueeze.pipeline",
    "EncodeError": "rawsqueeze.pipeline",
    "IntegrityError": "rawsqueeze.pipeline",
    "OriginalMismatchError": "rawsqueeze.pipeline",
    "VerifyReport": "rawsqueeze.verify",
    "EncodeParams": "rawsqueeze.presets",
    "PRESETS": "rawsqueeze.presets",
    "resolve": "rawsqueeze.presets",
    "RawFrame": "rawsqueeze.rawio",
    "load_raw": "rawsqueeze.rawio",
    "RawReadError": "rawsqueeze.rawio",
    "RsqFile": "rawsqueeze.container",
    "read_rsq": "rawsqueeze.container",
}

if TYPE_CHECKING:  # pragma: no cover
    from .container import RsqFile, read_rsq
    from .pipeline import (
        DecodeReport,
        EncodeError,
        EncodeReport,
        IntegrityError,
        OriginalMismatchError,
        decode_chunks,
        decode_file,
        decode_mosaic,
        encode_file,
        encode_frame,
        encode_frame_ex,
        verify_file,
    )
    from .presets import PRESETS, EncodeParams, resolve
    from .rawio import RawFrame, load_raw
    from .verify import VerifyReport


def __getattr__(name: str) -> Any:
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module 'rawsqueeze' has no attribute {name!r}")
    value = getattr(importlib.import_module(mod), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


__all__ = ["__version__", *_LAZY]
