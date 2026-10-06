"""Command-line interface (DESIGN.md 5.1).

::

    rawsqueeze encode IN... [-o OUT|DIR] [-r] [--preset P] [--engine E] [-q F] [--d D] [--f F] ...
    rawsqueeze decode IN.rsq... [-o OUT|DIR] [--format dng|npy|pgm16|tiff] ...
    rawsqueeze info IN.rsq... [--json]
    rawsqueeze verify IN.rsq --original IN.RW2 [--ev 0,2,3] [--tiles 4|--full] [--json]
    rawsqueeze bench IN... --sweep 'engine=half3;d=0.1,0.2,0.3' [--crop 2048] --csv out.csv

Exit codes: 0 success; 1 error (any file); 2 verify below the DESIGN.md 6.4 thresholds (or a
requested criterion could not be measured); 130 interrupted (Ctrl-C).
Batch encode/decode uses a ProcessPoolExecutor (``-j``) with ``cpu_count // jobs`` threads
per worker; progress lines go to stderr, ``--json`` writes machine-readable reports to stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VERIFY = 2
EXIT_INTERRUPTED = 130

RAW_EXTENSIONS = frozenset(
    {".rw2", ".nef", ".nrw", ".cr2", ".cr3", ".arw", ".srf", ".sr2", ".orf", ".raf", ".pef", ".dng",
     ".srw", ".rwl", ".3fr", ".iiq", ".erf", ".kdc", ".mrw", ".x3f"}
)
FORMAT_SUFFIX = {"dng": ".dng", "npy": ".npy", "pgm16": ".pgm", "tiff": ".tiff"}


# ---------------------------------------------------------------------------------------
# helpers


JOB_RAM_GB = 1.5
"""Per-file memory budget of ``default_jobs`` (measured peak: plain nlq encode 1.2 GB)."""
JOB_RAM_GB_VERIFY = 3.0
"""Budget with ``encode --verify`` (measured peak 2.8 GB RSS, PANA9831)."""


def default_jobs(verify: bool = False) -> int:
    """``max(1, min(cpu // 4, RAM_GB // budget))`` (DESIGN.md 4.2); budget 1.5 GB, 3 GB with verify."""
    cpu = os.cpu_count() or 1
    try:
        ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError, AttributeError):
        ram_gb = 4.0
    return max(1, min(cpu // 4, int(ram_gb // (JOB_RAM_GB_VERIFY if verify else JOB_RAM_GB))))


def _floats(s: str) -> list[float]:
    try:
        return [float(x) for x in s.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated numbers, got {s!r}") from None


def _err(msg: str) -> None:
    print(f"rawsqueeze: error: {msg}", file=sys.stderr)


def _mb(n: float | None) -> str:
    return "-" if n is None else f"{n / 1e6:.2f}MB"


@dataclass
class Job:
    src: Path
    dst: Path
    action: str = "run"
    """run | skip | conflict (output exists) | duplicate (another input maps to the same output)"""
    note: str = ""


_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


def _out_key(p: Path) -> str:
    """Duplicate-detection key; case-folded on (default) case-insensitive filesystems."""
    s = os.path.normcase(os.fspath(p.resolve()))
    return s.casefold() if _CASE_INSENSITIVE_FS else s


def _drop_dng_siblings(files: list[Path]) -> list[Path]:
    """Drop ``x.dng`` when a non-DNG raw ``x.*`` sits in the same folder.

    ``rawsqueeze decode`` writes ``x.dng`` next to ``x.rsq`` by default, so after a
    round-trip in one folder the decoded DNG would map to the same ``x.rsq`` as its raw.
    """
    stems = {(q.parent, q.stem.casefold()) for q in files if q.suffix.lower() != ".dng"}
    return [q for q in files if q.suffix.lower() != ".dng" or (q.parent, q.stem.casefold()) not in stems]


def plan_jobs(
    inputs: Sequence[str],
    out: str | None,
    *,
    recursive: bool,
    suffix: str,
    want: Callable[[Path], bool],
    overwrite: bool,
    skip_existing: bool,
) -> list[Job]:
    """Map inputs (files or directories) to output paths.

    Directories contribute matching files (recursively with ``-r``); with ``-o DIR`` the
    structure below the input directory is kept.  When encoding a directory, a ``x.dng``
    next to a raw ``x.*`` (a decoded copy) is ignored.  ``-o`` names a file only for a
    single input file that is not an existing directory and does not end with a separator.
    Raises ValueError on missing inputs or when the same input is given twice; two
    different inputs mapping to one output make the later one a ``duplicate`` job (a
    per-file error), so the rest of the batch still runs.
    """
    pairs: list[tuple[Path, Path]] = []  # (src, relative output name)
    any_dir = False
    for s in inputs:
        p = Path(s)
        if p.is_dir():
            any_dir = True
            it = p.rglob("*") if recursive else p.glob("*")
            files = sorted(q for q in it if q.is_file() and want(q) and not q.name.startswith("."))
            if suffix == ".rsq":
                files = _drop_dng_siblings(files)
            pairs += [(q, q.relative_to(p).with_suffix(suffix)) for q in files]
        elif p.is_file():
            pairs.append((p, Path(p.name).with_suffix(suffix)))
        else:
            raise ValueError(f"input not found: {s}")
    if out is None:
        jobs = [Job(src, src.with_suffix(suffix)) for src, _ in pairs]
    else:
        o = Path(out)
        as_dir = len(pairs) != 1 or any_dir or o.is_dir() or out.endswith(("/", os.sep))
        jobs = [Job(src, (o / rel) if as_dir else o) for src, rel in pairs]
    seen: dict[str, Path] = {}
    for j in jobs:
        key = _out_key(j.dst)
        if _out_key(j.src) == key:
            raise ValueError(f"output would overwrite the input: {j.src}")
        if key in seen:
            if _out_key(seen[key]) == _out_key(j.src):
                raise ValueError(f"input given twice, two jobs map to the same output {j.dst}: {j.src}")
            j.action = "duplicate"
            j.note = f"output {j.dst} is also the output of {seen[key]} (rename one or use -o)"
            continue
        seen[key] = j.src
        if j.dst.exists() and not overwrite:
            j.action = "skip" if skip_existing else "conflict"
    return jobs


def _plan_reports(plan: Sequence[Job]) -> list[dict[str, Any]]:
    """Reports for jobs that do not run (skip / conflict / duplicate)."""
    out: list[dict[str, Any]] = []
    for j in plan:
        if j.action == "conflict":
            out.append({"src": os.fspath(j.src), "dst": os.fspath(j.dst), "status": "error",
                        "error": "output exists (use --overwrite or --skip-existing)"})
        elif j.action == "duplicate":
            out.append({"src": os.fspath(j.src), "dst": os.fspath(j.dst), "status": "error", "error": j.note})
        elif j.action == "skip":
            out.append({"src": os.fspath(j.src), "dst": os.fspath(j.dst), "status": "skipped"})
    return out


# ---------------------------------------------------------------------------------------
# workers (top-level for pickling)


def _encode_job(job: dict[str, Any]) -> dict[str, Any]:
    from .pipeline import EncodeReport, encode_file

    t0 = time.perf_counter()
    try:
        rep = encode_file(job["src"], job["dst"], params=job["params"], verify=job["verify"],
                          fallback=job["fallback"])
        return rep.to_dict()
    except Exception as exc:  # noqa: BLE001 -- reported per file
        r = EncodeReport(src=job["src"], dst=job["dst"], status="error", error=f"{type(exc).__name__}: {exc}")
        r.enc_s = round(time.perf_counter() - t0, 4)
        return r.to_dict()


def _encode_crash(job: dict[str, Any], msg: str) -> dict[str, Any]:
    from .pipeline import EncodeReport

    return EncodeReport(src=job["src"], dst=job["dst"], status="error", error=msg).to_dict()


def _decode_crash(job: dict[str, Any], msg: str) -> dict[str, Any]:
    from .pipeline import DecodeReport

    return DecodeReport(src=job["src"], dst=job["dst"], status="error", fmt=job["fmt"], error=msg).to_dict()


def _decode_job(job: dict[str, Any]) -> dict[str, Any]:
    from .pipeline import DecodeReport, decode_file

    try:
        rep = decode_file(job["src"], job["dst"], fmt=job["fmt"], dng_compression=job["dng_compression"],
                          dng_tile=job["dng_tile"], exif=job["exif"], threads=job["threads"],
                          extract_preview=job["extract_preview"])
        return rep.to_dict()
    except Exception as exc:  # noqa: BLE001
        return DecodeReport(src=job["src"], dst=job["dst"], status="error", fmt=job["fmt"],
                            error=f"{type(exc).__name__}: {exc}").to_dict()


CRASH_MESSAGE = "worker process crashed (out of memory, or a crash inside LibRaw/libjxl?)"


def _worker_init() -> None:
    """Pool workers ignore SIGINT: Ctrl-C is handled once, by the parent (``_run_jobs``)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _new_pool(n: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_worker_init)


def _kill_pool(ex: ProcessPoolExecutor | None) -> None:
    """Cancel pending work and terminate the workers now (no waiting for running files)."""
    if ex is None:
        return
    procs = list((getattr(ex, "_processes", None) or {}).values())
    ex.shutdown(wait=False, cancel_futures=True)
    for p in procs:
        if p.is_alive():
            p.terminate()
    for p in procs:
        p.join(timeout=5)


def _run_jobs(
    fn: Callable[[dict[str, Any]], dict[str, Any]],
    jobs: list[dict[str, Any]],
    n_jobs: int,
    on_done: Callable[[int, dict[str, Any]], None],
    on_crash: Callable[[dict[str, Any], str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Run ``fn`` over ``jobs``; returns ``(results in job order, interrupted)``.

    * ``n_jobs > 1``: spawn worker pool.  If a worker dies (OOM kill, segfault) the pool
      breaks and every unfinished job fails with ``BrokenProcessPool``; those jobs are
      re-run one at a time in a fresh single-worker pool, and a job that crashes again
      gets ``on_crash(job, CRASH_MESSAGE)`` as its result.  The batch always completes.
    * Ctrl-C: pending jobs are cancelled and the workers terminated immediately; jobs
      without a result get ``status: interrupted`` and ``interrupted`` is True.
    """
    results: list[dict[str, Any]] = []

    def done(r: dict[str, Any]) -> None:
        results.append(r)
        on_done(len(results), r)

    interrupted = False
    ex: ProcessPoolExecutor | None = None
    try:
        if n_jobs <= 1 or len(jobs) <= 1:
            for j in jobs:
                done(fn(j))
        else:
            retry: list[dict[str, Any]] = []
            ex = _new_pool(n_jobs)
            futs = {ex.submit(fn, j): j for j in jobs}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except BrokenProcessPool:
                    retry.append(futs[fut])
                    continue
                done(r)
            ex.shutdown(wait=True)
            ex = None
            pos = {id(j): i for i, j in enumerate(jobs)}
            for j in sorted(retry, key=lambda j: pos[id(j)]):
                if ex is None:
                    ex = _new_pool(1)
                try:
                    r = ex.submit(fn, j).result()
                except BrokenProcessPool:
                    r = on_crash(j, CRASH_MESSAGE)
                    _kill_pool(ex)
                    ex = None
                done(r)
            if ex is not None:
                ex.shutdown(wait=True)
                ex = None
    except KeyboardInterrupt:
        interrupted = True
        _kill_pool(ex)
        have_src = {r["src"] for r in results}
        results += [{"src": j["src"], "dst": j["dst"], "status": "interrupted"} for j in jobs
                    if j["src"] not in have_src]
    order = {j["src"]: i for i, j in enumerate(jobs)}
    results.sort(key=lambda r: order.get(r["src"], 0))
    return results, interrupted


def _threads_for(args: argparse.Namespace, n_jobs: int) -> int:
    if args.threads:
        return max(1, args.threads)
    return max(1, (os.cpu_count() or 1) // max(1, n_jobs))


def _emit_json(obj: Any) -> None:
    def default(o: Any) -> Any:
        if isinstance(o, float) and math.isinf(o):
            return "inf"
        if hasattr(o, "tolist"):
            return o.tolist()
        return str(o)

    print(json.dumps(obj, indent=1, default=default))


# ---------------------------------------------------------------------------------------
# encode


def _encode_progress_line(k: int, n: int, r: dict[str, Any]) -> str:
    name = Path(r["src"]).name
    if r["status"] == "error":
        return f"[{k}/{n}] {name} ERROR {r.get('error')}"
    if r["status"] == "skipped":
        return f"[{k}/{n}] {name} skipped (exists: {r['dst']})"
    if r["status"] == "interrupted":
        return f"[{k}/{n}] {name} interrupted"
    rf = f"{r['ratio_file']:.2f}x" if r.get("ratio_file") else "-"
    rr = f" | raw {r['ratio_raw']:.2f}x" if r.get("ratio_raw") else ""
    line = (f"[{k}/{n}] {name} {_mb(r.get('src_size'))} -> {_mb(r.get('out_size'))} ({rf}{rr}) "
            f"{r.get('engine')} {r.get('param')} enc {r.get('enc_s') or 0:.1f}s")
    if r.get("fallback"):
        line += " [fallback half3->nlq]"
    v = r.get("verify")
    if v is not None:
        line += " verify " + ("PASS" if v.get("passed") else f"FAIL {v.get('failures')}")
    return line


def cmd_encode(args: argparse.Namespace) -> int:
    from .presets import resolve
    from .tools import have

    n_in_jobs = args.jobs if args.jobs else default_jobs(verify=args.verify)
    try:
        plan = plan_jobs(args.inputs, args.output, recursive=args.recursive, suffix=".rsq",
                         want=lambda q: q.suffix.lower() in RAW_EXTENSIONS,
                         overwrite=args.overwrite, skip_existing=args.skip_existing)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    if not plan:
        _err("no input raw files")
        return EXIT_ERROR
    n_jobs = max(1, min(n_in_jobs, sum(1 for j in plan if j.action == "run") or 1))
    try:
        params = resolve(
            preset=args.preset, engine=args.engine, quality=args.quality, d=args.d, f=args.f, dD=args.dD,
            effort=args.effort, layout=args.layout, recon=args.recon, noise_model=args.noise_model,
            snr_threshold=args.snr_threshold, use_matrix=not args.no_matrix, satmask=not args.no_satmask,
            threads=_threads_for(args, n_jobs), keep_preview=args.keep_preview, store_meta=not args.no_meta,
            noise=not args.no_noise, recon_hash=args.recon_hash or None,
        )
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR

    n = len(plan)
    quiet = args.quiet
    reports = _plan_reports(plan)
    failures = sum(r["status"] == "error" for r in reports)
    if args.dry_run:
        for k, j in enumerate(plan, 1):
            what = {"run": "encode", "skip": "skip (exists)", "conflict": "ERROR output exists",
                    "duplicate": f"ERROR {j.note}"}[j.action]
            print(f"[{k}/{n}] {what}: {j.src} -> {j.dst} (preset {params.preset}, engine {params.engine}, "
                  f"d {params.d:g}, f {params.f:g}, threads {params.threads}, jobs {n_jobs})", file=sys.stderr)
        if args.json:
            _emit_json([{"src": os.fspath(j.src), "dst": os.fspath(j.dst), "action": j.action} for j in plan])
        return EXIT_ERROR if failures else EXIT_OK

    if args.verify and params.engine in ("auto", "half3", "gat4"):
        missing = [t for t in ("ssimulacra2", "butteraugli_main") if not have(t)]
        if missing:
            print(f"rawsqueeze: warning: --verify: {', '.join(missing)} not found; perceptual criteria cannot be "
                  f"measured, so half3/gat4 results count as verify failures"
                  + (" (auto falls back to nlq)" if params.engine == "auto" and not args.no_fallback else ""),
                  file=sys.stderr)
    if not quiet:
        for k, r in enumerate(reports, 1):
            print(_encode_progress_line(k, n, r), file=sys.stderr)
    done0 = len(reports)
    todo = [{"src": os.fspath(j.src), "dst": os.fspath(j.dst), "params": params, "verify": args.verify,
             "fallback": not args.no_fallback} for j in plan if j.action == "run"]
    t0 = time.perf_counter()

    def on_done(k: int, r: dict[str, Any]) -> None:
        if not quiet:
            print(_encode_progress_line(done0 + k, n, r), file=sys.stderr, flush=True)

    res, interrupted = _run_jobs(_encode_job, todo, n_jobs, on_done, _encode_crash)
    reports += res
    wall = time.perf_counter() - t0
    ok = [r for r in reports if r["status"] in ("ok", "verify-failed")]
    errs = [r for r in reports if r["status"] == "error"]
    vfail = [r for r in reports if r["status"] == "verify-failed"]
    n_int = sum(r["status"] == "interrupted" for r in reports)
    tin = sum(r.get("src_size") or 0 for r in ok)
    tout = sum(r.get("out_size") or 0 for r in ok)
    if not quiet:
        ratio = f" ({tin / tout:.2f}x)" if tout else ""
        print(f"{'interrupted' if interrupted else 'done'}: {len(reports)} files ({len(ok)} encoded, "
              f"{sum(r['status'] == 'skipped' for r in reports)} skipped, {len(errs)} failed, {len(vfail)} "
              f"verify-failed{f', {n_int} not run' if n_int else ''}) {_mb(tin)} -> {_mb(tout)}{ratio} "
              f"in {wall:.1f}s (jobs {n_jobs}, threads {params.threads})", file=sys.stderr)
        for r in errs:
            _err(f"{r['src']}: {r.get('error')}")
    if args.json:
        _emit_json(reports)
    if interrupted:
        _err("interrupted")
        return EXIT_INTERRUPTED
    if errs:
        return EXIT_ERROR
    return EXIT_VERIFY if vfail else EXIT_OK


# ---------------------------------------------------------------------------------------
# decode


def _decode_progress_line(k: int, n: int, r: dict[str, Any]) -> str:
    name = Path(r["src"]).name
    if r["status"] == "error":
        return f"[{k}/{n}] {name} ERROR {r.get('error')}"
    if r["status"] == "skipped":
        return f"[{k}/{n}] {name} skipped (exists: {r['dst']})"
    if r["status"] == "interrupted":
        return f"[{k}/{n}] {name} interrupted"
    extra = " lossless-verified" if r.get("lossless_verified") else ""
    return (f"[{k}/{n}] {name} -> {Path(r['dst']).name} {_mb(r.get('out_size'))} {r.get('engine')} "
            f"{r.get('mode')} dec {r.get('dec_s') or 0:.2f}s{extra}")


def cmd_decode(args: argparse.Namespace) -> int:
    n_in_jobs = args.jobs if args.jobs else default_jobs()
    try:
        plan = plan_jobs(args.inputs, args.output, recursive=args.recursive, suffix=FORMAT_SUFFIX[args.format],
                         want=lambda q: q.suffix.lower() == ".rsq", overwrite=args.overwrite,
                         skip_existing=args.skip_existing)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    if not plan:
        _err("no input .rsq files")
        return EXIT_ERROR
    n_jobs = max(1, min(n_in_jobs, sum(1 for j in plan if j.action == "run") or 1))
    threads = _threads_for(args, n_jobs)
    n = len(plan)
    reports = _plan_reports(plan)
    if args.dry_run:
        for k, j in enumerate(plan, 1):
            print(f"[{k}/{n}] {j.action}: {j.src} -> {j.dst} ({args.format}){' ' + j.note if j.note else ''}",
                  file=sys.stderr)
        return EXIT_ERROR if any(j.action in ("conflict", "duplicate") for j in plan) else EXIT_OK
    if not args.quiet:
        for k, r in enumerate(reports, 1):
            print(_decode_progress_line(k, n, r), file=sys.stderr)
    done0 = len(reports)
    todo = [{"src": os.fspath(j.src), "dst": os.fspath(j.dst), "fmt": args.format,
             "dng_compression": args.dng_compression, "dng_tile": args.dng_tile, "exif": not args.no_exif,
             "threads": threads, "extract_preview": args.extract_preview} for j in plan if j.action == "run"]
    t0 = time.perf_counter()

    def on_done(k: int, r: dict[str, Any]) -> None:
        if not args.quiet:
            print(_decode_progress_line(done0 + k, n, r), file=sys.stderr, flush=True)

    res, interrupted = _run_jobs(_decode_job, todo, n_jobs, on_done, _decode_crash)
    reports += res
    errs = [r for r in reports if r["status"] == "error"]
    if not args.quiet:
        ok = sum(r["status"] == "ok" for r in reports)
        n_int = sum(r["status"] == "interrupted" for r in reports)
        print(f"{'interrupted' if interrupted else 'done'}: {len(reports)} files ({ok} decoded, "
              f"{sum(r['status'] == 'skipped' for r in reports)} skipped, {len(errs)} failed"
              f"{f', {n_int} not run' if n_int else ''}) in {time.perf_counter() - t0:.1f}s "
              f"(jobs {n_jobs}, threads {threads})", file=sys.stderr)
        for r in errs:
            _err(f"{r['src']}: {r.get('error')}")
    if args.json:
        _emit_json(reports)
    if interrupted:
        _err("interrupted")
        return EXIT_INTERRUPTED
    return EXIT_ERROR if errs else EXIT_OK


# ---------------------------------------------------------------------------------------
# info


def _head_lines(head: dict[str, Any]) -> list[str]:
    lines = []
    for k, v in head.items():
        lines.append(f"  {k}: {json.dumps(v, separators=(', ', ': '))}")
    return lines


def cmd_info(args: argparse.Namespace) -> int:
    from .container import RsqError, read_rsq_info

    rc = EXIT_OK
    out: list[dict[str, Any]] = []
    for s in args.inputs:
        try:
            f = read_rsq_info(s, verify_crc=True)
        except (RsqError, OSError) as exc:
            _err(f"{s}: {exc}")
            rc = EXIT_ERROR
            continue
        bad = [e.fourcc for e in f.entries if e.crc_ok is False]
        if bad:
            rc = EXIT_ERROR
        h = f.head
        mos = h.get("mosaic") or {}
        src = h.get("source") or {}
        if args.json:
            out.append({
                "path": s, "file_size": f.file_size, "version": list(f.version), "head": h,
                "chunks": [{"fourcc": e.fourcc, "offset": e.offset, "length": e.length, "flags": e.flags,
                            "critical": e.critical, "zstd": e.zstd, "crc_ok": e.crc_ok} for e in f.entries],
                "warnings": f.warnings, "index_rebuilt": f.index_rebuilt, "crc_ok": not bad,
            })
            continue
        ssize = int(src.get("size") or 0)
        ratio = f" ({ssize / f.file_size:.2f}x vs {src.get('name')})" if ssize else ""
        print(f"{s}: rsq v{f.version[0]}.{f.version[1]}, {f.file_size:,} bytes{ratio}")
        print(f"  {h.get('engine')} {h.get('mode')} preset={h.get('preset')} "
              f"{(h.get('selection') or {}).get('param', '')}  mosaic {mos.get('orig_width')}x{mos.get('orig_height')} "
              f"{src.get('make')} {src.get('model')} ISO {src.get('iso')}")
        print("HEAD:")
        print("\n".join(_head_lines(h)))
        print("chunks:")
        print(f"  {'fourcc':6} {'offset':>12} {'stored':>12}  flags  crc")
        for e in f.entries:
            fl = ("C" if e.critical else "-") + ("Z" if e.zstd else "-")
            crc = {True: "ok", False: "BAD", None: "-"}[e.crc_ok]
            print(f"  {e.fourcc:6} {e.offset:>12,} {e.length:>12,}  {fl:5}  {crc}")
        if f.index_rebuilt:
            print("  (index rebuilt by sequential scan)")
        for w in f.warnings:
            print(f"warning: {w}")
        print(f"CRC: {'all ok' if not bad else 'FAILED in ' + ', '.join(bad)}")
    if args.json:
        _emit_json(out if len(out) != 1 else out[0])
    return rc


# ---------------------------------------------------------------------------------------
# verify


def cmd_verify(args: argparse.Namespace) -> int:
    from .container import RsqError
    from .pipeline import verify_file
    from .verify import format_report, validate_metrics

    try:
        metrics = validate_metrics(args.metrics.split(","))
        if not args.ev:
            raise ValueError("--ev needs at least one EV value")
        if not args.full and args.tiles < 1:
            raise ValueError(f"--tiles must be >= 1, got {args.tiles}")
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR
    try:
        rep = verify_file(args.input, args.original, evs=args.ev, tiles=args.tiles, full=args.full,
                          metrics=metrics, floor=not args.no_floor, threads=args.threads, workdir=args.workdir,
                          force=args.force)
    except (RsqError, OSError, ValueError) as exc:
        _err(f"{args.input}: {exc}")
        return EXIT_ERROR
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(rep.to_json() + "\n")
    if args.json:
        print(rep.to_json())
    else:
        s = rep.sizes
        rf = f"{s['ratio_file']:.2f}x" if s.get("ratio_file") else "-"
        rr = f"{s['ratio_raw']:.2f}x" if s.get("ratio_raw") else "-"
        print(f"{args.input}: {s.get('bytes', 0):,} bytes (file {rf}, raw {rr}), decode {s.get('dec_s')}s")
        print(format_report(rep))
        for w in rep.warnings:
            print(f"warning: {w}")
    return EXIT_OK if rep.passed else EXIT_VERIFY


# ---------------------------------------------------------------------------------------
# bench


def cmd_bench(args: argparse.Namespace) -> int:
    from .bench import run_sweep, write_csv
    from .pipeline import bench_decode_fn, bench_encode_fn

    try:
        rows = run_sweep(
            args.inputs, args.sweep, args.crop, _floats(args.ev), encode_fn=bench_encode_fn,
            decode_fn=bench_decode_fn, tiles=args.tiles, metrics=[m for m in args.metrics.split(",") if m],
            floor=not args.no_floor, threads=args.threads,
        )
    except (ValueError, OSError) as exc:
        _err(str(exc))
        return EXIT_ERROR
    p = write_csv(rows, args.csv)
    print(f"wrote {len(rows)} rows to {p}", file=sys.stderr)
    return EXIT_ERROR if any("error" in r for r in rows) else EXIT_OK


# ---------------------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    from .bench import add_bench_arguments
    from .presets import ENGINE_CHOICES, KEEP_PREVIEW_CHOICES, LAYOUT_CHOICES, PRESETS, RECON_CHOICES

    p = argparse.ArgumentParser(
        prog="rawsqueeze",
        description="Adaptive dual-engine camera RAW compressor (.rsq container, JPEG XL payloads, DNG output).",
        epilog="Exit codes: 0 ok, 1 error, 2 verify below threshold or not measurable, 130 interrupted.",
    )
    p.add_argument("--version", action="version", version=f"rawsqueeze {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    # encode
    e = sub.add_parser("encode", help="raw file(s) -> .rsq", description="Encode camera raw files into .rsq.")
    e.add_argument("inputs", nargs="+", metavar="IN", help="raw files or directories")
    e.add_argument("-o", "--output", metavar="OUT|DIR", help="output file (single input) or directory")
    e.add_argument("-r", "--recursive", action="store_true", help="recurse into input directories")
    e.add_argument("--preset", default="vl", choices=list(PRESETS), help="quality preset (default vl)")
    e.add_argument("--engine", default="auto", choices=list(ENGINE_CHOICES), help="engine (default auto)")
    e.add_argument("-q", "--quality", type=float, help="engine-relative quality (d for half3/gat4, f for nlq); "
                   "needs a forced --engine")
    e.add_argument("--d", type=float, help="half3/gat4 JXL distance (0.05-0.45)")
    e.add_argument("--f", type=float, help="nlq step in noise sigmas (0 = lossless, 0.25-4)")
    e.add_argument("--dD", type=float, help="half3 distance of the G1-G2 plane (default = d)")
    e.add_argument("--effort", type=int, help="JXL effort 1-9 (default: nlq 3, half3/gat4 5)")
    e.add_argument("--layout", choices=list(LAYOUT_CHOICES), help="nlq/lossless layout (default from effort)")
    e.add_argument("--recon", default=None, choices=list(RECON_CHOICES), help="nlq LUT (default auto)")
    e.add_argument("--noise-model", default=None, help="auto | auto+iso_cap | manual:G,S2 (default auto+iso_cap)")
    e.add_argument("--snr-threshold", type=float, default=None, help="auto engine SNR18 threshold (default 60)")
    e.add_argument("--no-matrix", action="store_true", help="half3: WB only, no camera->sRGB matrix")
    e.add_argument("--no-satmask", action="store_true", help="half3/gat4: no saturation mask (debug)")
    e.add_argument("--no-noise", action="store_true", help="forced half3: skip the noise estimate")
    e.add_argument("--recon-hash", action="store_true", help="half3/gat4: store recon_sha256 (extra decode)")
    e.add_argument("--threads", type=int, help="threads per file (default cpu_count // jobs)")
    e.add_argument("-j", "--jobs", type=int, help="parallel files (default min(cpu/4, RAM_GB/1.5))")
    e.add_argument("--keep-preview", default=None, choices=list(KEEP_PREVIEW_CHOICES),
                   help="store camera JPEG(s) (default none; archival: small)")
    e.add_argument("--no-meta", action="store_true", help="do not store the metadata skeleton")
    e.add_argument("--verify", action="store_true", help="quick verify after encoding (exit 2 if below threshold)")
    e.add_argument("--no-fallback", action="store_true", help="with --verify: do not fall back half3 -> nlq")
    g = e.add_mutually_exclusive_group()
    g.add_argument("--overwrite", action="store_true", help="replace existing outputs")
    g.add_argument("--skip-existing", action="store_true", help="skip inputs whose output exists")
    e.add_argument("--dry-run", action="store_true", help="show what would be done")
    e.add_argument("--json", action="store_true", help="print JSON reports to stdout")
    e.add_argument("--quiet", action="store_true", help="no progress lines")
    e.set_defaults(func=cmd_encode)

    # decode
    d = sub.add_parser("decode", help=".rsq -> DNG (or npy/pgm16/tiff)", description="Decode .rsq files.")
    d.add_argument("inputs", nargs="+", metavar="IN.rsq", help=".rsq files or directories")
    d.add_argument("-o", "--output", metavar="OUT|DIR", help="output file (single input) or directory")
    d.add_argument("-r", "--recursive", action="store_true", help="recurse into input directories")
    d.add_argument("--format", default="dng", choices=list(FORMAT_SUFFIX), help="output format (default dng)")
    d.add_argument("--dng-compression", default="lj92", choices=["lj92", "none12", "none16"])
    d.add_argument("--dng-tile", type=int, default=256, help="DNG tile size, multiple of 16 (default 256)")
    d.add_argument("--no-exif", action="store_true", help="do not transfer EXIF/MakerNotes into the DNG")
    d.add_argument("--extract-preview", action="store_true", help="also write stored camera JPEGs")
    d.add_argument("--threads", type=int, help="threads per file (default cpu_count // jobs)")
    d.add_argument("-j", "--jobs", type=int, help="parallel files")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--overwrite", action="store_true")
    g.add_argument("--skip-existing", action="store_true")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--json", action="store_true", help="print JSON reports to stdout")
    d.add_argument("--quiet", action="store_true", help="no progress lines")
    d.set_defaults(func=cmd_decode)

    # info
    i = sub.add_parser("info", help="HEAD, chunk table and CRC check", description="Inspect .rsq files.")
    i.add_argument("inputs", nargs="+", metavar="IN.rsq")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_info)

    # verify
    v = sub.add_parser("verify", help="compare a .rsq against its original raw",
                       description="Develop original and reconstruction identically and compare (DESIGN.md 6).")
    v.add_argument("input", metavar="IN.rsq")
    v.add_argument("--original", required=True, metavar="IN.RW2")
    v.add_argument("--ev", type=_floats, default=[0.0, 2.0, 3.0], help="comma-separated EVs (default 0,2,3)")
    gv = v.add_mutually_exclusive_group()
    gv.add_argument("--tiles", type=int, default=4, help="number of 2048^2 tiles (default 4)")
    gv.add_argument("--full", action="store_true", help="whole image instead of tiles")
    v.add_argument("--metrics", default="psnr,ssimulacra2,butteraugli,noise",
                   help="comma-separated subset of psnr,ssimulacra2,butteraugli,noise")
    v.add_argument("--no-floor", action="store_true", help="skip the FLOOR control")
    v.add_argument("--threads", type=int)
    v.add_argument("--workdir", help="keep PPMs here (default: temp dir)")
    v.add_argument("--report", help="also write the JSON report to this path")
    v.add_argument("--json", action="store_true")
    v.add_argument("--force", action="store_true",
                   help="compare even if the original's mosaic sha256 does not match the .rsq HEAD")
    v.set_defaults(func=cmd_verify)

    # bench
    b = sub.add_parser("bench", help="engine x parameter sweep -> CSV", description="Benchmark sweeps (DESIGN.md 6.3).")
    add_bench_arguments(b)
    b.set_defaults(func=cmd_bench)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
