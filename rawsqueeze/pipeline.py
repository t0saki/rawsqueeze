"""End-to-end encode / decode / verify pipeline (DESIGN.md 2.1, 3.3, 5.2).

This module glues the building blocks together:

* :func:`encode_frame_ex` -- even padding, noise model, engine selection, engine encode,
  HEAD assembly (DESIGN.md 3.3) and ``recon_sha256``.
* :func:`encode_file` -- ``RW2 -> .rsq``: load, META skeleton, previews, encode, optional
  quick verify with automatic fallback to nlq (risk R1), atomic write.
* :func:`decode_chunks` / :func:`decode_mosaic` -- HEAD + chunks -> original-size mosaic,
  with the lossless sha256 check.
* :func:`decode_file` -- ``.rsq -> DNG | npy | pgm16 | tiff``.
* :func:`verify_file` -- ``rawsqueeze verify``.
* :func:`bench_encode_fn` / :func:`bench_decode_fn` -- plug-ins for :mod:`rawsqueeze.bench`.
"""

from __future__ import annotations

import atexit
import dataclasses
import hashlib
import io
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import __version__, jxl
from .cfa import pad_even
from .container import (
    Chunk,
    RsqError,
    RsqFile,
    make_chunk,
    make_head_chunk,
    read_rsq,
    read_rsq_info,
    write_rsq,
)
from .engines import EngineParams, get_engine
from .presets import EncodeParams, resolve
from .rawio import RawFrame, load_raw
from .select import EngineChoice, SelectOptions, select_engine
from .tools import ExifTool, ToolError, default_file_mode, have

DECODE_FORMATS: tuple[str, ...] = ("dng", "npy", "pgm16", "tiff")
QUICK_VERIFY_EVS: tuple[float, ...] = (3.0,)
QUICK_VERIFY_TILES = 2


class EncodeError(ValueError):
    """The input cannot be encoded with the requested parameters."""


class OriginalMismatchError(ValueError):
    """``verify``: the given original is not the file the .rsq was encoded from."""


class IntegrityError(RsqError):
    """Decoded mosaic does not match the sha256 recorded in HEAD (lossless)."""


# ---------------------------------------------------------------------------------------
# exiftool (one stay_open process per worker process -- a cache, not program state)

_ET_LOCK = threading.Lock()
_ET: ExifTool | None = None


def get_exiftool() -> ExifTool | None:
    """Shared ``exiftool -stay_open`` instance for this process (None if exiftool is missing)."""
    global _ET
    with _ET_LOCK:
        if _ET is None:
            if not have("exiftool"):
                return None
            et = ExifTool()
            try:
                et.start()
            except (ToolError, OSError):
                return None
            _ET = et
            atexit.register(et.close)
        return _ET


# ---------------------------------------------------------------------------------------
# reports


@dataclass
class EncodeReport:
    """Result of :func:`encode_file` (one input)."""

    src: str
    dst: str | None
    status: str = "ok"
    """ok | skipped | dry-run | error | verify-failed."""
    engine: str | None = None
    preset: str | None = None
    param: str | None = None
    mode: str | None = None
    snr18: float | None = None
    reason: str | None = None
    src_size: int = 0
    raw_data_size: int | None = None
    out_size: int | None = None
    ratio_file: float | None = None
    ratio_raw: float | None = None
    enc_s: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    chunk_sizes: dict[str, int] = field(default_factory=dict)
    verify: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class DecodeReport:
    """Result of :func:`decode_file` (one input)."""

    src: str
    dst: str | None
    status: str = "ok"
    """ok | skipped | dry-run | error."""
    fmt: str = "dng"
    engine: str | None = None
    mode: str | None = None
    out_size: int | None = None
    dec_s: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    lossless_verified: bool | None = None
    recon_match: bool | None = None
    previews: list[str] = field(default_factory=list)
    lens: dict[str, Any] | None = None
    """DNG only: lens-distortion handling (:attr:`dng.DngReport.lens`; None without distortion data)."""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class FrameEncoding:
    """Result of :func:`encode_frame_ex`: HEAD-first chunk list and diagnostics."""

    chunks: list[Chunk]
    head: dict[str, Any]
    engine: str
    recon: np.ndarray | None
    """Original-size reconstruction when available (nlq always; half3/gat4 with recon_hash)."""
    choice: EngineChoice | None
    noise: Any
    timings: dict[str, float]
    warnings: list[str]


# ---------------------------------------------------------------------------------------
# encode


def _sha256(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def _engine_params(p: EncodeParams, engine: str, noise: Any) -> EngineParams:
    return EngineParams(
        quality=p.quality_for(engine),
        dD=p.dD,
        effort=p.effort,
        layout=p.layout,
        recon=p.recon,
        threads=p.threads,
        noise=noise,
        use_matrix=p.use_matrix,
        satmask=p.satmask,
        extra={"noise_model": p.noise_model, **p.extra},
    )


def build_head(
    frame: RawFrame,
    params: EncodeParams,
    engine: str,
    codec: Mapping[str, Any],
    head_extra: Mapping[str, Any],
    *,
    padded_hw: tuple[int, int],
    noise: Any = None,
    choice: EngineChoice | None = None,
    meta_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the HEAD JSON (DESIGN.md 3.3) from frame + engine output + noise + preset."""
    sec = frame.head_sections()
    mode = str(head_extra.get("mode", "lossless" if params.lossless else "lossy"))
    head: dict[str, Any] = {
        "format": "rsq",
        "version": [1, 0],
        "encoder": f"rawsqueeze {__version__}",
        "mode": mode,
        "preset": params.preset,
        "engine": "nlq" if engine == "lossless" else engine,
        "codec": {"libjxl_version": jxl.libjxl_version(), "imagecodecs": jxl.imagecodecs_version(), **codec},
    }
    for k, v in head_extra.items():
        if k not in ("mode", "noise"):
            head[k] = v
    if noise is not None and mode != "lossless":
        snr = choice.snr18 if choice is not None else None
        to_head = getattr(noise, "to_head", None)
        if callable(to_head):
            head["noise"] = to_head(snr18=snr)
        elif "noise" in head_extra:
            head["noise"] = dict(head_extra["noise"]) | ({"snr18": snr} if snr is not None else {})
    head["selection"] = {
        "requested": params.engine,
        "engine": head["engine"],
        "snr18": choice.snr18 if choice else None,
        "threshold": params.snr_threshold,
        "reason": choice.reason if choice else ("lossless" if params.lossless else "forced"),
        "param": params.param_string(engine),
    }
    src = dict(sec["source"])
    src["iso"] = frame.iso
    head["source"] = src
    mos = dict(sec["mosaic"])
    mos["height"], mos["width"] = int(padded_hw[0]), int(padded_hw[1])
    head["mosaic"] = mos
    for k in ("cfa", "levels", "color", "geometry"):
        head[k] = sec[k]
    head["meta"] = dict(meta_info) if meta_info else {"strategy": "none", "previews": []}
    return head


def encode_frame_ex(
    frame: RawFrame,
    params: EncodeParams,
    *,
    extra_chunks: Sequence[Chunk] = (),
    meta_info: Mapping[str, Any] | None = None,
    noise: Any = None,
) -> FrameEncoding:
    """Encode one frame: pad, noise, select, engine encode, HEAD.  No file I/O.

    ``extra_chunks`` (META/PRVn) are appended after the codec chunks; ``meta_info`` becomes
    ``head["meta"]``.  ``noise``: precomputed :class:`noise.NoiseEstimate` (skips the estimate).
    Raises :class:`EncodeError` for lossy encoding of a non-Bayer CFA.
    """
    from .noise import estimate_all

    t: dict[str, float] = {}
    warnings: list[str] = []
    orig_h, orig_w = frame.height, frame.width
    if orig_h < 2 or orig_w < 2:
        raise EncodeError(f"mosaic too small: {orig_h}x{orig_w}")
    m2, _ = pad_even(frame.mosaic)
    work = frame if m2 is frame.mosaic else dataclasses.replace(frame, mosaic=m2)
    if m2 is not frame.mosaic:
        warnings.append(f"odd size {orig_h}x{orig_w} padded to {m2.shape[0]}x{m2.shape[1]}")

    requested = params.engine
    lossless = params.lossless
    if not lossless and not frame.is_bayer:
        raise EncodeError(
            f"lossy encoding needs an RGGB-like 2x2 Bayer CFA (pattern {np.asarray(frame.pattern).tolist()}, "
            f"desc {frame.color_desc!r}, raw_type {frame.raw_type}); use --preset lossless"
        )

    ne = noise
    t0 = time.perf_counter()
    need_noise = not lossless and not (requested == "half3" and not params.noise)
    if ne is None and need_noise:
        ne = estimate_all(work, params.threads, noise_model=params.noise_model)
    t["noise"] = time.perf_counter() - t0

    choice: EngineChoice | None = None
    if lossless:
        engine = "lossless"
    else:
        choice = select_engine(work, ne, SelectOptions(engine=requested, snr_threshold=params.snr_threshold))
        engine = choice.engine
        if engine == "lossless":
            lossless = True

    t0 = time.perf_counter()
    eng = get_engine(engine)
    out = eng.encode(work, _engine_params(params, engine, ne if not lossless else None))
    t["engine"] = time.perf_counter() - t0

    head = build_head(
        frame, params, engine, out.codec, out.head_extra, padded_hw=(m2.shape[0], m2.shape[1]),
        noise=ne, choice=choice, meta_info=meta_info,
    )
    recon = out.recon
    t0 = time.perf_counter()
    if recon is None and params.recon_hash and engine in ("half3", "gat4"):
        payloads = {c.name: c.payload for c in out.chunks}
        recon = eng.decode(head, payloads, threads=params.threads)
    if recon is not None:
        recon = recon[:orig_h, :orig_w]
        head["mosaic"]["recon_sha256"] = (
            head["mosaic"]["sha256"] if recon is frame.mosaic or head["mode"] == "lossless" else _sha256(recon)
        )
    t["recon"] = time.perf_counter() - t0
    chunks = [make_head_chunk(head), *out.chunks, *extra_chunks]
    return FrameEncoding(chunks, head, head["engine"], recon, choice, ne, t, warnings)


def encode_frame(frame: RawFrame, params: EncodeParams) -> list[Chunk]:
    """Spec 5.2 signature: HEAD-first chunk list for ``frame`` (no META/previews)."""
    return encode_frame_ex(frame, params).chunks


# ---------------------------------------------------------------------------------------
# decode


def _decoder_for(head: Mapping[str, Any]) -> Any:
    name = str(head.get("engine") or "")
    if not name:
        raise RsqError("HEAD has no engine")
    return get_engine("nlq" if name == "lossless" else name)


def decode_chunks(
    head: Mapping[str, Any],
    chunks: Mapping[str, bytes],
    *,
    threads: int | None = None,
    check: bool = True,
    warnings: list[str] | None = None,
) -> np.ndarray:
    """HEAD + decompressed chunk payloads -> original-size HxW uint16 mosaic.

    Lossless: the sha256 of the result must equal ``mosaic.sha256`` (else
    :class:`IntegrityError`).  Lossy: a ``recon_sha256`` mismatch only adds a warning.
    """
    mos = head["mosaic"]
    rec = _decoder_for(head).decode(head, chunks, threads=threads)
    oh, ow = int(mos.get("orig_height", mos["height"])), int(mos.get("orig_width", mos["width"]))
    if rec.shape != (int(mos["height"]), int(mos["width"])):
        raise RsqError(f"decoded mosaic {rec.shape} != HEAD size {(mos['height'], mos['width'])}")
    rec = rec[:oh, :ow]
    if check:
        if head.get("mode") == "lossless":
            if mos.get("sha256") and _sha256(rec) != mos["sha256"]:
                raise IntegrityError("lossless decode: mosaic sha256 mismatch (corrupted file or decoder bug)")
        elif mos.get("recon_sha256") and warnings is not None:
            if _sha256(rec) != mos["recon_sha256"]:
                warnings.append("recon_sha256 differs from the encoder's (float decode differences; advisory)")
    return rec


def decode_mosaic(rsq: RsqFile | str | os.PathLike[str] | bytes, threads: int | None = None) -> np.ndarray:
    """Spec 5.2: decode an :class:`RsqFile` (or a path / bytes) to the original-size mosaic."""
    f = rsq if isinstance(rsq, RsqFile) else read_rsq(rsq)
    return decode_chunks(f.head, f.chunks, threads=threads)


def _write_pgm16(path: Path, m: np.ndarray) -> None:
    h, w = m.shape
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n65535\n" % (w, h))
        f.write(np.ascontiguousarray(m, dtype=">u2").tobytes())


def _atomic_write(path: Path, writer: Any) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp" + path.suffix, dir=path.parent)
    os.close(fd)
    try:
        writer(Path(tmp))
        os.chmod(tmp, default_file_mode())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def decode_file(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    fmt: str = "dng",
    dng_compression: str = "lj92",
    dng_tile: int = 256,
    exif: bool = True,
    threads: int | None = None,
    extract_preview: bool = False,
    et: ExifTool | None = None,
    lens_opcode: bool | None = None,
) -> DecodeReport:
    """Decode ``src`` (.rsq) into ``dst`` (written atomically, path used verbatim).

    ``fmt``: dng (LJ92 by default, EXIF transferred from META), npy, pgm16 (P5 16-bit), tiff
    (16-bit RGB developed with the verify pipeline at 0EV, DefaultCrop applied).
    ``extract_preview``: also write stored camera JPEGs as ``<dst stem>.<Tag>.jpg``.
    ``lens_opcode``: DNG only; write the Panasonic in-camera distortion correction as an
    OpcodeList3 WarpRectilinear opcode (None = default on, env ``RAWSQUEEZE_DNG_LENS_OPCODE``).
    """
    if fmt not in DECODE_FORMATS:
        raise ValueError(f"fmt must be one of {DECODE_FORMATS}, got {fmt!r}")
    t: dict[str, float] = {}
    t0 = time.perf_counter()
    rsq = read_rsq(src)
    t["read"] = time.perf_counter() - t0
    rep = DecodeReport(src=os.fspath(src), dst=os.fspath(dst), fmt=fmt, engine=rsq.head.get("engine"),
                       mode=rsq.head.get("mode"))
    rep.warnings += rsq.warnings
    t1 = time.perf_counter()
    m = decode_chunks(rsq.head, rsq.chunks, threads=threads, warnings=rep.warnings)
    t["decode"] = time.perf_counter() - t1
    mos = rsq.head["mosaic"]
    if rep.mode == "lossless":
        rep.lossless_verified = True
    elif mos.get("recon_sha256"):
        rep.recon_match = not any("recon_sha256" in w for w in rep.warnings)
    out = Path(dst)
    t2 = time.perf_counter()
    if fmt == "dng":
        from .dng import write_dng

        meta = rsq.get("META")
        use_et = et if et is not None else (get_exiftool() if (meta and exif) else None)
        if meta and exif and use_et is None and not have("exiftool"):
            rep.warnings.append("exiftool not found: DNG written without EXIF/MakerNotes")
            exif = False
        dr = write_dng(m, rsq.head, out, compression=dng_compression, tile=dng_tile, threads=threads,
                       meta=meta, et=use_et, exif=exif, lens_opcode=lens_opcode)
        rep.lens = dr.lens
        t["dng_encode"] = dr.t_encode
        t["exif"] = dr.t_exif
        rep.warnings += dr.warnings
    elif fmt == "npy":
        _atomic_write(out, lambda p: _save_npy(p, m))
    elif fmt == "pgm16":
        _atomic_write(out, lambda p: _write_pgm16(p, m))
    else:
        import tifffile

        from .verify import develop

        img = develop(m, rsq.head, 0.0)
        _atomic_write(out, lambda p: tifffile.imwrite(p, img, photometric="rgb"))
    t["write"] = time.perf_counter() - t2
    if extract_preview:
        from .meta import restore_preview

        for fcc, tag in (("PRV0", "JpgFromRaw"), ("PRV1", "JpgFromRaw2")):
            b = rsq.get(fcc)
            if b is None:
                continue
            jp = out.with_name(f"{out.stem}.{tag}.jpg")
            jpeg = restore_preview(b)
            _atomic_write(jp, lambda p, data=jpeg: p.write_bytes(data))
            rep.previews.append(os.fspath(jp))
        if not rep.previews:
            rep.warnings.append("no stored previews (encode with --keep-preview small|full)")
    rep.out_size = out.stat().st_size
    rep.dec_s = round(time.perf_counter() - t0, 4)
    rep.timings = {k: round(v, 4) for k, v in t.items()}
    return rep


def _save_npy(p: Path, m: np.ndarray) -> None:
    with open(p, "wb") as f:
        np.save(f, m, allow_pickle=False)


# ---------------------------------------------------------------------------------------
# verify


def quick_verify(
    orig: np.ndarray,
    rec: np.ndarray,
    head: Mapping[str, Any],
    *,
    threads: int | None = None,
) -> Any:
    """``encode --verify``: 2 tiles at +3EV, no FLOOR (DESIGN.md 5.1).  Returns VerifyReport."""
    from .verify import verify

    engine = head.get("engine")
    if head.get("mode") == "lossless":
        metrics: tuple[str, ...] = ("psnr",)
    elif engine == "nlq":
        metrics = ("psnr", "noise")
    else:
        metrics = ("psnr", "ssimulacra2", "butteraugli")
    return verify(orig, rec, head, QUICK_VERIFY_EVS, QUICK_VERIFY_TILES, metrics=metrics, floor=False,
                  threads=threads)


def _raw_data_size(source_size: int, rdo: int | None) -> int | None:
    if not source_size or rdo is None or not 0 < rdo < source_size:
        return None
    return int(source_size - rdo)


def verify_file(
    rsq_path: str | os.PathLike[str],
    original_path: str | os.PathLike[str],
    *,
    evs: Sequence[float] = (0.0, 2.0, 3.0),
    tiles: int = 4,
    full: bool = False,
    metrics: Sequence[str] = ("psnr", "ssimulacra2", "butteraugli", "noise"),
    floor: bool = True,
    threads: int | None = None,
    workdir: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> Any:
    """Spec 5.2: decode ``rsq_path``, load ``original_path`` and run :func:`verify.verify`.

    ``sizes`` in the report holds bytes / ratio_file / ratio_raw / dec_s.  The original is
    checked first: if its mosaic sha256 differs from HEAD ``mosaic.sha256`` (wrong pairing)
    :class:`OriginalMismatchError` is raised before the expensive decode/develop, unless
    ``force`` (then it is only a warning).
    """
    from .verify import validate_metrics, verify

    metrics = validate_metrics(metrics)
    rsq = read_rsq(rsq_path)
    warnings: list[str] = list(rsq.warnings)
    frame = load_raw(original_path, want_exif=False)
    want = rsq.head["mosaic"].get("sha256")
    if want and frame.mosaic_sha256() != want:
        src = rsq.head.get("source") or {}
        msg = (f"original {Path(original_path).name} does not match this .rsq (encoded from "
               f"{src.get('name')!r}, mosaic sha256 {str(want)[:16]}...)")
        if not force:
            raise OriginalMismatchError(msg + "; use --force to compare anyway")
        warnings.append(msg)
    t0 = time.perf_counter()
    rec = decode_chunks(rsq.head, rsq.chunks, threads=threads, warnings=warnings)
    dec_s = time.perf_counter() - t0
    if frame.mosaic.shape != rec.shape:
        raise ValueError(f"original mosaic {frame.mosaic.shape} != decoded {rec.shape}")
    rep = verify(frame.mosaic, rec, rsq.head, evs, tiles, full=full, metrics=metrics, floor=floor,
                 threads=threads, workdir=workdir)
    src = rsq.head.get("source") or {}
    ssize = int(src.get("size") or 0)
    raw = _raw_data_size(ssize, src.get("raw_data_offset"))
    rep.sizes = {
        "bytes": rsq.file_size,
        "source_size": ssize,
        "ratio_file": ssize / rsq.file_size if ssize else None,
        "ratio_raw": raw / rsq.file_size if raw else None,
        "dec_s": round(dec_s, 4),
        "chunks": rsq.chunk_sizes(),
    }
    rep.warnings = warnings + rep.warnings
    return rep


# ---------------------------------------------------------------------------------------
# encode_file


def _meta_and_previews(
    src: Path, data: bytes, frame: RawFrame, p: EncodeParams, et: ExifTool | None, warnings: list[str]
) -> tuple[list[Chunk], dict[str, Any]]:
    from .meta import make_skeleton, preview_payloads

    chunks: list[Chunk] = []
    info: dict[str, Any] = {"strategy": "none", "previews": []}
    if p.store_meta:
        payload, info = make_skeleton(src, data, raw_data_offset=frame.raw_data_offset, et=et)
        info = dict(info)
        warnings += [f"meta: {w}" for w in info.get("warnings", [])]
        if info.get("strategy") == "exif-only":
            warnings.append("meta: raw data not located; only partial (exif-only) metadata stored")
        if payload:
            chunks.append(make_chunk("META", payload))
        info["previews_stripped"] = info.pop("previews", [])
        info["previews"] = []
    if p.keep_preview != "none":
        if not (have("cjxl") and have("djxl")):
            warnings.append("cjxl/djxl not found: previews not stored")
        else:
            pay, pinfo = preview_payloads(src, p.keep_preview, data, et=et)
            for fcc, b in pay.items():
                chunks.append(make_chunk(fcc, b))
            info["previews"] = pinfo
            if not pay:
                warnings.append("no camera JPEG previews found")
    return chunks, info


def encode_file(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    preset: str = "vl",
    engine: str = "auto",
    quality: float | None = None,
    d: float | None = None,
    f: float | None = None,
    effort: int | None = None,
    threads: int | None = None,
    keep_preview: str | None = None,
    store_meta: bool = True,
    verify: bool = False,
    fallback: bool = True,
    params: EncodeParams | None = None,
    et: ExifTool | None = None,
    **opts: Any,
) -> EncodeReport:
    """Spec 5.2: encode raw file ``src`` into ``dst`` (.rsq, atomic write).

    Either pass ``params`` (resolved :class:`EncodeParams`) or the keyword options, which
    go through :func:`presets.resolve` (extra ``opts``: dD, layout, recon, noise_model,
    snr_threshold, use_matrix, satmask, noise, recon_hash).

    ``verify``: decode in memory and run the quick verify (2 tiles, +3EV).  If an auto-chosen
    half3 fails its criteria and ``fallback`` is true, the file is re-encoded with nlq (R1).
    ``status`` is ``verify-failed`` when the final result does not meet DESIGN.md 6.4.
    """
    t_all = time.perf_counter()
    if params is None:
        params = resolve(
            dict(preset=preset, engine=engine, quality=quality, d=d, f=f, effort=effort, threads=threads,
                 keep_preview=keep_preview, store_meta=store_meta, **opts)
        )
    srcp, dstp = Path(src), Path(dst)
    rep = EncodeReport(src=os.fspath(srcp), dst=os.fspath(dstp), preset=params.preset)
    t: dict[str, float] = {}

    t0 = time.perf_counter()
    data = srcp.read_bytes()
    if et is None:
        et = get_exiftool()
    frame = load_raw(srcp, exiftool=et)
    frame.source_sha256 = hashlib.sha256(data).hexdigest()
    t["load"] = time.perf_counter() - t0
    rep.src_size = frame.source_size
    rep.raw_data_size = _raw_data_size(frame.source_size, frame.raw_data_offset)

    t0 = time.perf_counter()
    extra, meta_info = _meta_and_previews(srcp, data, frame, params, et, rep.warnings)
    t["meta"] = time.perf_counter() - t0
    del data

    if verify and not params.recon_hash:
        params = dataclasses.replace(params, recon_hash=True)  # verify needs the recon anyway
    enc = encode_frame_ex(frame, params, extra_chunks=extra, meta_info=meta_info)
    t.update({k: v for k, v in enc.timings.items()})
    rep.warnings += enc.warnings

    def serialise(e: FrameEncoding) -> bytes:
        bio = io.BytesIO()
        write_rsq(bio, e.chunks)
        return bio.getvalue()

    t0 = time.perf_counter()
    blob = serialise(enc)
    t["container"] = time.perf_counter() - t0

    if verify:
        t0 = time.perf_counter()
        vrep = _verify_encoding(frame, enc, blob, params)
        t["verify"] = time.perf_counter() - t0
        rep.warnings += [f"verify: {w}" for w in vrep.warnings]
        acc = vrep.acceptance
        rep.verify = {"engine": enc.engine, "passed": vrep.passed, "failures": acc.get("failures", []),
                      "skipped": acc.get("skipped", []), "worst": vrep.to_dict().get("worst"),
                      "noise": {k: v for k, v in (vrep.noise or {}).items() if k != "raw"}, "clip": vrep.clip}
        auto_half3 = params.engine == "auto" and enc.engine == "half3"
        if not vrep.passed and auto_half3 and fallback:
            t0 = time.perf_counter()
            half3_bytes = len(blob)
            fb_params = dataclasses.replace(params, engine="nlq")
            enc2 = encode_frame_ex(frame, fb_params, extra_chunks=extra, meta_info=meta_info, noise=enc.noise)
            enc2.head["fallback"] = {"from": "half3", "failures": acc.get("failures", [])}
            enc2.head["selection"]["requested"] = "auto"
            enc2.head["selection"]["reason"] = "fallback to nlq: half3 failed the quick verify"
            enc2.chunks[0] = make_head_chunk(enc2.head)
            blob = serialise(enc2)
            vrep2 = _verify_encoding(frame, enc2, blob, fb_params)
            rep.fallback = {"from": "half3", "to": "nlq", "half3_failures": acc.get("failures", []),
                            "half3_bytes": half3_bytes}
            rep.verify = {"engine": "nlq", "passed": vrep2.passed,
                          "failures": vrep2.acceptance.get("failures", []),
                          "skipped": vrep2.acceptance.get("skipped", []),
                          "noise": {k: v for k, v in (vrep2.noise or {}).items() if k != "raw"},
                          "clip": vrep2.clip, "half3": rep.verify}
            enc = enc2
            params = fb_params
            t["fallback"] = time.perf_counter() - t0
        if not rep.verify["passed"]:
            rep.status = "verify-failed"

    t0 = time.perf_counter()
    dstp.parent.mkdir(parents=True, exist_ok=True)
    tmp = dstp.with_name(f".{dstp.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, dstp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    t["write"] = time.perf_counter() - t0

    head = enc.head
    rep.engine = enc.engine
    rep.mode = head.get("mode")
    rep.param = head["selection"]["param"]
    rep.snr18 = enc.choice.snr18 if enc.choice else None
    rep.reason = head["selection"]["reason"]
    rep.out_size = len(blob)
    rep.ratio_file = rep.src_size / rep.out_size if rep.src_size else None
    rep.ratio_raw = rep.raw_data_size / rep.out_size if rep.raw_data_size else None
    rep.chunk_sizes = read_rsq_info(blob).chunk_sizes()
    rep.enc_s = round(time.perf_counter() - t_all, 4)
    rep.timings = {k: round(v, 4) for k, v in t.items()}
    return rep


def _verify_encoding(frame: RawFrame, enc: FrameEncoding, blob: bytes, params: EncodeParams) -> Any:
    """Quick verify of a serialised encoding (decoded from ``blob`` unless a recon exists)."""
    rsq = read_rsq(blob)
    if enc.recon is None or rsq.head.get("mode") == "lossless":  # lossless: really decode (sha256 check)
        rec = decode_chunks(rsq.head, rsq.chunks, threads=params.threads)
    else:
        rec = enc.recon
    return quick_verify(frame.mosaic, rec, rsq.head, threads=params.threads)


# ---------------------------------------------------------------------------------------
# bench plug-ins


def bench_encode_fn(frame: RawFrame, engine: str, params: dict[str, Any]) -> tuple[list[Chunk], dict[str, Any]]:
    """:mod:`bench` encoder: sweep keys go through :func:`presets.resolve` (engine forced)."""
    p = resolve(dict(params), engine=engine)
    enc = encode_frame_ex(frame, p)
    return enc.chunks, enc.head


def bench_decode_fn(head: dict[str, Any], chunks: Mapping[str, bytes]) -> np.ndarray:
    """:mod:`bench` decoder (original-size mosaic)."""
    return decode_chunks(head, chunks)


__all__ = [
    "DECODE_FORMATS",
    "DecodeReport",
    "EncodeError",
    "EncodeReport",
    "FrameEncoding",
    "IntegrityError",
    "OriginalMismatchError",
    "bench_decode_fn",
    "bench_encode_fn",
    "build_head",
    "decode_chunks",
    "decode_file",
    "decode_mosaic",
    "encode_file",
    "encode_frame",
    "encode_frame_ex",
    "get_exiftool",
    "quick_verify",
    "verify_file",
]
