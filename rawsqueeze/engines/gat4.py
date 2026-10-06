"""Engine ``gat4`` (experimental): generalized Anscombe transform + per-plane JXL VarDCT.

DESIGN.md section 2.6.  For each position ``p`` with noise ``(g, s2)`` (var = g*x + s2, DN):

* ``x = min(P_p, wl) - blk_p``; ``y = (2/g) * sqrt(max(g*x + 3/8*g^2 + s2, 0))`` (unit-variance
  domain); ``a = rint(y * K)`` as uint16 with fixed ``K = 100`` codes per sigma; ``a`` is coded
  with ``jpegxl_encode(a, distance=d, effort=5)`` into ``G4P0..G4P3``.
* Decode: ``y = a / K``; ``x = ((g*y/2)^2 - 3/8*g^2 - s2) / g``; ``rint(x + blk_p)`` clipped to
  ``[0, wl-1]`` (``[0, wl]`` without a mask); saturated pixels restored from the same
  ``SATM`` mask as half3, so unmasked pixels never decode as clipped.

Deviation (documented): when ``K * y(wl - blk_p)`` would exceed 65535 (very low g, roughly
g < 0.037 for a 12-bit range), that plane's K is lowered to ``floor(65535 / y_max)``; the
per-plane K is stored in HEAD (``codec.gat4.planes[k].K``) and used by the decoder.

Only used with ``--engine gat4``; not part of ``auto``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import cfa, jxl
from ..container import Chunk, make_chunk
from . import EngineOutput, EngineParams
from .half3 import apply_satm, frame_roles, resolve_threads, saturation_mask, satm_chunk

if TYPE_CHECKING:
    from ..rawio import RawFrame

NAME = "gat4"
DEFAULT_D = 0.2
DEFAULT_EFFORT = 5
K_FIXED = 100.0
CODE_MAX = 65535
FOURCCS = ("G4P0", "G4P1", "G4P2", "G4P3")


def gat_fwd(x: np.ndarray, g: float, s2: float) -> np.ndarray:
    """Generalized Anscombe transform to the unit-variance domain (x = raw - black, DN)."""
    x = np.asarray(x, dtype=np.float64)
    return (2.0 / g) * np.sqrt(np.maximum(g * x + 0.375 * g * g + s2, 0.0))


def gat_inv(y: np.ndarray, g: float, s2: float) -> np.ndarray:
    """Algebraic inverse of :func:`gat_fwd` (returns DN above black, may be negative)."""
    y = np.asarray(y, dtype=np.float64)
    t = g * y * 0.5
    return (t * t - 0.375 * g * g - s2) / g


def plane_k(g: float, s2: float, x_max: float, k_fixed: float = K_FIXED) -> float:
    """Codes per sigma for one plane: ``k_fixed`` unless ``k_fixed * y(x_max)`` overflows uint16."""
    y_max = float(gat_fwd(np.float64(x_max), g, s2))
    if y_max * k_fixed <= CODE_MAX:
        return float(k_fixed)
    return float(math.floor(CODE_MAX / y_max * 1e6) / 1e6)


def _noise_list(noise: Sequence[Any] | None) -> list[tuple[float, float]]:
    from ..select import noise_g_s2

    if noise is None or len(noise) != 4:
        raise ValueError("gat4 needs 4 per-position noise parameters (EngineParams.noise)")
    out = []
    for p in noise:
        g, s2 = noise_g_s2(p)
        if not (g > 0):
            raise ValueError(f"gat4 needs g > 0, got {g}")
        out.append((g, max(s2, 0.0)))
    return out


class Gat4Engine:
    """gat4 engine (see module docstring)."""

    name = NAME

    def encode(self, frame: RawFrame, params: EngineParams) -> EngineOutput:
        m = np.asarray(frame.mosaic)
        if m.ndim != 2 or m.shape[0] % 2 or m.shape[1] % 2:
            raise ValueError(f"gat4 needs an even-sized 2-D mosaic (pad_even first), got {m.shape}")
        frame_roles(frame)  # Bayer check
        gs2 = _noise_list(params.noise)
        d = float(params.quality) if params.quality and params.quality > 0 else DEFAULT_D
        effort = int(params.effort) if params.effort is not None else DEFAULT_EFFORT
        k_fixed = float(params.extra.get("gat4_K", K_FIXED))
        nthreads = resolve_threads(params.threads)
        white = int(frame.white)
        blk = [int(v) for v in frame.black_per_position]

        sat = saturation_mask(m, white) if params.satmask else None
        planes = cfa.split_planes(np.minimum(m, white))
        ks = [plane_k(g, s2, white - blk[k], k_fixed) for k, (g, s2) in enumerate(gs2)]

        def enc(k: int) -> bytes:
            g, s2 = gs2[k]
            y = gat_fwd(planes[k].astype(np.float64) - blk[k], g, s2)
            y *= ks[k]
            np.rint(y, out=y)
            np.clip(y, 0, CODE_MAX, out=y)
            a = y.astype(np.uint16)
            return jxl.encode_lossy(a, distance=d, effort=effort, threads=max(1, nthreads // 4))

        with ThreadPoolExecutor(max_workers=4) as ex:
            blobs = list(ex.map(enc, range(4)))

        chunks: list[Chunk] = [make_chunk(fc, b) for fc, b in zip(FOURCCS, blobs)]
        if sat is not None:
            chunks.append(satm_chunk(sat))
        codec = {
            "effort": effort,
            "libjxl_version": jxl.libjxl_version(),
            "imagecodecs": jxl.imagecodecs_version(),
            NAME: {
                "d": d,
                "K": k_fixed,
                "planes": [{"g": g, "s2": s2, "K": kk} for (g, s2), kk in zip(gs2, ks)],
                "satmask": sat is not None,
            },
        }
        return EngineOutput(chunks=chunks, codec=codec, head_extra={"mode": "lossy"})

    def decode(
        self,
        head: Mapping[str, Any],
        chunks: Mapping[str, bytes],
        *,
        threads: int | None = None,
    ) -> np.ndarray:
        block = head["codec"][NAME]
        H = int(head["mosaic"]["height"])
        W = int(head["mosaic"]["width"])
        if H % 2 or W % 2:
            raise ValueError(f"gat4 HEAD mosaic size must be even, got {H}x{W}")
        white = int(head["levels"]["white"])
        blk = [int(v) for v in head["levels"]["black_per_position"]]
        k_default = float(block.get("K", K_FIXED))
        pl = block["planes"]
        if len(pl) != 4:
            raise ValueError("gat4 HEAD needs 4 plane parameter records")
        for fc in FOURCCS:
            if fc not in chunks:
                raise ValueError(f"gat4 chunk {fc} missing")
        nthreads = max(1, resolve_threads(threads) // 4)
        hi = white - 1 if "SATM" in chunks else white
        h2, w2 = H // 2, W // 2

        def dec(k: int) -> np.ndarray:
            a = jxl.decode(chunks[FOURCCS[k]], threads=nthreads)
            if a.size != h2 * w2:
                raise ValueError(f"{FOURCCS[k]} decoded to shape {a.shape}, expected {(h2, w2)}")
            g, s2 = float(pl[k]["g"]), float(pl[k]["s2"])
            kk = float(pl[k].get("K", k_default))
            if a.dtype.kind == "f":
                y = a.astype(np.float64).reshape(h2, w2) * (CODE_MAX / kk)
            else:
                y = a.astype(np.float64).reshape(h2, w2) / kk
            x = gat_inv(y, g, s2)
            x += blk[k]
            np.rint(x, out=x)
            np.clip(x, 0, hi, out=x)
            return x.astype(np.uint16)

        with ThreadPoolExecutor(max_workers=4) as ex:
            planes = list(ex.map(dec, range(4)))
        out = cfa.merge_planes(planes)
        apply_satm(out, chunks, white, required=bool(block.get("satmask", "SATM" in chunks)))
        return out


ENGINE = Gat4Engine()

__all__ = ["ENGINE", "Gat4Engine", "K_FIXED", "gat_fwd", "gat_inv", "plane_k"]
