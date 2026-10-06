"""Benchmark harness: engine x parameter sweeps with size, speed and verify metrics (DESIGN.md 6.3).

The harness does not import the engines.  The integrator plugs them in through two
callables::

    encode_fn(frame: RawFrame, engine: str, params: dict) -> (chunks, head)
        chunks: list[container.Chunk] (HEAD optional) or {fourcc: payload}
        head:   the HEAD dict
    decode_fn(head: dict, chunks: Mapping[str, bytes]) -> np.ndarray   # HxW uint16

Each encode result is serialised into a real ``.rsq`` (``container.write_rsq``) to measure
``bytes`` and read back (``container.read_rsq``) before decoding, so the decoder sees
exactly what a file would give it.

Sweep grammar: ``key=v1,v2;key2=v3`` gives the Cartesian product, e.g.
``engine=half3;d=0.1,0.2,0.3``.  Several independent sweeps can be joined with ``|`` (or
passed as a list): ``engine=half3;d=0.2|engine=nlq;f=1,2``.  Values are parsed as int,
float, bool (true/false) or kept as strings.

CSV columns (one row per file x config x EV)::

    file,iso,engine,param,effort,bytes,ratio_file,ratio_raw,enc_s,dec_s,ev,ss2,ss2_ds2,
    ba_max,ba_p3,psnr,bias8,noise_ratio,floor_ss2

``bias8``/``noise_ratio`` are +3EV measurements and are filled on the ``ev == 3`` row only.
With ``--crop`` the ratios compare against the source size scaled by the crop's area
fraction.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import io
import itertools
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .container import make_chunk, make_head_chunk, read_rsq, write_rsq
from .rawio import RawFrame, crop_frame, load_raw
from .verify import DEFAULT_EVS, DEFAULT_METRICS, VerifyReport, validate_metrics, verify

EncodeFn = Callable[[RawFrame, str, dict[str, Any]], tuple[Any, dict[str, Any]]]
DecodeFn = Callable[[dict[str, Any], Mapping[str, bytes]], np.ndarray]

CSV_COLUMNS: tuple[str, ...] = (
    "file", "iso", "engine", "param", "effort", "bytes", "ratio_file", "ratio_raw", "enc_s", "dec_s",
    "ev", "ss2", "ss2_ds2", "ba_max", "ba_p3", "psnr", "bias8", "noise_ratio", "floor_ss2",
)


# ---------------------------------------------------------------------------------------
# sweep grammar


def _parse_value(s: str) -> Any:
    t = s.strip()
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def parse_sweep(spec: str | Sequence[str]) -> list[dict[str, Any]]:
    """Parse a sweep spec into a list of config dicts (Cartesian product per sweep).

    >>> parse_sweep("engine=half3;d=0.1,0.2")
    [{'engine': 'half3', 'd': 0.1}, {'engine': 'half3', 'd': 0.2}]
    """
    specs = [spec] if isinstance(spec, str) else list(spec)
    parts: list[str] = []
    for s in specs:
        parts += [p for p in s.split("|") if p.strip()]
    out: list[dict[str, Any]] = []
    for part in parts:
        keys: list[str] = []
        vals: list[list[Any]] = []
        for item in part.split(";"):
            item = item.strip()
            if not item:
                continue
            if "=" not in item:
                raise ValueError(f"bad sweep item {item!r} (expected key=v1,v2)")
            k, v = item.split("=", 1)
            k = k.strip()
            if not k or k in keys:
                raise ValueError(f"empty or duplicate sweep key {k!r}")
            values = [_parse_value(x) for x in v.split(",") if x.strip()]
            if not values:
                raise ValueError(f"no values for sweep key {k!r}")
            keys.append(k)
            vals.append(values)
        for combo in itertools.product(*vals):
            out.append(dict(zip(keys, combo)))
    if not out:
        raise ValueError("empty sweep")
    return out


def param_string(cfg: Mapping[str, Any]) -> str:
    """Config without engine/effort as ``k=v;k2=v2`` (the CSV ``param`` column)."""
    return ";".join(f"{k}={v}" for k, v in cfg.items() if k not in ("engine", "effort"))


# ---------------------------------------------------------------------------------------
# helpers


def center_crop(frame: RawFrame, size: int | tuple[int, int]) -> RawFrame:
    """Centre crop of ``size`` (int or (h, w)); origin aligned to even (CFA phase kept)."""
    h, w = (size, size) if isinstance(size, int) else size
    h, w = min(h, frame.height), min(w, frame.width)
    top = ((frame.height - h) // 2) & ~1
    left = ((frame.width - w) // 2) & ~1
    return crop_frame(frame, top, left, h, w)


def to_rsq_bytes(chunks: Any, head: Mapping[str, Any]) -> bytes:
    """Serialise encode output (list of Chunk or {fourcc: bytes}) + HEAD into .rsq bytes."""
    if isinstance(chunks, Mapping):
        lst = [make_chunk(k, v) for k, v in chunks.items() if k not in ("HEAD", "INDX")]
    else:
        lst = [c for c in chunks if c.name != "INDX"]
    if not lst or lst[0].name != "HEAD":
        lst = [c for c in lst if c.name != "HEAD"]
        lst.insert(0, make_head_chunk(dict(head)))
    bio = io.BytesIO()
    write_rsq(bio, lst)
    return bio.getvalue()


def _fmt(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isinf(v):
            return "inf"
        if math.isnan(v):
            return ""
        return f"{v:.6g}"
    return v


def _effort_of(cfg: Mapping[str, Any], head: Mapping[str, Any]) -> Any:
    if cfg.get("effort") is not None:
        return cfg["effort"]
    codec = head.get("codec") or {}
    return codec.get("effort")


# ---------------------------------------------------------------------------------------
# main loop


def run_sweep(
    files: Iterable[str | os.PathLike[str] | RawFrame],
    sweep: str | Sequence[str] | Sequence[Mapping[str, Any]],
    crop: int | tuple[int, int] | None = None,
    evs: Sequence[float] = DEFAULT_EVS,
    *,
    encode_fn: EncodeFn,
    decode_fn: DecodeFn,
    tiles: int = 4,
    metrics: Sequence[str] = DEFAULT_METRICS,
    floor: bool = True,
    threads: int | None = None,
    log: Callable[[str], None] | None = None,
    reports: list[VerifyReport] | None = None,
) -> list[dict[str, Any]]:
    """Run ``sweep`` on each file; return CSV-ready row dicts (see :data:`CSV_COLUMNS`).

    ``files``: raw paths (loaded with :func:`rawio.load_raw`) or RawFrame objects.
    ``crop``: centre crop size (even-aligned) encoded instead of the full mosaic; verify
    tiles are capped at the crop size.  FLOOR is computed once per file (first config) and
    repeated on all rows.  Failed configs produce rows with an ``error`` key and no
    metrics.  ``reports`` (optional list) receives each VerifyReport.
    """
    cfgs = [dict(c) for c in sweep] if (not isinstance(sweep, str) and sweep and isinstance(sweep[0], Mapping)) else parse_sweep(sweep)  # type: ignore[arg-type]
    metrics = validate_metrics(metrics)
    say = log or (lambda s: print(s, file=sys.stderr))
    rows: list[dict[str, Any]] = []
    files = list(files)
    for fi, src in enumerate(files):
        try:
            frame = src if isinstance(src, RawFrame) else load_raw(src)
        except (ValueError, OSError) as exc:  # a bad input must not stop the sweep
            name = Path(os.fspath(src)).name
            say(f"[bench] {name}: ERROR {exc}")
            rows += [{"file": name, "engine": str(c.get("engine", "auto")), "param": param_string(c),
                      "effort": c.get("effort"), "error": str(exc)} for c in cfgs]
            continue
        # Ratios need the full-sensor area; a frame that is already a crop of its source
        # (crop_origin != (0, 0)) has unknown full area -> ratios are left empty.
        known_full = not frame.is_cropped
        full_area = frame.height * frame.width
        if crop:
            frame = center_crop(frame, crop)
        area_frac = frame.height * frame.width / full_area
        src_bytes = frame.source_size * area_frac if (frame.source_size and known_full) else None
        raw_bytes = (
            (frame.source_size - frame.raw_data_offset) * area_frac
            if frame.source_size and frame.raw_data_offset is not None and known_full
            else None
        )
        tsize = min(2048, frame.height, frame.width)
        floor_ss2: dict[float, float | None] = {}
        for ci, cfg in enumerate(cfgs):
            cfg = dict(cfg)
            engine = str(cfg.get("engine", "auto"))
            params = {k: v for k, v in cfg.items() if k != "engine"}
            base = {
                "file": frame.source_name,
                "iso": frame.iso,
                "engine": engine,
                "param": param_string(cfg),
                "effort": cfg.get("effort"),
            }
            try:
                t0 = time.perf_counter()
                chunks, head = encode_fn(frame, engine, dict(params))
                enc_s = time.perf_counter() - t0
                blob = to_rsq_bytes(chunks, head)
                rsq = read_rsq(blob)
                t1 = time.perf_counter()
                rec = decode_fn(rsq.head, rsq.chunks)
                dec_s = time.perf_counter() - t1
                rec = np.asarray(rec)[: frame.height, : frame.width]
                if rec.shape != frame.mosaic.shape:
                    raise ValueError(f"decoded shape {rec.shape} != {frame.mosaic.shape}")
                need_floor = floor and not floor_ss2
                vhead = dict(rsq.head)
                for k, v in frame.head_sections().items():
                    vhead.setdefault(k, v)
                rep = verify(
                    frame.mosaic, rec, vhead, evs, tiles, metrics=metrics, floor=need_floor,
                    tile_size=tsize, threads=threads, engine=rsq.head.get("engine", engine),
                )
                if reports is not None:
                    reports.append(rep)
                if need_floor and rep.floor:
                    floor_ss2 = {ev: m.ss2 for ev, m in rep.floor.items()}
            except Exception as exc:  # noqa: BLE001 -- a failing config must not stop the sweep
                say(f"[bench] {frame.source_name} {engine} {param_string(cfg)}: ERROR {exc!r}")
                rows.append({**base, "error": repr(exc)})
                continue
            nbytes = len(blob)
            common = {
                **base,
                "engine": rsq.head.get("engine", engine),
                "effort": _effort_of(cfg, rsq.head),
                "bytes": nbytes,
                "ratio_file": (src_bytes / nbytes) if src_bytes else None,
                "ratio_raw": (raw_bytes / nbytes) if raw_bytes else None,
                "enc_s": enc_s,
                "dec_s": dec_s,
            }
            for ev in rep.evs:
                w = rep.worst[ev]
                row = {
                    **common,
                    "ev": ev,
                    "ss2": w.ss2,
                    "ss2_ds2": w.ss2_ds2,
                    "ba_max": w.ba_max,
                    "ba_p3": w.ba_p3,
                    "psnr": w.psnr,
                    "bias8": _ev3(rep, "bias8") if ev == 3.0 else None,
                    "noise_ratio": rep.noise.get("noise_ratio_max") if ev == 3.0 else None,
                    "floor_ss2": floor_ss2.get(ev),
                }
                rows.append(row)
            say(
                f"[bench {fi + 1}/{len(files)} cfg {ci + 1}/{len(cfgs)}] {frame.source_name} {common['engine']} "
                f"{param_string(cfg)}: {nbytes / 1e6:.3f} MB"
                + (f" ({common['ratio_file']:.2f}x)" if common["ratio_file"] else "")
                + f" enc {enc_s:.2f}s dec {dec_s:.2f}s "
                + " ".join(f"{ev:+g}EV ss2 {_fmt(rep.worst[ev].ss2)}" for ev in rep.evs)
            )
    return rows


def _ev3(rep: VerifyReport, key: str) -> float | None:
    """Signed value with the largest magnitude over the +3EV tiles."""
    vals = [v.get(key) for v in (rep.noise.get("ev3") or {}).values() if v.get(key) is not None]
    return max(vals, key=abs) if vals else None


def write_csv(rows: Sequence[Mapping[str, Any]], path: str | os.PathLike[str]) -> Path:
    """Write rows with :data:`CSV_COLUMNS` (plus ``error`` if any row has one)."""
    cols = list(CSV_COLUMNS) + (["error"] if any("error" in r for r in rows) else [])
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: _fmt(r.get(c)) for c in cols})
    return p


# ---------------------------------------------------------------------------------------
# command line (python -m rawsqueeze.bench)


def load_plugin(spec: str) -> tuple[EncodeFn, DecodeFn]:
    """Load ``module:attr``; ``attr`` is either a factory returning ``(encode_fn, decode_fn)``
    or a module-level object/namespace with ``encode_fn``/``decode_fn`` attributes."""
    mod_name, _, attr = spec.partition(":")
    mod = importlib.import_module(mod_name)
    obj = getattr(mod, attr) if attr else mod
    if hasattr(obj, "encode_fn") and hasattr(obj, "decode_fn"):
        return obj.encode_fn, obj.decode_fn
    if callable(obj):
        enc, dec = obj()
        return enc, dec
    raise ValueError(f"plugin {spec!r} provides no encode_fn/decode_fn")


def add_bench_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Arguments of ``rawsqueeze bench`` (for reuse by cli.py)."""
    p.add_argument("inputs", nargs="+", help="raw files")
    p.add_argument("--sweep", action="append", required=True, help="e.g. 'engine=half3;d=0.1,0.2,0.3' (repeatable, '|' joins)")
    p.add_argument("--crop", type=int, default=None, help="encode a centred crop of this size")
    p.add_argument("--ev", default="0,2,3", help="comma-separated EVs (default 0,2,3)")
    p.add_argument("--tiles", type=int, default=4)
    p.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    p.add_argument("--no-floor", action="store_true")
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--csv", required=True, help="output CSV path")
    return p


def main(argv: Sequence[str] | None = None, *, encode_fn: EncodeFn | None = None, decode_fn: DecodeFn | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rawsqueeze.bench", description="rawsqueeze sweep benchmark")
    add_bench_arguments(p)
    p.add_argument("--plugin", default=None, help="module:attr providing encode_fn/decode_fn")
    a = p.parse_args(argv)
    if encode_fn is None or decode_fn is None:
        if not a.plugin:
            p.error("--plugin module:attr is required (no encoder registered)")
        encode_fn, decode_fn = load_plugin(a.plugin)
    rows = run_sweep(
        a.inputs, a.sweep, a.crop, [float(x) for x in a.ev.split(",") if x.strip()],
        encode_fn=encode_fn, decode_fn=decode_fn, tiles=a.tiles,
        metrics=[m for m in a.metrics.split(",") if m], floor=not a.no_floor, threads=a.threads,
    )
    write_csv(rows, a.csv)
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CSV_COLUMNS",
    "DecodeFn",
    "EncodeFn",
    "add_bench_arguments",
    "center_crop",
    "load_plugin",
    "main",
    "param_string",
    "parse_sweep",
    "run_sweep",
    "to_rsq_bytes",
    "write_csv",
]
