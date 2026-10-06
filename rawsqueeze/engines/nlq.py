"""nlq engine: noise-adaptive quantization + JPEG XL modular lossless (DESIGN.md 2.2).

``f > 0`` (lossy): per CFA position ``p`` the raw plane is black-subtracted, clamped to
``X = white - black``, companded (``curves``) to integer codes ``q`` (uint8 if
``q_sat < 256`` else uint16) and stored with JXL lossless; the decoder maps codes back with
the stored uint16 LUTs (``LUTS`` chunk).  Pixels >= white get the saturation code and
reconstruct to exactly ``white``.

``f == 0`` (lossless mode, engine alias ``lossless``): the raw uint16 planes are stored
directly (no black subtraction, no clamping; values > white kept), bit exact.

Layouts: ``planes`` (``PLN0..PLN3``, 4 independent codestreams encoded/decoded in a
ThreadPool(4) with ``max(1, threads//4)`` libjxl threads each) or ``stack4`` (one
H/2 x W/2 x 4 codestream ``PLNS``).  Default layout: ``stack4`` if ``effort >= 5`` else
``planes``.

HEAD ``codec`` written by :meth:`NlqEngine.encode`::

    {"layout": "planes"|"stack4", "effort": 3,
     "nlq": {"f": 1.0, "recon": "mid"|"centroid"|null,
             "planes": [{"g", "s2", "offset", "q_sat", "dtype", "black"} x4]   # lossy
                    or [{"dtype": "uint16"} x4]}}                              # lossless

The decoder uses only ``head["mosaic"]["height"/"width"]``, ``head["codec"]`` and the chunks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import jxl
from ..cfa import merge_planes, split_planes
from ..container import Chunk, make_chunk, pack_luts, unpack_luts
from ..curves import PlaneQuantizer, identity_step_for, resolve_recon
from ..noise import NoiseEstimate, as_noise_params, estimate_all
from . import EngineOutput, EngineParams

if TYPE_CHECKING:
    from ..rawio import RawFrame

LAYOUTS = ("planes", "stack4")
DEFAULT_EFFORT = 3
STACK4_EFFORT_THRESHOLD = 5
PLANE_CHUNKS = ("PLN0", "PLN1", "PLN2", "PLN3")


def _threads(threads: int | None) -> int:
    return max(1, int(threads) if threads is not None else (os.cpu_count() or 1))


def resolve_layout(layout: str | None, effort: int) -> str:
    """Explicit layout, else ``stack4`` when ``effort >= 5`` and ``planes`` otherwise."""
    if layout in (None, "", "auto"):
        return "stack4" if effort >= STACK4_EFFORT_THRESHOLD else "planes"
    if layout not in LAYOUTS:
        raise ValueError(f"unknown nlq layout {layout!r}; expected planes or stack4")
    return layout


@dataclass
class NlqEncoded:
    """Result of :func:`encode_mosaic` (engine-independent form)."""

    chunks: list[Chunk]
    codec: dict[str, Any]
    recon: np.ndarray
    quantizers: list[PlaneQuantizer] | None = None
    sizes: dict[str, int] = field(default_factory=dict)
    """Stored payload size per chunk (before the container's zstd for LUTS)."""


def _encode_streams(
    arrays: Sequence[np.ndarray], layout: str, effort: int, threads: int
) -> list[tuple[str, bytes]]:
    if layout == "stack4":
        stack = np.ascontiguousarray(np.stack(arrays, axis=-1))
        return [("PLNS", jxl.encode_lossless(stack, effort=effort, threads=threads))]
    nt = max(1, threads // 4)
    with ThreadPoolExecutor(4) as ex:
        blobs = list(ex.map(lambda a: jxl.encode_lossless(a, effort=effort, threads=nt), arrays))
    return list(zip(PLANE_CHUNKS, blobs))


def _decode_streams(chunks: Mapping[str, bytes], layout: str, threads: int) -> list[np.ndarray]:
    if layout == "stack4":
        if "PLNS" not in chunks:
            raise ValueError("nlq stack4 layout but PLNS chunk is missing")
        a = jxl.decode(chunks["PLNS"], threads=threads)
        if a.ndim != 3 or a.shape[2] != 4:
            raise ValueError(f"PLNS decoded to shape {a.shape}, expected (h, w, 4)")
        return [a[..., k] for k in range(4)]
    missing = [c for c in PLANE_CHUNKS if c not in chunks]
    if missing:
        raise ValueError(f"nlq planes layout but chunks {missing} are missing")
    nt = max(1, threads // 4)
    with ThreadPoolExecutor(4) as ex:
        out = list(ex.map(lambda c: jxl.decode(chunks[c], threads=nt), PLANE_CHUNKS))
    return [a[..., 0] if a.ndim == 3 and a.shape[2] == 1 else a for a in out]


def encode_mosaic(
    mosaic: np.ndarray,
    *,
    black: Sequence[int],
    white: int,
    f: float,
    noise: Sequence[Any] | None = None,
    effort: int | None = None,
    layout: str | None = None,
    recon: str | None = "auto",
    threads: int | None = None,
) -> NlqEncoded:
    """Encode an even-sized uint16 mosaic with nlq (``f > 0``) or raw lossless (``f == 0``).

    ``black``: per-position black levels; ``noise``: 4 objects with ``.g``/``.s2`` (required
    for ``f > 0``).  Returns chunks, the HEAD ``codec`` dict and the reconstruction.
    """
    if mosaic.ndim != 2 or mosaic.shape[0] % 2 or mosaic.shape[1] % 2:
        raise ValueError(f"mosaic must be an even-sized 2-D array, got {mosaic.shape}")
    if mosaic.dtype != np.uint16:
        raise TypeError(f"mosaic must be uint16, got {mosaic.dtype}")
    if f < 0:
        raise ValueError(f"f must be >= 0, got {f}")
    eff = int(effort) if effort is not None else DEFAULT_EFFORT
    lay = resolve_layout(layout, eff)
    nt = _threads(threads)
    planes = split_planes(mosaic)

    if f == 0:
        streams = _encode_streams([planes[k] for k in range(4)], lay, eff, nt)
        chunks = [make_chunk(name, blob) for name, blob in streams]
        codec = {
            "layout": lay,
            "effort": eff,
            "nlq": {"f": 0.0, "recon": None, "planes": [{"dtype": "uint16"} for _ in range(4)]},
        }
        return NlqEncoded(chunks, codec, mosaic, None, {n: len(b) for n, b in streams})

    if noise is None or len(noise) != 4:
        raise ValueError("lossy nlq needs 4 per-position noise parameters")
    nps = as_noise_params(noise)
    rmode = resolve_recon(recon, f)
    blk = [int(b) for b in black]

    def prep(k: int) -> tuple[PlaneQuantizer, np.ndarray, np.ndarray, np.ndarray]:
        P = planes[k]
        off = max(0, blk[k] - int(P.min()))
        pq = PlaneQuantizer.create(nps[k].g, nps[k].s2, f, blk[k], white, off)
        q = pq.quantize(P)
        lut = pq.lut(rmode, P, q)
        return pq, q, lut, lut[q]

    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(prep, range(4)))
    pqs = [r[0] for r in res]
    qs = [r[1] for r in res]
    luts = [r[2] for r in res]
    recon_planes = [r[3] for r in res]

    if lay == "stack4":
        dt = np.uint8 if max(pq.q_sat for pq in pqs) < 256 else np.uint16
        qs = [q.astype(dt, copy=False) for q in qs]
    streams = _encode_streams(qs, lay, eff, nt)
    luts_payload = pack_luts(luts)
    chunks = [make_chunk("LUTS", luts_payload)] + [make_chunk(n, b) for n, b in streams]
    recs = []
    for pq, q in zip(pqs, qs):
        r = pq.head_record()
        r["dtype"] = q.dtype.name
        recs.append(r)
    codec = {"layout": lay, "effort": eff,
             "nlq": {"f": float(f), "recon": rmode, "identity_step": identity_step_for(float(f)), "planes": recs}}
    sizes = {"LUTS": len(luts_payload), **{n: len(b) for n, b in streams}}
    return NlqEncoded(chunks, codec, merge_planes(recon_planes), pqs, sizes)


def decode_mosaic(
    codec: Mapping[str, Any],
    height: int,
    width: int,
    chunks: Mapping[str, bytes],
    *,
    threads: int | None = None,
) -> np.ndarray:
    """Decode nlq/lossless chunks to the even-sized ``height x width`` uint16 mosaic."""
    nlq = codec.get("nlq") or {}
    f = float(nlq.get("f", 0.0))
    lay = str(codec.get("layout", "planes"))
    if lay not in LAYOUTS:
        raise ValueError(f"unknown nlq layout {lay!r}")
    h2, w2 = int(height) // 2, int(width) // 2
    qs = _decode_streams(chunks, lay, _threads(threads))
    for q in qs:
        if q.shape != (h2, w2):
            raise ValueError(f"decoded plane shape {q.shape} != expected {(h2, w2)}")
    if f == 0:
        if any(q.dtype != np.uint16 for q in qs):
            raise ValueError("lossless nlq planes must decode as uint16")
        return merge_planes(qs)
    if "LUTS" not in chunks:
        raise ValueError("lossy nlq stream without LUTS chunk")
    luts = unpack_luts(chunks["LUTS"])
    out = []
    for k, (q, lut) in enumerate(zip(qs, luts)):
        if q.size and int(q.max()) >= lut.size:
            raise ValueError(f"plane {k}: code {int(q.max())} outside LUT of size {lut.size}")
        out.append(lut[q])
    return merge_planes(out)


def _noise_head(noise: Sequence[Any]) -> dict[str, Any]:
    if isinstance(noise, NoiseEstimate):
        return noise.to_head()
    nps = as_noise_params(noise)
    return NoiseEstimate(nps, "manual").to_head() | {"model": "external"}


class NlqEngine:
    """Engine protocol implementation (``ENGINE``); also serves the ``lossless`` alias."""

    name = "nlq"

    def encode(self, frame: RawFrame, params: EngineParams) -> EngineOutput:
        """Encode an even-sized frame.  ``params.quality`` is f (0 = lossless).

        Lossy without ``params.noise``: the noise model is estimated here
        (``estimate_all(frame, threads, noise_model=params.extra.get('noise_model',
        'auto+iso_cap'))``).  ``head_extra`` carries ``mode`` and, when lossy, ``noise``.
        """
        f = float(params.quality or 0.0)
        noise = params.noise
        if f > 0 and noise is None:
            noise = estimate_all(
                frame, params.threads, noise_model=str(params.extra.get("noise_model", "auto+iso_cap"))
            )
        enc = encode_mosaic(
            frame.mosaic,
            black=frame.black_per_position,
            white=frame.white,
            f=f,
            noise=noise,
            effort=params.effort,
            layout=params.layout,
            recon=params.recon,
            threads=params.threads,
        )
        head_extra: dict[str, Any] = {"mode": "lossy" if f > 0 else "lossless"}
        if f > 0 and noise is not None:
            head_extra["noise"] = _noise_head(noise)
        return EngineOutput(chunks=enc.chunks, codec=enc.codec, head_extra=head_extra, recon=enc.recon)

    def decode(
        self,
        head: Mapping[str, Any],
        chunks: Mapping[str, bytes],
        *,
        threads: int | None = None,
    ) -> np.ndarray:
        mos = head["mosaic"]
        return decode_mosaic(head["codec"], int(mos["height"]), int(mos["width"]), chunks, threads=threads)


ENGINE = NlqEngine()

__all__ = [
    "ENGINE",
    "LAYOUTS",
    "NlqEncoded",
    "NlqEngine",
    "decode_mosaic",
    "encode_mosaic",
    "resolve_layout",
]
