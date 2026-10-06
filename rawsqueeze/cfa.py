"""2x2 CFA helpers: polyphase plane split/merge, colour roles, padding.

Conventions (DESIGN.md section 2.0):

* Planes are always in POSITION order ``(0,0), (0,1), (1,0), (1,1)`` (raster order of the
  2x2 cell), independent of which colour sits where.  ``POSITIONS[k] == (dy, dx)`` and
  plane ``k`` is ``m[dy::2, dx::2]``.
* ``pattern`` is LibRaw's ``raw_pattern`` (2x2 array of colour *indices*), ``color_desc`` is
  LibRaw's ``color_desc`` (e.g. ``"RGBG"``); ``color_desc[pattern[dy, dx]]`` is the colour
  letter at that position.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

POSITIONS: tuple[tuple[int, int], ...] = ((0, 0), (0, 1), (1, 0), (1, 1))
"""Position order of the four polyphase planes: index k -> (dy, dx)."""

ROLE_NAMES: tuple[str, ...] = ("R", "G1", "G2", "B")
DNG_COLOR_CODES: dict[str, int] = {"R": 0, "G": 1, "B": 2}


def _desc_str(color_desc: str | bytes) -> str:
    if isinstance(color_desc, (bytes, bytearray)):
        return bytes(color_desc).decode("ascii", errors="replace")
    return str(color_desc)


def _pattern_array(pattern: object) -> np.ndarray:
    p = np.asarray(pattern, dtype=np.int64)
    if p.shape != (2, 2):
        raise ValueError(f"only 2x2 CFA patterns are supported, got shape {p.shape}")
    return p


def position_letters(pattern: object, color_desc: str | bytes) -> list[str]:
    """Colour letter at each position (position order), e.g. ``['R', 'G', 'G', 'B']``."""
    p = _pattern_array(pattern)
    desc = _desc_str(color_desc)
    out: list[str] = []
    for dy, dx in POSITIONS:
        idx = int(p[dy, dx])
        if not 0 <= idx < len(desc):
            raise ValueError(f"pattern index {idx} out of range for color_desc {desc!r}")
        out.append(desc[idx])
    return out


def split_planes(m: np.ndarray, pattern: object | None = None) -> np.ndarray:
    """Split an even-sized HxW mosaic into a contiguous ``(4, H/2, W/2)`` array in position order.

    ``pattern`` is accepted for signature compatibility with the spec and ignored: the
    split is purely positional.  Raises ``ValueError`` on odd or non-2-D input (use
    :func:`pad_even` first).
    """
    del pattern
    if m.ndim != 2:
        raise ValueError(f"mosaic must be 2-D, got shape {m.shape}")
    h, w = m.shape
    if h % 2 or w % 2:
        raise ValueError(f"mosaic dimensions must be even, got {m.shape}; call pad_even first")
    out = np.empty((4, h // 2, w // 2), dtype=m.dtype)
    for k, (dy, dx) in enumerate(POSITIONS):
        out[k] = m[dy::2, dx::2]
    return out


def merge_planes(planes: np.ndarray | Sequence[np.ndarray]) -> np.ndarray:
    """Inverse of :func:`split_planes`: interleave 4 planes (position order) into a mosaic."""
    if isinstance(planes, np.ndarray):
        if planes.ndim != 3 or planes.shape[0] != 4:
            raise ValueError(f"expected (4, h, w) array, got {planes.shape}")
        plist = [planes[k] for k in range(4)]
    else:
        plist = list(planes)
        if len(plist) != 4:
            raise ValueError(f"expected 4 planes, got {len(plist)}")
    h, w = plist[0].shape
    dtype = np.result_type(*[p.dtype for p in plist])
    out = np.empty((2 * h, 2 * w), dtype=dtype)
    for k, (dy, dx) in enumerate(POSITIONS):
        if plist[k].shape != (h, w):
            raise ValueError("all planes must have the same shape")
        out[dy::2, dx::2] = plist[k]
    return out


def color_roles(pattern: object, color_desc: str | bytes) -> dict[str, int]:
    """Map colour roles to position indices: ``{'R': k, 'G1': k, 'G2': k, 'B': k}``.

    The two green positions are ordered in raster (position) order.  Raises
    ``ValueError`` unless the 2x2 cell contains exactly one R, two G and one B.
    """
    letters = position_letters(pattern, color_desc)
    r = [k for k, c in enumerate(letters) if c == "R"]
    g = [k for k, c in enumerate(letters) if c == "G"]
    b = [k for k, c in enumerate(letters) if c == "B"]
    if len(r) != 1 or len(g) != 2 or len(b) != 1:
        raise ValueError(f"not an RGGB-like Bayer CFA: positions -> {letters}")
    return {"R": r[0], "G1": g[0], "G2": g[1], "B": b[0]}


def is_bayer_rggb_like(pattern: object, color_desc: str | bytes) -> bool:
    """True if the pattern is a 2x2 cell with exactly 1R + 2G + 1B."""
    try:
        color_roles(pattern, color_desc)
    except ValueError:
        return False
    return True


def dng_cfa_pattern(pattern: object, color_desc: str | bytes) -> list[int]:
    """DNG ``CFAPattern`` value: per-position colour codes (0=R, 1=G, 2=B), e.g. ``[0, 1, 1, 2]``."""
    letters = position_letters(pattern, color_desc)
    try:
        return [DNG_COLOR_CODES[c] for c in letters]
    except KeyError as exc:
        raise ValueError(f"colour {exc.args[0]!r} cannot be expressed as DNG R/G/B CFA") from None


def black_per_position(pattern: object, black_per_channel: Sequence[int]) -> list[int]:
    """Map LibRaw's per-colour-index black levels to per-position black levels."""
    p = _pattern_array(pattern)
    blk = list(black_per_channel)
    return [int(blk[int(p[dy, dx])]) for dy, dx in POSITIONS]


def pad_even(m: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """Edge-replicate a 2-D mosaic to even height/width.

    Returns ``(padded, (orig_h, orig_w))``.  When already even, returns the input array
    itself (no copy).
    """
    if m.ndim != 2:
        raise ValueError(f"mosaic must be 2-D, got shape {m.shape}")
    h, w = m.shape
    ph, pw = h % 2, w % 2
    if not ph and not pw:
        return m, (h, w)
    return np.pad(m, ((0, ph), (0, pw)), mode="edge"), (h, w)


def crop_to(m: np.ndarray, orig_hw: Sequence[int]) -> np.ndarray:
    """Crop a (padded) mosaic back to ``orig_hw``; returns a view."""
    h, w = int(orig_hw[0]), int(orig_hw[1])
    if h > m.shape[0] or w > m.shape[1]:
        raise ValueError(f"cannot crop {m.shape} to larger size {(h, w)}")
    return m[:h, :w]
