"""Tests for rawsqueeze.verify and rawsqueeze.bench."""

from __future__ import annotations

import copy
import csv
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from rawsqueeze import bench
from rawsqueeze import verify as V
from rawsqueeze.cfa import merge_planes, split_planes
from rawsqueeze.container import make_chunk
from rawsqueeze.tools import have

pytest.importorskip("rawpy")
needs_ss2 = pytest.mark.skipif(not have("ssimulacra2"), reason="ssimulacra2 not installed")
needs_ba = pytest.mark.skipif(not have("butteraugli_main"), reason="butteraugli_main not installed")


# ---------------------------------------------------------------------------------------
# development


def test_develop_deterministic_and_cropped(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso100.head_sections()
    a = V.develop(frame_iso100.mosaic, head, 0)
    b = V.develop(frame_iso100.mosaic, head, 0)
    assert a.dtype == np.uint16 and a.shape == (504, 512, 3)  # crop_ltwh = (0, 8, 512, 504)
    assert np.array_equal(a, b)
    a3 = V.develop(frame_iso100.mosaic, head, 3)
    assert a3.mean() > a.mean()


def test_develop_many_matches_develop(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso4000.head_sections()
    imgs = V.develop_many({"o": frame_iso4000.mosaic}, head, [0.0, 2.0], threads=4)
    assert np.array_equal(imgs[("o", 2.0)], V.develop(frame_iso4000.mosaic, head, 2.0))


def test_libraw_curve_matches_and_inverts(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    from rawsqueeze.dng import dng_bytes16

    buf = dng_bytes16(frame_iso4000.mosaic, frame_iso4000.head_sections())
    g = V._postprocess(buf, 0.0)
    lin = V._postprocess(buf, 0.0, linear=True).astype(np.float64)
    ours = np.minimum(np.floor(65536.0 * V.libraw_oetf(lin / 65536.0)), 65535)
    assert np.array_equal(ours, g)  # numpy tone curve == LibRaw (2.4, 12.92) curve
    x = np.linspace(0, 1, 1001)
    assert np.allclose(V.libraw_eotf(V.libraw_oetf(x)), x, atol=1e-9)


def test_develop_beyond_libraw_range(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso4000.head_sections()
    a3 = V.develop(frame_iso4000.mosaic, head, 3)
    a4 = V.develop(frame_iso4000.mosaic, head, 4)
    assert a4.shape == a3.shape and a4.dtype == np.uint16
    assert a4.astype(float).mean() > a3.astype(float).mean()


# ---------------------------------------------------------------------------------------
# tiles / parsing / helpers


def test_pick_tiles_rules() -> None:
    rng = np.random.default_rng(0)
    H, W = 4000, 6000
    img = np.full((H, W, 3), 30000, np.uint16)
    img[3000:4000, 0:2048] = 2000  # dark region bottom-left
    img[0:2048, 3952:6000] += rng.integers(0, 20000, (2048, 2048, 3)).astype(np.uint16)  # textured top-right
    tiles = V.pick_tiles(img, 4, seed=1)
    names = [t.name for t in tiles]
    assert names == ["center", "darkest", "variance", "random"]
    for t in tiles:
        assert t.h == t.w == 2048 and t.y % 2 == 0 and t.x % 2 == 0
        assert 0 <= t.y <= H - 2048 and 0 <= t.x <= W - 2048
    assert tiles[0].y == (H - 2048) // 2 & ~1
    d = tiles[1]
    assert d.x < 1000 and d.y > 1500
    v = tiles[2]
    assert v.x > 3000 and v.y < 600
    assert V.pick_tiles(img, 4, seed=1) == tiles  # deterministic
    small = V.pick_tiles(img[:500, :700], 4)
    assert [t.name for t in small] == ["full"] and (small[0].h, small[0].w) == (500, 700)


def test_parse_tool_outputs() -> None:
    assert V.parse_ssimulacra2("87.12345678\n") == pytest.approx(87.12345678)
    assert V.parse_ssimulacra2("Score: -12.5") == -12.5
    out = "3.2717154026\n3-norm: 0.903126\n"
    assert V.parse_butteraugli(out) == (pytest.approx(3.2717154026), pytest.approx(0.903126))
    assert V.parse_butteraugli("1.5\n") == (1.5, None)


def test_psnr_and_ppm(tmp_path: Path) -> None:
    a = np.zeros((4, 5, 3), np.uint16)
    b = a.copy()
    assert V.psnr16(a, b) == math.inf
    b[0, 0, 0] = 65535
    assert V.psnr16(a, b) == pytest.approx(10 * math.log10(60))
    V.write_ppm16(tmp_path / "x.ppm", b)
    raw = (tmp_path / "x.ppm").read_bytes()
    assert raw.startswith(b"P6\n5 4\n65535\n") and len(raw) == len(b"P6\n5 4\n65535\n") + 4 * 5 * 3 * 2
    assert raw[len(b"P6\n5 4\n65535\n") :][:2] == b"\xff\xff"


def test_floor_control_reproducible() -> None:
    m = np.full((64, 64), 4094, np.uint16)
    m[:, :32] = 128
    f1, f2 = V.floor_control(m), V.floor_control(m)
    assert np.array_equal(f1, f2)
    assert not np.array_equal(f1, V.floor_control(m, seed=6))
    d = f1.astype(int) - m
    assert set(np.unique(d)) <= {0, 1}
    assert 0.4 < d.mean() < 0.6
    assert f1.max() <= 4095
    assert V.floor_control(np.full((8, 8), 4095, np.uint16)).max() == 4095


def test_clip_consistency_counts() -> None:
    m = np.array([[4079, 4095, 100], [4079, 200, 300]], np.uint16)
    r = np.array([[4079, 4079, 100], [4000, 4079, 300]], np.uint16)
    c = V.clip_consistency(m, r, 4079)
    assert c == {"n_clipped": 3, "inconsistent": 1, "false_clip": 1}


def test_raw_noise_metrics_scale(synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    fr = synthetic_frame_factory(h=512, w=512, g=0.6, s2=4.0)
    head = fr.head_sections()
    head["noise"] = {"planes": [{"g_used": 0.6, "s2": 4.0}] * 4}
    rng = np.random.default_rng(1)
    M = fr.mosaic.astype(np.float64)
    sig = np.sqrt(0.6 * np.maximum(M - 128, 0) + 4.0)
    rec = np.clip(np.rint(M + 0.3 * sig * rng.standard_normal(M.shape)), 0, 65535).astype(np.uint16)
    res = V.raw_noise_metrics(fr.mosaic, rec, head)
    assert res["model"] is True
    assert 0.27 < res["rmse_sigma_max"] < 0.36  # 0.3 + rounding contribution
    for p in res["positions"]:
        assert len(p["bins"]) == 16 and abs(p["bias_dn"]) < 0.2
    nomodel = V.raw_noise_metrics(fr.mosaic, rec, fr.head_sections())
    assert nomodel["rmse_sigma_max"] is None and nomodel["positions"][0]["rmse_dn"] > 0


def test_noise_ratio_and_bias() -> None:
    rng = np.random.default_rng(0)
    base = np.full((512, 512, 3), 20000.0)
    a = np.clip(base + 400 * rng.standard_normal(base.shape), 0, 65535).astype(np.uint16)
    b = np.clip(a + 200 * rng.standard_normal(base.shape), 0, 65535).astype(np.uint16)
    r = V.noise_std_ratio(a, b)
    assert r == pytest.approx(math.sqrt(1 + 0.25), rel=0.03)
    assert V.noise_std_ratio(a, a) == pytest.approx(1.0)
    assert V.bias8(a, a + 257) == pytest.approx(1.0)


def test_acceptance_table_and_check() -> None:
    assert V.acceptance_criteria("vl", "half3")["ss2_min"] == {0.0: 84.0, 3.0: 79.0}
    assert V.acceptance_criteria("archival", "nlq") == {"mosaic_equal": True}
    assert V.acceptance_criteria("vl", "nlq", mode="lossless") == {"mosaic_equal": True}
    assert V.acceptance_criteria("vl", "gat4") is None
    assert set(V.ACCEPTANCE) >= {("high", "half3"), ("high", "nlq"), ("vl", "nlq"), ("compact", "half3"), ("compact", "nlq")}
    worst = {
        0.0: V.TileMetrics("w", 0.0, ss2=85.0, ba_p3=0.5),
        3.0: V.TileMetrics("w", 3.0, ss2=78.0, ba_p3=1.2),
    }
    rep = V.VerifyReport("half3", "vl", "lossy", {"low_iso": True}, [0.0, 3.0], [], worst, [], None, {}, {"inconsistent": 2}, False)
    acc = V.check_acceptance(rep)
    assert not acc["passed"] and len(acc["failures"]) == 3
    rep.worst[3.0] = V.TileMetrics("w", 3.0, ss2=80.0, ba_p3=0.9)
    rep.clip = {"inconsistent": 0}
    assert V.check_acceptance(rep)["passed"]


# ---------------------------------------------------------------------------------------
# verify()


@needs_ss2
def test_verify_identical(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso100.head_sections()
    head.update(engine="nlq", mode="lossless", preset="lossless")
    rep = V.verify(frame_iso100.mosaic, frame_iso100.mosaic.copy(), head, floor=False)
    assert rep.mosaic_equal
    for ev in (0.0, 2.0, 3.0):
        assert rep.worst[ev].ss2 == 100.0 and rep.worst[ev].psnr == math.inf
    assert rep.acceptance["passed"]
    assert rep.clip["inconsistent"] == 0
    d = json.loads(rep.to_json())
    assert d["worst"]["3.0"]["psnr"] == "inf"


@needs_ss2
def test_ssimulacra2_tool_identical_is_100(tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    img = V.develop(frame_iso100.mosaic, frame_iso100.head_sections(), 2)
    V.write_ppm16(tmp_path / "a.ppm", img)
    V.write_ppm16(tmp_path / "b.ppm", img)
    assert V.ssimulacra2(tmp_path / "a.ppm", tmp_path / "b.ppm") == pytest.approx(100.0)


@needs_ss2
@needs_ba
def test_verify_floor_reproducible(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso4000.head_sections()
    head.update(engine="nlq", preset="vl", mode="lossy")
    rec = V.floor_control(frame_iso4000.mosaic, seed=1)
    r1 = V.verify(frame_iso4000.mosaic, rec, head, evs=(0, 3), threads=4)
    r2 = V.verify(frame_iso4000.mosaic, rec, head, evs=(0, 3), threads=4)
    assert r1.floor and r2.floor
    for ev in (0.0, 3.0):
        assert r1.floor[ev].ss2 == r2.floor[ev].ss2
        assert r1.worst[ev].ss2 == r2.worst[ev].ss2
        assert r1.worst[ev].ba_max is not None and r1.worst[ev].ba_p3 is not None
        assert r1.worst[ev].ss2_ds2 is not None
        # a different 0/1 realisation scores like the FLOOR itself
        assert abs(r1.worst[ev].ss2 - r1.floor[ev].ss2) < 3.0
    assert r1.worst[3.0].ss2 < r1.worst[0.0].ss2
    assert r1.noise["noise_ratio_max"] == pytest.approx(1.0, abs=0.05)
    assert r1.noise["bias8_abs_max"] is not None
    assert [t.name for t in r1.tiles] == ["full"]
    assert "FLOOR" in V.format_report(r1)


def test_verify_detects_clip_inconsistency(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso100.head_sections()
    head.update(engine="half3", preset="vl", mode="lossy")
    rec = frame_iso100.mosaic.copy()
    sat = frame_iso100.mosaic >= frame_iso100.white
    assert sat.any()
    rec[sat] = frame_iso100.white - 50
    rep = V.verify(frame_iso100.mosaic, rec, head, evs=(0,), floor=False, metrics=("psnr",))
    assert rep.clip["inconsistent"] == int(sat.sum())
    assert not rep.acceptance["passed"]


@pytest.mark.slow
@needs_ss2
def test_verify_full_sample_identical(sample_path) -> None:  # type: ignore[no-untyped-def]
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path("P1060444.RW2"), want_exif=False)
    rep = V.verify(fr.mosaic, fr.mosaic, fr.head_sections(), floor=False, metrics=("psnr", "ssimulacra2"))
    assert [t.name for t in rep.tiles] == ["center", "darkest", "variance", "random"]
    assert all(rep.worst[ev].ss2 == 100.0 and rep.worst[ev].psnr == math.inf for ev in rep.evs)


# ---------------------------------------------------------------------------------------
# bench


def test_parse_sweep() -> None:
    assert bench.parse_sweep("engine=half3;d=0.1,0.2,0.3") == [
        {"engine": "half3", "d": 0.1}, {"engine": "half3", "d": 0.2}, {"engine": "half3", "d": 0.3}]
    s = bench.parse_sweep("engine=nlq,gat4;f=1,2;effort=3")
    assert len(s) == 4 and s[0] == {"engine": "nlq", "f": 1, "effort": 3}
    assert bench.parse_sweep(["engine=half3;d=0.2", "engine=nlq;f=1|engine=lossless"]) == [
        {"engine": "half3", "d": 0.2}, {"engine": "nlq", "f": 1}, {"engine": "lossless"}]
    assert bench.parse_sweep("engine=x;flag=true;y=none")[0] == {"engine": "x", "flag": True, "y": None}
    assert bench.param_string({"engine": "half3", "d": 0.2, "effort": 5, "dD": 0.3}) == "d=0.2;dD=0.3"
    for bad in ("", "engine", "d=;", "d=1;d=2"):
        with pytest.raises(ValueError):
            bench.parse_sweep(bad)


def _fake_codec() -> tuple[Any, Any]:
    """Planes engine: lossless JXL e1, or additive noise of `noise` DN (to test metrics)."""
    from rawsqueeze import jxl

    def enc(frame: Any, engine: str, params: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        m = frame.mosaic
        amp = float(params.get("noise", 0))
        if amp:
            rng = np.random.default_rng(0)
            m = np.clip(m.astype(np.int32) + rng.integers(-int(amp), int(amp) + 1, m.shape), 0, 4095).astype(np.uint16)
        P = split_planes(m)
        chunks = [make_chunk(f"PLN{k}", jxl.encode_lossless(P[k], effort=1)) for k in range(4)]
        head = dict(frame.head_sections())
        head.update(engine=engine, mode="lossless" if not amp else "lossy", codec={"effort": params.get("effort", 1)})
        return chunks, head

    def dec(head: dict[str, Any], chunks: Mapping[str, bytes]) -> np.ndarray:
        return merge_planes([jxl.decode(chunks[f"PLN{k}"]) for k in range(4)])

    return enc, dec


@needs_ss2
def test_run_sweep_and_csv(tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    enc, dec = _fake_codec()
    reports: list[Any] = []
    logs: list[str] = []
    fr = copy.deepcopy(frame_iso100)
    rows = bench.run_sweep(
        [fr], "engine=fake;noise=0,2|engine=broken", crop=256, evs=(0, 3),
        encode_fn=lambda f, e, p: (_ for _ in ()).throw(RuntimeError("boom")) if e == "broken" else enc(f, e, p),
        decode_fn=dec, metrics=("psnr", "ssimulacra2", "noise"), reports=reports, log=logs.append,
    )
    good = [r for r in rows if "error" not in r]
    bad = [r for r in rows if "error" in r]
    assert len(good) == 4 and len(bad) == 1 and "boom" in bad[0]["error"]
    l0 = [r for r in good if r["param"] == "noise=0"]
    assert all(r["ss2"] == 100.0 and r["psnr"] == math.inf for r in l0)
    l2 = [r for r in good if r["param"] == "noise=2"]
    assert all(r["ss2"] < 100 for r in l2)
    assert all(r["floor_ss2"] is not None for r in good)
    assert all(r["bytes"] > 0 and r["ratio_file"] is None and r["ratio_raw"] is None for r in good)  # pre-cropped
    ev3 = [r for r in good if r["ev"] == 3.0]
    assert all(r["bias8"] is not None for r in ev3)  # noise_ratio may be None: tile clips at +3EV
    assert all(r["bias8"] is None for r in good if r["ev"] == 0.0)
    assert len(reports) == 2 and len(logs) == 3
    p = bench.write_csv(rows, tmp_path / "out.csv")
    with open(p) as f:
        rd = list(csv.reader(f))
    assert rd[0] == list(bench.CSV_COLUMNS) + ["error"]
    assert len(rd) == 6
    assert rd[1][0] == fr.source_name and rd[1][2] == "fake"


def test_run_sweep_ratios(synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    enc, dec = _fake_codec()
    fr = synthetic_frame_factory(h=512, w=512)
    fr.source_size = 10_000_000
    fr.raw_data_offset = 2_000_000
    rows = bench.run_sweep([fr], "engine=fake", crop=256, evs=(0,), encode_fn=enc, decode_fn=dec,
                           metrics=("psnr",), floor=False, log=lambda s: None)
    (r,) = rows
    assert r["ratio_file"] == pytest.approx(10_000_000 / 4 / r["bytes"])
    assert r["ratio_raw"] == pytest.approx(8_000_000 / 4 / r["bytes"])
    assert r["effort"] == 1 and r["psnr"] == math.inf and r["ss2"] is None


def test_to_rsq_bytes_accepts_mapping(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    from rawsqueeze.container import read_rsq

    head = {"format": "rsq", "engine": "x"}
    blob = bench.to_rsq_bytes({"PLN0": b"abc", "META": b"zz"}, head)
    f = read_rsq(blob)
    assert f.head["engine"] == "x" and f.chunks["PLN0"] == b"abc"
    del frame_iso100
