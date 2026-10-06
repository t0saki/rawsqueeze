"""CLI tests (DESIGN.md 5.1, test plan 12) on 512x512 fixture DNGs (no samples needed)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from rawsqueeze.cli import EXIT_ERROR, EXIT_OK, EXIT_VERIFY, build_parser, main, plan_jobs
from rawsqueeze.dng import write_dng
from rawsqueeze.tools import have

FIX = Path(__file__).resolve().parent / "fixtures"
STEMS = {"iso100": "p1060444_iso100_512", "iso4000": "p1037920_iso4000_512"}


@pytest.fixture(scope="session")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Fixture frames written as LJ92 DNGs (rawpy-readable raw inputs for the CLI)."""
    from rawsqueeze.rawio import load_frame_npz

    d = tmp_path_factory.mktemp("raws")
    for name, stem in STEMS.items():
        fr = load_frame_npz(FIX / stem)
        write_dng(fr.mosaic, fr.head_sections(), d / f"{name}.dng", exif=False)
    return d


@pytest.fixture
def raws(raw_dir: Path, tmp_path: Path) -> Path:
    d = tmp_path / "in"
    shutil.copytree(raw_dir, d)
    return d


def _json_out(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "encode" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    p = build_parser()
    for cmd in ("encode", "decode", "info", "verify", "bench"):
        assert cmd in p.format_help()


def test_encode_decode_info_roundtrip(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import rawpy

    out = tmp_path / "rsq"
    assert main(["encode", str(raws / "iso100.dng"), str(raws / "iso4000.dng"), "-o", str(out), "--json", "-j", "1"]) == EXIT_OK
    cap = capsys.readouterr()
    reps = json.loads(cap.out)
    assert [Path(r["dst"]).name for r in reps] == ["iso100.rsq", "iso4000.rsq"]
    assert all(r["status"] == "ok" and r["out_size"] > 0 for r in reps)
    assert {r["engine"] for r in reps} <= {"half3", "nlq"}
    assert "[1/2]" in cap.err and "[2/2]" in cap.err and "done: 2 files" in cap.err
    assert (out / "iso100.rsq").stat().st_mode & 0o777 != 0o600

    dec = tmp_path / "dng"
    assert main(["decode", str(out / "iso100.rsq"), str(out / "iso4000.rsq"), "-o", str(dec), "-j", "1"]) == EXIT_OK
    for n in ("iso100", "iso4000"):
        with rawpy.imread(str(dec / f"{n}.dng")) as r:
            assert r.raw_image.shape == (512, 512)

    assert main(["info", str(out / "iso4000.rsq")]) == EXIT_OK
    txt = capsys.readouterr().out
    assert "CRC: all ok" in txt and "HEAD" in txt and "PLN0" in txt
    assert main(["info", str(out / "iso4000.rsq"), "--json"]) == EXIT_OK
    info = _json_out(capsys)
    assert info["crc_ok"] and info["head"]["engine"] == "nlq"
    assert [c["fourcc"] for c in info["chunks"]][0] == "HEAD"


def test_lossless_is_bit_exact_through_dng(raws: Path, tmp_path: Path) -> None:
    import rawpy

    src = raws / "iso100.dng"
    rsq, dng = tmp_path / "a.rsq", tmp_path / "a.dng"
    assert main(["encode", str(src), "-o", str(rsq), "--preset", "lossless", "--quiet"]) == EXIT_OK
    assert main(["decode", str(rsq), "-o", str(dng), "--quiet"]) == EXIT_OK
    with rawpy.imread(str(src)) as a, rawpy.imread(str(dng)) as b:
        assert np.array_equal(a.raw_image, b.raw_image)


def test_existing_outputs(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "o"
    args = ["encode", str(raws), "-o", str(out), "--preset", "lossless", "--quiet"]
    assert main(args) == EXIT_OK
    assert sorted(p.name for p in out.iterdir()) == ["iso100.rsq", "iso4000.rsq"]
    mtime = (out / "iso100.rsq").stat().st_mtime_ns
    assert main(args) == EXIT_ERROR  # outputs exist
    assert main([*args, "--skip-existing"]) == EXIT_OK
    assert (out / "iso100.rsq").stat().st_mtime_ns == mtime
    assert main([*args, "--overwrite"]) == EXIT_OK
    capsys.readouterr()
    assert main([*args, "--skip-existing", "--json"]) == EXIT_OK
    assert {r["status"] for r in _json_out(capsys)} == {"skipped"}


def test_dry_run_writes_nothing(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "dry"
    assert main(["encode", str(raws), "-o", str(out), "--dry-run"]) == EXIT_OK
    assert not out.exists()
    assert "encode:" in capsys.readouterr().err


def test_bad_arguments(raws: Path, tmp_path: Path) -> None:
    assert main(["encode", str(raws / "iso100.dng"), "-o", str(tmp_path / "x.rsq"), "-q", "0.3"]) == EXIT_ERROR
    assert main(["encode", str(tmp_path / "missing.RW2")]) == EXIT_ERROR
    assert main(["decode", str(tmp_path / "missing.rsq")]) == EXIT_ERROR
    assert main(["info", str(raws / "iso100.dng")]) == EXIT_ERROR  # not an rsq


def test_batch_jobs_2(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "b"
    assert main(["encode", str(raws), "-o", str(out), "-j", "2", "--json"]) == EXIT_OK
    cap = capsys.readouterr()
    reps = json.loads(cap.out)
    assert len(reps) == 2 and all(r["status"] == "ok" for r in reps)
    assert "jobs 2" in cap.err and "threads" in cap.err
    assert main(["decode", str(out), "-o", str(tmp_path / "bd"), "-j", "2", "--quiet"]) == EXIT_OK
    assert len(list((tmp_path / "bd").glob("*.dng"))) == 2


def test_corrupted_chunk_rejected(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rsq = tmp_path / "c.rsq"
    assert main(["encode", str(raws / "iso4000.dng"), "-o", str(rsq), "--preset", "lossless", "--quiet"]) == EXIT_OK
    assert main(["info", str(rsq), "--json"]) == EXIT_OK
    pln = next(c for c in _json_out(capsys)["chunks"] if c["fourcc"] == "PLN1")
    b = bytearray(rsq.read_bytes())
    b[pln["offset"] + 16 + pln["length"] // 2] ^= 0x01
    bad = tmp_path / "bad.rsq"
    bad.write_bytes(bytes(b))
    assert main(["decode", str(bad), "-o", str(tmp_path / "bad.dng")]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "CRC mismatch in chunk PLN1" in err
    assert not (tmp_path / "bad.dng").exists()
    assert main(["info", str(bad)]) == EXIT_ERROR
    assert "FAILED in PLN1" in capsys.readouterr().out


@pytest.mark.parametrize("fmt,suffix", [("npy", ".npy"), ("pgm16", ".pgm"), ("tiff", ".tiff")])
def test_decode_formats(raws: Path, tmp_path: Path, fmt: str, suffix: str) -> None:
    rsq = tmp_path / "f.rsq"
    assert main(["encode", str(raws / "iso100.dng"), "-o", str(rsq), "--preset", "lossless", "--quiet"]) == EXIT_OK
    assert main(["decode", str(rsq), "--format", fmt, "--quiet"]) == EXIT_OK
    out = rsq.with_suffix(suffix)
    assert out.exists() and out.stat().st_size > 0
    if fmt == "npy":
        import rawpy

        with rawpy.imread(str(raws / "iso100.dng")) as r:
            assert np.array_equal(np.load(out), r.raw_image)
    if fmt == "pgm16":
        assert out.read_bytes().startswith(b"P5\n512 512\n65535\n")


@pytest.mark.skipif(not have("ssimulacra2"), reason="ssimulacra2 not installed")
def test_verify_exit_codes(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = tmp_path / "good.rsq", tmp_path / "bad.rsq"
    src = raws / "iso4000.dng"
    assert main(["encode", str(src), "-o", str(good), "--preset", "lossless", "--quiet"]) == EXIT_OK
    assert main(["verify", str(good), "--original", str(src), "--metrics", "psnr", "--no-floor", "--json"]) == EXIT_OK
    rep = _json_out(capsys)
    assert rep["mosaic_equal"] and rep["acceptance"]["passed"] and rep["sizes"]["bytes"] == good.stat().st_size
    assert main(["encode", str(src), "-o", str(bad), "--engine", "half3", "-q", "3.0", "--quiet"]) == EXIT_OK
    rc = main(["verify", str(bad), "--original", str(src), "--ev", "3", "--metrics", "psnr,ssimulacra2",
               "--no-floor", "--report", str(tmp_path / "r.json")])
    assert rc == EXIT_VERIFY
    assert "FAIL" in capsys.readouterr().out
    assert json.loads((tmp_path / "r.json").read_text())["acceptance"]["passed"] is False


@pytest.mark.skipif(not (have("ssimulacra2") and have("butteraugli_main")), reason="metric tools not installed")
def test_encode_verify_fallback(raws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = raws / "iso4000.dng"
    # threshold 1 makes auto pick half3 on the high-ISO fixture; the quick verify must reject it (R1)
    rc = main(["encode", str(src), "-o", str(tmp_path / "fb.rsq"), "--verify", "--snr-threshold", "1", "--json"])
    r = _json_out(capsys)[0]
    assert r["fallback"] and r["fallback"]["from"] == "half3" and r["engine"] == "nlq"
    assert rc in (EXIT_OK, EXIT_VERIFY) and (rc == EXIT_OK) == r["verify"]["passed"]
    rc2 = main(["encode", str(src), "-o", str(tmp_path / "nofb.rsq"), "--verify", "--snr-threshold", "1",
                "--no-fallback", "--json"])
    r2 = _json_out(capsys)[0]
    assert rc2 == EXIT_VERIFY and r2["engine"] == "half3" and r2["status"] == "verify-failed" and not r2["fallback"]


def test_plan_jobs_mapping(tmp_path: Path) -> None:
    (tmp_path / "a" / "sub").mkdir(parents=True)
    for p in ("a/x.RW2", "a/sub/y.rw2", "a/z.txt"):
        (tmp_path / p).write_bytes(b"0")
    want = lambda q: q.suffix.lower() == ".rw2"  # noqa: E731
    kw = dict(suffix=".rsq", want=want, overwrite=False, skip_existing=False)
    flat = plan_jobs([str(tmp_path / "a")], str(tmp_path / "o"), recursive=False, **kw)
    assert [j.dst.relative_to(tmp_path).as_posix() for j in flat] == ["o/x.rsq"]
    rec = plan_jobs([str(tmp_path / "a")], str(tmp_path / "o"), recursive=True, **kw)
    assert sorted(j.dst.relative_to(tmp_path).as_posix() for j in rec) == ["o/sub/y.rsq", "o/x.rsq"]
    single = plan_jobs([str(tmp_path / "a/x.RW2")], str(tmp_path / "out.rsq"), recursive=False, **kw)
    assert single[0].dst == tmp_path / "out.rsq"
    beside = plan_jobs([str(tmp_path / "a/x.RW2")], None, recursive=False, **kw)
    assert beside[0].dst == tmp_path / "a/x.rsq"
    with pytest.raises(ValueError, match="same output"):
        plan_jobs([str(tmp_path / "a/x.RW2"), str(tmp_path / "a/x.RW2")], str(tmp_path / "o2"), recursive=False, **kw)
