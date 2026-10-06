from __future__ import annotations

import threading

import pytest

from rawsqueeze import tools

needs_exiftool = pytest.mark.skipif(not tools.have("exiftool"), reason="exiftool not installed")


def test_which_unknown() -> None:
    assert tools.which("definitely-not-a-real-tool-xyz") is None
    assert not tools.have("definitely-not-a-real-tool-xyz")
    with pytest.raises(tools.ToolError):
        tools.require("definitely-not-a-real-tool-xyz")


def test_versions_shape() -> None:
    v = tools.tool_versions()
    assert set(v) == set(tools.KNOWN_TOOLS)
    for name, ver in v.items():
        if tools.have(name):
            assert ver is None or ver[0].isdigit()
        else:
            assert ver is None


def test_run_helper() -> None:
    cp = tools.run(["/bin/echo", "hello"])
    assert cp.stdout == b"hello\n"
    with pytest.raises(tools.ToolError):
        tools.run(["/bin/sh", "-c", "echo boom >&2; exit 3"])
    cp = tools.run(["/bin/sh", "-c", "exit 3"], check=False)
    assert cp.returncode == 3


@needs_exiftool
def test_exiftool_stay_open_basic(frame_iso100, tmp_path) -> None:
    # make a tiny JPEG-free test file exiftool can describe: a text file
    p = tmp_path / "a b ü.txt"  # spaces + non-ASCII in path
    p.write_text("hello\n")
    with tools.ExifTool() as et:
        assert et.running
        ver = et.version()
        assert ver[0].isdigit()
        rec = et.run_json(["-n", "-FileSize", "-FileType", str(p)])
        assert rec[0]["FileSize"] == 6 and rec[0]["FileType"] == "TXT"
        out = et.run(["-s3", "-FileType", str(p)])
        assert out.strip() == "TXT"
        b = et.run_bytes(["-b", "-FileType", str(p)])
        assert b == b"TXT"
        # missing file: no JSON, error on stderr
        with pytest.raises(tools.ToolError):
            et.run_json([str(tmp_path / "missing.jpg")])
        assert "missing.jpg" in et.last_stderr or et.last_stderr
        # process still usable after an error
        assert et.run(["-s3", "-FileType", str(p)]).strip() == "TXT"
        with pytest.raises(ValueError):
            et.run(["bad\narg"])
    assert not et.running


@needs_exiftool
def test_exiftool_many_commands_and_threads(tmp_path) -> None:
    files = []
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_text("x" * (i + 1))
        files.append(p)
    et = tools.ExifTool()
    try:
        for _ in range(20):
            for i, p in enumerate(files):
                assert et.run_json(["-n", "-FileSize", str(p)])[0]["FileSize"] == i + 1
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                for _ in range(10):
                    r = et.run_json(["-n", "-FileSize", str(files[i])])
                    assert r[0]["FileSize"] == i + 1
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
    finally:
        et.close()
        et.close()  # idempotent


@needs_exiftool
def test_exiftool_large_binary_output(tmp_path) -> None:
    # stdout larger than the pipe buffer must not deadlock
    big = tmp_path / "big.txt"
    big.write_text("y" * 10)
    with tools.ExifTool() as et:
        out = et.run_bytes(["-j", "-n"] + [str(big)] * 400)
        assert out.count(b"SourceFile") == 400


@needs_exiftool
def test_exiftool_restarts_after_kill(tmp_path) -> None:
    p = tmp_path / "k.txt"
    p.write_text("k")
    with tools.ExifTool() as et:
        assert et.run(["-s3", "-FileType", str(p)]).strip() == "TXT"
        et._proc.kill()  # simulate crash
        et._proc.wait()
        assert et.run(["-s3", "-FileType", str(p)]).strip() == "TXT"


@needs_exiftool
def test_exiftool_json_helper(sample_path) -> None:
    rec = tools.exiftool_json(sample_path("P1060444.RW2"), ["ISO", "Make", "RawDataOffset"])
    assert rec == {"ISO": 100, "Make": "Panasonic", "RawDataOffset": 6823936}
    with tools.ExifTool() as et:
        rec2 = tools.exiftool_json(sample_path("P1060444.RW2"), ["ISO", "Make", "RawDataOffset"], et=et)
    assert rec2 == rec
    assert tools.exiftool_json("/nonexistent/file.rw2", ["ISO"]) is None


@needs_exiftool
def test_exiftool_quiet_flags_do_not_hang(tmp_path) -> None:
    with tools.ExifTool(timeout=20) as et:
        assert et.run(["-q", "-q", "-ver"]).strip()[0].isdigit()
        assert et.run(["-q", "-q", str(tmp_path / "nope.jpg")]) == ""
        assert "not found" in et.last_stderr
