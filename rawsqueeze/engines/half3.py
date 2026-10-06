"""Engine ``half3``: half-resolution linear sRGB (JXL VarDCT/XYB) + G1-G2 plane + saturation mask.

DESIGN.md section 2.5.  Encode (even-sized RGGB-like Bayer mosaic ``m``):

1. ``S = m >= wl`` (saturation mask, mosaic coordinates); ``m <- min(m, wl)``.
2. Per position ``p``: ``n_p = clip((P_p - blk_p) / (wl - blk_p), 0, 1)`` (float32).
3. ``wb = [cwb_R / cwb_G, 1, cwb_B / cwb_G]`` (colour-index lookup through the pattern), with
   the fallback chain camera -> daylight -> unity.
4. ``c = xyz_to_cam[R,G,B rows] @ XYZ_FROM_SRGB``, rows normalised to sum 1, ``M = inv(c)``
   (srgb_from_cam, dcraw convention; rows picked by colour letter, so every CFA phase gets
   the same matrix).  ``M = I`` if the matrix is all-zero/singular or ``use_matrix`` is off.
   Gamut guard: libjxl's XYB clamps negative opsin mixes (``OPSIN @ rgb + bias``), which
   rewrites saturated out-of-gamut colours (blue/cyan LEDs).  When more than
   ``GAMUT_TOLERANCE`` of the sites would be clamped, ``M`` is blended towards ``I`` by the
   smallest ``alpha`` that keeps them >= 0 (HEAD ``matrix_blend``).  ``M`` and ``Minv``
   (of the blended matrix) are stored float64 in HEAD; the decoder only uses ``Minv``.
5. ``rgb = stack([R*wbR, (G1+G2)/2, B*wbB]) @ M.T`` -> float32, NOT clipped -> ``H3RG``
   (3-channel VarDCT, distance ``d``, effort 5).
6. ``D = (G1 - G2) + 0.5`` float32 -> ``H3DG`` (gray VarDCT, distance ``dD`` = ``d``).
7. ``SATM = zstd19(packbits(S))`` (zstd applied by the container writer).
8. ``H3RG`` and ``H3DG`` are encoded in two threads, libjxl threads split 3:1.

Decode uses only HEAD + chunks (never LibRaw): inverse matrix, inverse WB, ``G1/G2 = G -/+ D/2``,
de-normalise with ``clip(rint(n*(wl-blk)+blk), blk, wl-1)`` (``wl`` without a mask), then
``m[S] = wl`` -- so unmasked pixels never decode as clipped.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import cfa, jxl
from ..container import Chunk, make_chunk, pack_mask, unpack_mask
from . import EngineOutput, EngineParams

if TYPE_CHECKING:
    from ..rawio import RawFrame

NAME = "half3"
DEFAULT_D = 0.2
DEFAULT_EFFORT = 5

XYZ_FROM_SRGB = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
"""XYZ <- linear sRGB (D65), as in DESIGN.md A.1."""

ROLE_ORDER: tuple[str, ...] = ("R", "G1", "G2", "B")


# ---------------------------------------------------------------------------------------
# shared helpers (also used by gat4)


def resolve_threads(threads: int | None) -> int:
    """``None`` -> ``os.cpu_count()``; always >= 1."""
    if threads is None:
        return max(1, os.cpu_count() or 1)
    return max(1, int(threads))


def split_threads_3_1(n: int) -> tuple[int, int]:
    """Split ``n`` libjxl threads 3:1 between the rgb and the D stream (each >= 1)."""
    n = max(1, int(n))
    a = max(1, int(round(n * 0.75)))
    b = max(1, n - a)
    return a, b


def saturation_mask(mosaic: np.ndarray, white: int) -> np.ndarray:
    """Boolean HxW mask of raw values ``>= white`` (computed on the unclamped mosaic)."""
    return np.asarray(mosaic) >= int(white)


def satm_chunk(mask: np.ndarray) -> Chunk:
    """``SATM`` chunk: ``packbits(mask.ravel(), bitorder='big')``; zstd-19 via the chunk flag."""
    return make_chunk("SATM", pack_mask(mask))


def apply_satm(
    out: np.ndarray, chunks: Mapping[str, bytes], white: int, *, required: bool
) -> int:
    """Restore ``out[mask] = white`` in place from the ``SATM`` chunk; returns #pixels set.

    Raises ``ValueError`` if ``required`` and the chunk is missing.
    """
    b = chunks.get("SATM")
    if b is None:
        if required:
            raise ValueError("HEAD says satmask=true but the SATM chunk is missing")
        return 0
    mask = unpack_mask(bytes(b), out.shape)
    out[mask] = np.uint16(white)
    return int(np.count_nonzero(mask))


def frame_roles(frame: RawFrame) -> dict[str, int]:
    """Colour roles (position indices); ``ValueError`` if the CFA is not RGGB-like Bayer."""
    if not frame.is_bayer:
        raise ValueError(
            f"engine requires an RGGB-like 2x2 Bayer CFA; got pattern "
            f"{np.asarray(frame.pattern).tolist()} desc {frame.color_desc!r} raw_type {frame.raw_type!r}"
        )
    return frame.color_roles()


def head_roles(head: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, int]:
    """Roles stored in the codec block (``roles`` = [R, G1, G2, B] positions) or derived from HEAD cfa."""
    r = block.get("roles")
    if r is not None:
        return {name: int(v) for name, v in zip(ROLE_ORDER, r)}
    c = head["cfa"]
    return cfa.color_roles(c["pattern"], c["color_desc"])


def _even_hw(mosaic: np.ndarray) -> tuple[int, int]:
    if mosaic.ndim != 2 or mosaic.shape[0] % 2 or mosaic.shape[1] % 2:
        raise ValueError(f"engine needs an even-sized 2-D mosaic (pad_even first), got {mosaic.shape}")
    return int(mosaic.shape[0]), int(mosaic.shape[1])


# ---------------------------------------------------------------------------------------
# colour


def _valid_wb3(v: Sequence[float]) -> bool:
    return len(v) >= 3 and all(math.isfinite(float(x)) and float(x) > 0 for x in v)


def _role_color_index(frame: RawFrame, roles: Mapping[str, int], role: str) -> int:
    """LibRaw colour index used to look up WB / matrix rows for ``role``.

    The index is taken by colour *letter* (first occurrence in ``color_desc``), not from the
    raw pattern: LibRaw labels one of the two greens with index 3 (e.g. BGGR is
    ``[[2, 3], [1, 0]]``), and index 3 usually has no matrix row / a zero WB entry.
    """
    pat = np.asarray(frame.pattern, dtype=np.int64)
    dy, dx = cfa.POSITIONS[roles[role]]
    idx = int(pat[dy, dx])
    desc = cfa._desc_str(frame.color_desc)
    if 0 <= idx < len(desc):
        first = desc.find(desc[idx])
        if first >= 0:
            return first
    return idx


def white_balance(frame: RawFrame, roles: Mapping[str, int]) -> tuple[list[float], str]:
    """``([wbR, 1, wbB], source)`` relative to G, with fallback camera -> daylight -> unity.

    WB arrays are indexed by LibRaw colour index; the index of each role is looked up by
    colour letter (:func:`_role_color_index`).  ``frame.camera_wb`` is already the effective
    WB from rawio, but invalid values are re-checked here so frames built by hand also work.
    """
    ir, ig, ib = (_role_color_index(frame, roles, r) for r in ("R", "G1", "B"))
    for wb, src in ((frame.camera_wb, frame.wb_source or "camera"), (frame.daylight_wb, "daylight")):
        w = [float(x) for x in wb]
        if len(w) > max(ir, ig, ib):
            trip = [w[ir], w[ig], w[ib]]
            if _valid_wb3(trip):
                return [trip[0] / trip[1], 1.0, trip[2] / trip[1]], src
    return [1.0, 1.0, 1.0], "unity"


def srgb_from_cam(frame: RawFrame, roles: Mapping[str, int], *, use_matrix: bool = True) -> tuple[np.ndarray, bool]:
    """``(M, matrix_used)``: dcraw-convention srgb_from_cam (rows of cam matrix normalised).

    Matrix rows are selected by colour letter (independent of the CFA phase).  Falls back
    to the identity (WB only) when disabled, all-zero, non-finite or singular.
    """
    eye = np.eye(3, dtype=np.float64)
    if not use_matrix:
        return eye, False
    xyz_to_cam = np.asarray(frame.xyz_to_cam, dtype=np.float64)
    if xyz_to_cam.ndim != 2 or xyz_to_cam.shape[0] < 3 or xyz_to_cam.shape[1] != 3:
        return eye, False
    rows = [_role_color_index(frame, roles, r) for r in ("R", "G1", "B")]
    if max(rows) >= xyz_to_cam.shape[0]:
        return eye, False
    cam = xyz_to_cam[rows]
    if not np.all(np.isfinite(cam)) or not np.any(cam):
        return eye, False
    c = cam @ XYZ_FROM_SRGB
    s = c.sum(axis=1, keepdims=True)
    if np.any(np.abs(s) < 1e-12):
        return eye, False
    c = c / s
    if abs(np.linalg.det(c)) < 1e-12:
        return eye, False
    M = np.linalg.inv(c)
    if not np.all(np.isfinite(M)):
        return eye, False
    return M, True


# libjxl's XYB transform (lib/jxl/cms/opsin_params.h): mix = OPSIN @ rgb + bias is clamped to
# >= 0 before the cube root, so colours with a negative mix are altered irreversibly.
OPSIN_ABSORBANCE = np.array(
    [
        [0.30, 0.622, 0.078],
        [0.23, 0.692, 0.078],
        [0.24342268924547819, 0.20476744424496821, 0.55180986650955360],
    ],
    dtype=np.float64,
)
OPSIN_BIAS = 0.0037930732552754493
GAMUT_TOLERANCE = 1e-6
"""Fraction of half-resolution sites allowed to keep a negative opsin mix (isolated outliers)."""


def gamut_blend(cam: Sequence[np.ndarray], M: np.ndarray, *, tolerance: float = GAMUT_TOLERANCE) -> tuple[float, int]:
    """Smallest ``alpha`` so that ``(1-alpha)*M + alpha*I`` keeps every opsin mix >= 0.

    ``cam``: the three WB-applied camera planes (non-negative).  With ``M = I`` every mix is
    >= the bias (all opsin coefficients are positive), and the mix is linear in ``alpha``,
    so the per-site requirement is ``-m / (n - m)`` for each negative mix ``m`` (``n``: the
    mix with ``M = I``).  Up to ``tolerance * sites`` sites may stay negative.  Returns
    ``(alpha, negative_sites_at_alpha0)``.
    """
    A = (OPSIN_ABSORBANCE @ M).astype(np.float32)
    O = OPSIN_ABSORBANCE.astype(np.float32)
    bias = np.float32(OPSIN_BIAS)
    req: np.ndarray | None = None
    for i in range(3):
        m = cam[0] * A[i, 0]
        m += cam[1] * A[i, 1]
        m += cam[2] * A[i, 2]
        m += bias
        neg = m < 0
        if not neg.any():
            continue
        mn = m[neg]
        n = cam[0][neg] * O[i, 0] + cam[1][neg] * O[i, 1] + cam[2][neg] * O[i, 2] + bias
        r = -mn / np.maximum(n - mn, np.float32(1e-12))
        if req is None:
            req = np.zeros(m.shape, dtype=np.float32)
        sub = req[neg]
        np.maximum(sub, r, out=sub)
        req[neg] = sub
    if req is None:
        return 0.0, 0
    pos = req[req > 0]
    npos = int(pos.size)
    k = int(tolerance * req.size)
    if npos <= k:
        return 0.0, npos
    a = float(np.partition(pos, npos - k - 1)[npos - k - 1])
    return min(1.0, a + 1e-3), npos


def _matrix_from_json(v: Any) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    if a.shape != (3, 3):
        raise ValueError(f"expected 3x3 matrix in HEAD, got shape {a.shape}")
    return a


# ---------------------------------------------------------------------------------------
# engine


class Half3Engine:
    """half3 engine (see module docstring)."""

    name = NAME

    def encode(self, frame: RawFrame, params: EngineParams) -> EngineOutput:
        m = np.asarray(frame.mosaic)
        _even_hw(m)
        roles = frame_roles(frame)
        d = float(params.quality) if params.quality and params.quality > 0 else DEFAULT_D
        dD = float(params.dD) if params.dD is not None and params.dD > 0 else d
        effort = int(params.effort) if params.effort is not None else DEFAULT_EFFORT
        nthreads = resolve_threads(params.threads)
        white = int(frame.white)
        blk = [int(v) for v in frame.black_per_position]
        if any(white - b <= 0 for b in blk):
            raise ValueError(f"white level {white} must exceed every black level {blk}")

        sat = saturation_mask(m, white) if params.satmask else None
        planes = cfa.split_planes(np.minimum(m, white))

        def norm(k: int) -> np.ndarray:
            x = (planes[k].astype(np.float32) - np.float32(blk[k])) * np.float32(1.0 / (white - blk[k]))
            return np.clip(x, 0.0, 1.0, out=x)

        R, G1, G2, B = (norm(roles[r]) for r in ROLE_ORDER)
        del planes
        wb, wb_source = white_balance(frame, roles)
        M, matrix_used = srgb_from_cam(frame, roles, use_matrix=params.use_matrix)
        cam = (R * np.float32(wb[0]), (G1 + G2) * np.float32(0.5), B * np.float32(wb[2]))
        alpha, neg_sites = 0.0, 0
        if matrix_used:
            # keep out-of-gamut colours representable in XYB (libjxl clamps negative opsin mixes)
            alpha, neg_sites = gamut_blend(cam, M)
            if alpha > 0:
                Mb = (1.0 - alpha) * M + alpha * np.eye(3)
                if abs(np.linalg.det(Mb)) < 1e-9:
                    Mb, alpha = np.eye(3), 1.0
                M = Mb
        Minv = np.linalg.inv(M)

        Mf = M.astype(np.float32)
        h2, w2 = R.shape
        rgb = np.empty((h2, w2, 3), dtype=np.float32)
        for i in range(3):
            # rgb[..., i] = sum_j M[i, j] * cam[j]  (== stack(cam) @ M.T), float32, unclipped
            acc = cam[0] * Mf[i, 0]
            acc += cam[1] * Mf[i, 1]
            acc += cam[2] * Mf[i, 2]
            rgb[..., i] = acc
        del cam, R, B
        D = G1 - G2
        D += np.float32(0.5)
        del G1, G2

        t_rgb, t_d = split_threads_3_1(nthreads)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_rgb = ex.submit(jxl.encode_lossy, rgb, distance=d, effort=effort, threads=t_rgb)
            f_d = ex.submit(jxl.encode_lossy, D, distance=dD, effort=effort, threads=t_d)
            b_rgb, b_d = f_rgb.result(), f_d.result()

        chunks: list[Chunk] = [make_chunk("H3RG", b_rgb), make_chunk("H3DG", b_d)]
        if sat is not None:
            chunks.append(satm_chunk(sat))

        codec = {
            "effort": effort,
            "libjxl_version": jxl.libjxl_version(),
            "imagecodecs": jxl.imagecodecs_version(),
            NAME: {
                "d": d,
                "dD": dD,
                "wb": [float(v) for v in wb],
                "wb_source": wb_source,
                "M": M.tolist(),
                "Minv": Minv.tolist(),
                "matrix": bool(matrix_used),
                "matrix_blend": round(float(alpha), 6),
                "gamut_neg_sites": int(neg_sites),
                "satmask": sat is not None,
                "roles": [int(roles[r]) for r in ROLE_ORDER],
                "normalize": "clip01",
                "d_offset": 0.5,
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
            raise ValueError(f"half3 HEAD mosaic size must be even, got {H}x{W}")
        white = int(head["levels"]["white"])
        blk = [int(v) for v in head["levels"]["black_per_position"]]
        roles = head_roles(head, block)
        wb = [float(v) for v in block["wb"]]
        Minv = _matrix_from_json(block["Minv"])
        d_off = float(block.get("d_offset", 0.5))
        for fc in ("H3RG", "H3DG"):
            if fc not in chunks:
                raise ValueError(f"half3 chunk {fc} missing")

        t_rgb, t_d = split_threads_3_1(resolve_threads(threads))
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_rgb = ex.submit(jxl.decode, chunks["H3RG"], threads=t_rgb)
            f_d = ex.submit(jxl.decode, chunks["H3DG"], threads=t_d)
            rgb, Dq = f_rgb.result(), f_d.result()
        rgb = _as_unit_float(rgb)
        Dq = _as_unit_float(Dq)
        h2, w2 = H // 2, W // 2
        if rgb.shape != (h2, w2, 3):
            raise ValueError(f"H3RG decoded to shape {rgb.shape}, expected {(h2, w2, 3)}")
        Dq = Dq.reshape(h2, w2) if Dq.size == h2 * w2 else None
        if Dq is None:
            raise ValueError("H3DG decoded to unexpected size")

        r64 = rgb.astype(np.float64)
        cam = [r64[..., 0] * Minv[i, 0] + r64[..., 1] * Minv[i, 1] + r64[..., 2] * Minv[i, 2] for i in range(3)]
        del r64, rgb
        Dh = (Dq.astype(np.float64) - d_off) * 0.5
        vals = {
            "R": cam[0] / wb[0],
            "G1": cam[1] + Dh,
            "G2": cam[1] - Dh,
            "B": cam[2] / wb[2],
        }
        # unmasked pixels never reach wl (the mask restores the clipped ones): no false clips
        hi = white - 1 if "SATM" in chunks else white
        out = np.empty((H, W), dtype=np.uint16)
        for role in ROLE_ORDER:
            k = roles[role]
            dy, dx = cfa.POSITIONS[k]
            x = vals[role] * float(white - blk[k]) + float(blk[k])
            np.rint(x, out=x)
            np.clip(x, blk[k], max(blk[k], hi), out=x)
            out[dy::2, dx::2] = x.astype(np.uint16)
        apply_satm(out, chunks, white, required=bool(block.get("satmask", "SATM" in chunks)))
        return out


def _as_unit_float(a: np.ndarray) -> np.ndarray:
    """Decoded JXL samples as float in nominal [0, 1] (float32 expected; ints normalised)."""
    if a.dtype == np.float32 or a.dtype == np.float16 or a.dtype == np.float64:
        return a
    if a.dtype == np.uint16:
        return a.astype(np.float32) / np.float32(65535.0)
    if a.dtype == np.uint8:
        return a.astype(np.float32) / np.float32(255.0)
    raise TypeError(f"unexpected JXL decode dtype {a.dtype} for a float stream")


ENGINE = Half3Engine()

__all__ = [
    "ENGINE",
    "GAMUT_TOLERANCE",
    "Half3Engine",
    "OPSIN_ABSORBANCE",
    "OPSIN_BIAS",
    "XYZ_FROM_SRGB",
    "apply_satm",
    "gamut_blend",
    "saturation_mask",
    "satm_chunk",
    "split_threads_3_1",
    "srgb_from_cam",
    "white_balance",
]
