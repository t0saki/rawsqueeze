"""Thin JPEG XL wrapper over imagecodecs (libjxl).

Observed behaviour (imagecodecs 2026.8.16 / libjxl 0.12.0, verified by tests/test_jxl.py):

* Lossless round trip is exact for uint8, uint16 gray, uint16 multi-channel (e.g. HxWx4)
  and float32 gray/RGB in the range [0, 1]; the decoded dtype equals the input dtype.
* float32 decodes as float32 (lossless and lossy).
* ``usecontainer=False`` yields a bare codestream (``FF 0A``) for uint8/float input, but for
  uint16 input with ``bitspersample=None`` libjxl still wraps the stream in an ISO-BMFF
  container with a ``jxll`` (level 10) box: ~40 bytes overhead.  Passing
  ``bitspersample=12`` for 12-bit data gives a bare codestream (52 B smaller on a test).
  :func:`decode` accepts both forms.
* Lossless float32 with values outside [0, 1] fails with ``JXL_ENC_ERR_GENERIC`` at
  effort <= 3 (works at effort >= 5).  Lossy float32 keeps values > 1; gray lossy clamps
  values below about -0.0038 (RGB lossy did not clamp in a small random test).
* Float input is signalled as linear sRGB by default (DESIGN.md A.1).
* ``bitspersample < 8`` with uint16 input fails with ``JXL_ENC_ERR_GENERIC``.
"""

from __future__ import annotations

import functools
import os

import imagecodecs
import numpy as np


def _threads(threads: int | None) -> int:
    if threads is None:
        return max(1, os.cpu_count() or 1)
    return max(1, int(threads))


def _prep(a: np.ndarray) -> np.ndarray:
    if a.dtype not in (np.uint8, np.uint16, np.float32, np.float16):
        raise TypeError(f"unsupported dtype for JPEG XL: {a.dtype} (use uint8/uint16/float32)")
    if a.ndim not in (2, 3):
        raise ValueError(f"expected HxW or HxWxC array, got shape {a.shape}")
    return np.ascontiguousarray(a)


def encode_lossless(
    a: np.ndarray,
    *,
    effort: int = 3,
    threads: int | None = None,
    bitspersample: int | None = None,
) -> bytes:
    """Mathematically lossless JPEG XL (modular) encode; returns codestream bytes.

    ``a``: HxW or HxWxC uint8/uint16/float32 ([0,1] for float).  ``bitspersample``: leave
    ``None`` (spec default); never < 8 for uint16 input.
    """
    return bytes(
        imagecodecs.jpegxl_encode(
            _prep(a),
            lossless=True,
            effort=int(effort),
            numthreads=_threads(threads),
            usecontainer=False,
            bitspersample=bitspersample,
        )
    )


def encode_lossy(
    a: np.ndarray,
    *,
    distance: float,
    effort: int = 5,
    threads: int | None = None,
) -> bytes:
    """Lossy (VarDCT) JPEG XL encode at butteraugli ``distance``; returns codestream bytes.

    float32 input is signalled as linear and may exceed 1.0 (HDR values are kept).
    """
    if distance <= 0:
        raise ValueError("distance must be > 0 for lossy encoding (use encode_lossless)")
    return bytes(
        imagecodecs.jpegxl_encode(
            _prep(a),
            distance=float(distance),
            effort=int(effort),
            numthreads=_threads(threads),
            usecontainer=False,
        )
    )


def decode(b: bytes | bytearray | memoryview, *, threads: int | None = None) -> np.ndarray:
    """Decode a JPEG XL codestream or container to a numpy array (HxW or HxWxC)."""
    return imagecodecs.jpegxl_decode(bytes(b), numthreads=_threads(threads))


@functools.cache
def libjxl_version() -> str:
    """libjxl version, e.g. ``"0.12.0"``."""
    v = str(imagecodecs.jpegxl_version())
    return v.split()[-1] if v else v


def imagecodecs_version() -> str:
    """imagecodecs package version, e.g. ``"2026.8.16"``."""
    return str(imagecodecs.__version__)
