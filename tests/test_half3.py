"""Tests for the half3 engine (DESIGN.md 2.5, test plan item 6)."""

from __future__ import annotations

import dataclasses
import io
import json
import sys

import numpy as np
import pytest

from rawsqueeze import jxl
from rawsqueeze.container import head_to_json, make_head_chunk, read_rsq, write_rsq
from rawsqueeze.engines import EngineParams, get_engine
from rawsqueeze.engines.half3 import (
    XYZ_FROM_SRGB,
    split_threads_3_1,
    srgb_from_cam,
    white_balance,
)


def _roundtrip(frame, params: EngineParams, *, via_container: bool = True):
    eng = get_engine("half3")
    out = eng.encode(frame, params)
    head = frame.head_sections()
    head.update(engine="half3", codec=out.codec, **out.head_extra)
    if via_container:
        buf = io.BytesIO()
        write_rsq(buf, [make_head_chunk(head)] + out.chunks)
        f = read_rsq(buf.getvalue())
        rec = eng.decode(f.head, f.chunks, threads=4)
        return out, f.head, f.chunks, rec
    chunks = {c.name: c.payload for c in out.chunks}
    return out, head, chunks, eng.decode(head, chunks, threads=4)


def _err(frame, rec):
    return rec.astype(np.int64) - frame.mosaic.astype(np.int64)


def test_engine_registry():
    eng = get_engine("half3")
    assert eng.name == "half3"


def test_flat_low_noise_bounded(synthetic_frame_factory):
    fr = synthetic_frame_factory(128, 192, g=1e-3, s2=0.01, signal="flat")
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    assert rec.dtype == np.uint16 and rec.shape == fr.mosaic.shape
    e = _err(fr, rec)
    assert np.abs(e).max() <= 12
    assert abs(e.mean()) < 1.0
    assert out.head_extra == {"mode": "lossy"}
    assert {"H3RG", "H3DG", "SATM"} <= set(chunks)


def test_gradient_bounded(synthetic_frame_factory):
    fr = synthetic_frame_factory(256, 256, g=0.06, s2=2.0, signal="gradient")
    _, _, _, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    e = _err(fr, rec)
    assert abs(e.mean()) < 1.0
    assert np.sqrt((e.astype(np.float64) ** 2).mean()) < 15
    assert np.abs(e).max() < 200


def test_lower_distance_is_more_accurate(synthetic_frame_factory):
    fr = synthetic_frame_factory(256, 256, g=0.06, s2=2.0)
    rmse = []
    for d in (0.1, 0.4):
        _, _, _, rec = _roundtrip(fr, EngineParams(quality=d, threads=2), via_container=False)
        rmse.append(np.sqrt((_err(fr, rec).astype(np.float64) ** 2).mean()))
    assert rmse[0] < rmse[1]


def test_saturation_mask_exact(synthetic_frame_factory):
    fr = synthetic_frame_factory(256, 256, g=0.06, s2=2.0, saturate_frac=0.05)
    # scattered single saturated pixels with values above white
    rng = np.random.default_rng(1)
    idx = rng.integers(0, fr.mosaic.size, 300)
    fr.mosaic.ravel()[idx] = rng.integers(fr.white, 4096, 300)
    sat = fr.mosaic >= fr.white
    _, _, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    assert np.all(rec[sat] == fr.white)
    assert rec.max() <= fr.white
    blk = np.array(fr.black_per_position).reshape(2, 2)
    blk_full = np.tile(blk, (fr.height // 2, fr.width // 2))
    assert np.all(rec >= blk_full)
    # packbits payload size = ceil(H*W/8)
    assert len(chunks["SATM"]) == (fr.mosaic.size + 7) // 8


def test_no_satmask_option(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64, saturate_frac=0.1)
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, satmask=False, threads=1))
    assert "SATM" not in chunks
    assert head["codec"]["half3"]["satmask"] is False


def test_missing_satm_chunk_raises(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64)
    out = get_engine("half3").encode(fr, EngineParams(quality=0.2, threads=1))
    head = fr.head_sections()
    head["codec"] = out.codec
    chunks = {c.name: c.payload for c in out.chunks if c.name != "SATM"}
    with pytest.raises(ValueError, match="SATM"):
        get_engine("half3").decode(head, chunks)


def test_matrix_json_roundtrip_exact(frame_iso100):
    out = get_engine("half3").encode(frame_iso100, EngineParams(quality=0.2, threads=2))
    blk = out.codec["half3"]
    back = json.loads(head_to_json({"codec": out.codec}))["codec"]["half3"]
    for key in ("M", "Minv", "wb"):
        a = np.array(blk[key], dtype=np.float64)
        b = np.array(back[key], dtype=np.float64)
        assert np.array_equal(a, b), key
    M = np.array(blk["M"])
    assert blk["matrix"] is True
    assert np.allclose(M @ np.array(blk["Minv"]), np.eye(3), atol=1e-12)
    # srgb_from_cam rows: inverse of row-normalised cam matrix -> M maps (1,1,1) to (1,1,1)
    assert np.allclose(M @ np.ones(3), np.ones(3), atol=1e-12)
    assert blk["wb"] == pytest.approx([534 / 256, 1.0, 431 / 256])


def test_matrix_matches_reference_formula(frame_iso100):
    roles = frame_iso100.color_roles()
    M, used = srgb_from_cam(frame_iso100, roles)
    c = frame_iso100.xyz_to_cam[:3] @ XYZ_FROM_SRGB
    c /= c.sum(1, keepdims=True)
    assert used and np.allclose(M, np.linalg.inv(c), rtol=0, atol=1e-14)


def test_zero_matrix_falls_back_to_identity(synthetic_frame_factory):
    fr = synthetic_frame_factory(128, 128, g=0.06, s2=2.0)
    fr = dataclasses.replace(fr, xyz_to_cam=np.zeros((3, 3)))
    out, head, _, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    blk = head["codec"]["half3"]
    assert blk["matrix"] is False
    assert np.array_equal(np.array(blk["M"]), np.eye(3))
    assert np.sqrt((_err(fr, rec).astype(np.float64) ** 2).mean()) < 15


def test_use_matrix_off(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64)
    out = get_engine("half3").encode(fr, EngineParams(quality=0.2, use_matrix=False, threads=1))
    assert out.codec["half3"]["matrix"] is False


def test_wb_fallback_chain(synthetic_frame_factory):
    fr = synthetic_frame_factory(32, 32)
    roles = fr.color_roles()
    assert white_balance(fr, roles) == ([534 / 256, 1.0, 431 / 256], "camera")
    fr2 = dataclasses.replace(fr, camera_wb=[0.0, 256.0, 431.0, 0.0])
    wb, src = white_balance(fr2, roles)
    assert src == "daylight" and wb == pytest.approx([2.135 / 0.9385, 1.0, 1.3927 / 0.9385])
    fr3 = dataclasses.replace(fr2, daylight_wb=[0.0, 0.0, 0.0, 0.0])
    assert white_balance(fr3, roles) == ([1.0, 1.0, 1.0], "unity")


@pytest.mark.parametrize(
    "pattern",
    [((0, 1), (3, 2)), ((2, 3), (1, 0)), ((1, 0), (2, 3)), ((3, 2), (0, 1))],
    ids=["RGGB", "BGGR", "GRBG", "GBRG"],
)
def test_all_bayer_phases(synthetic_frame_factory, pattern):
    fr = synthetic_frame_factory(128, 128, g=0.06, s2=2.0, pattern=pattern, saturate_frac=0.02)
    out, head, _, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    roles = fr.color_roles()
    assert head["codec"]["half3"]["roles"] == [roles[r] for r in ("R", "G1", "G2", "B")]
    sat = fr.mosaic >= fr.white
    assert np.all(rec[sat] == fr.white)
    e = _err(fr, rec)[~sat]
    assert np.sqrt((e.astype(np.float64) ** 2).mean()) < 15


def test_roles_derived_from_head_cfa_when_absent(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64, pattern=((2, 3), (1, 0)))
    eng = get_engine("half3")
    out = eng.encode(fr, EngineParams(quality=0.2, threads=1))
    head = fr.head_sections()
    head["codec"] = json.loads(json.dumps(out.codec))
    chunks = {c.name: c.payload for c in out.chunks}
    a = eng.decode(head, chunks)
    del head["codec"]["half3"]["roles"]
    b = eng.decode(head, chunks)
    assert np.array_equal(a, b)


def test_per_position_black(synthetic_frame_factory):
    fr = synthetic_frame_factory(128, 128, g=0.06, s2=2.0, black=128)
    blk = [120, 128, 132, 140]
    m = fr.mosaic.astype(np.int32)
    for k, (dy, dx) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        m[dy::2, dx::2] += blk[k] - 128
    fr = dataclasses.replace(fr, mosaic=np.clip(m, 0, 4095).astype(np.uint16), black_per_position=blk)
    _, _, _, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    for k, (dy, dx) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        e = rec[dy::2, dx::2].astype(np.int64) - fr.mosaic[dy::2, dx::2]
        assert abs(e.mean()) < 1.5, (k, e.mean())
        assert rec[dy::2, dx::2].min() >= blk[k]


def test_decoded_streams_are_float32(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 96)
    out = get_engine("half3").encode(fr, EngineParams(quality=0.2, threads=1))
    ch = {c.name: c.payload for c in out.chunks}
    rgb = jxl.decode(ch["H3RG"])
    d = jxl.decode(ch["H3DG"])
    assert rgb.dtype == np.float32 and rgb.shape == (32, 48, 3)
    assert d.dtype == np.float32 and d.size == 32 * 48


def test_decode_does_not_use_rawpy(synthetic_frame_factory, monkeypatch):
    fr = synthetic_frame_factory(64, 64, saturate_frac=0.05)
    eng = get_engine("half3")
    out = eng.encode(fr, EngineParams(quality=0.2, threads=1))
    head = json.loads(head_to_json({**fr.head_sections(), "codec": out.codec}))
    chunks = {c.name: c.payload for c in out.chunks}
    monkeypatch.setitem(sys.modules, "rawpy", None)  # any `import rawpy` now raises ImportError
    rec = eng.decode(head, chunks)
    assert rec.shape == (64, 64)


def test_dD_default_and_override(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64)
    eng = get_engine("half3")
    a = eng.encode(fr, EngineParams(quality=0.25, threads=1))
    assert a.codec["half3"]["d"] == 0.25 and a.codec["half3"]["dD"] == 0.25
    b = eng.encode(fr, EngineParams(quality=0.25, dD=0.5, threads=1))
    assert b.codec["half3"]["dD"] == 0.5
    sa = {c.name: len(c.payload) for c in a.chunks}
    sb = {c.name: len(c.payload) for c in b.chunks}
    assert sb["H3DG"] < sa["H3DG"] and sb["H3RG"] == sa["H3RG"]
    assert a.codec["effort"] == 5


def test_rejects_non_bayer_and_odd(synthetic_frame_factory):
    eng = get_engine("half3")
    fr = synthetic_frame_factory(64, 64, pattern=((0, 1), (2, 3)), color_desc="CMYG")
    with pytest.raises(ValueError, match="Bayer"):
        eng.encode(fr, EngineParams(quality=0.2))
    fr2 = synthetic_frame_factory(63, 64)
    with pytest.raises(ValueError, match="even"):
        eng.encode(fr2, EngineParams(quality=0.2))


def test_thread_split():
    assert split_threads_3_1(8) == (6, 2)
    assert split_threads_3_1(1) == (1, 1)
    assert split_threads_3_1(2) == (2, 1) or split_threads_3_1(2) == (1, 1)
    assert split_threads_3_1(4) == (3, 1)


def test_fixture_iso100(frame_iso100):
    fr = frame_iso100
    sat = fr.mosaic >= fr.white
    assert sat.any()
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=4))
    assert np.all(rec[sat] == fr.white)
    e = _err(fr, rec)
    assert abs(e.mean()) < 1.0
    assert np.percentile(np.abs(e), 99) < 120  # highlight-heavy crop


@pytest.mark.slow
def test_full_sample_p1060444(sample_path):
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path("P1060444.RW2"), want_exif=False)
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, effort=5, threads=8))
    total = sum(len(c.payload) for c in out.chunks if c.name != "SATM")
    assert 4.4e6 < total < 5.4e6  # spec: 4.89 MB total
    sat = fr.mosaic >= fr.white
    assert int((rec[sat] != fr.white).sum()) == 0
    e = _err(fr, rec)
    assert abs(e.mean()) < 0.5
    assert (np.abs(e) > 50).mean() < 0.01


# ---------------------------------------------------------------------------------------
# review fixes: CFA-phase independent colour, gamut guard, no false saturation

_PHASES = [((0, 1), (3, 2)), ((2, 3), (1, 0)), ((1, 0), (2, 3)), ((3, 2), (0, 1))]


def test_matrix_and_wb_independent_of_cfa_phase(synthetic_frame_factory):
    """LibRaw labels the green next to B with colour index 3 (BGGR/GBRG G1): rows by letter."""
    ref = None
    for pattern in _PHASES:
        fr = synthetic_frame_factory(64, 64, pattern=pattern)
        roles = fr.color_roles()
        M, used = srgb_from_cam(fr, roles)
        assert used, pattern
        wb, src = white_balance(fr, roles)
        out = get_engine("half3").encode(fr, EngineParams(quality=0.2, threads=1))
        assert out.codec["half3"]["matrix"] is True
        # daylight fallback must not hit the zero 4th entry either
        dl, dsrc = white_balance(dataclasses.replace(fr, camera_wb=[0.0, 0.0, 0.0, 0.0]), roles)
        assert dsrc == "daylight"
        if ref is None:
            ref = (M, wb, dl)
        else:
            assert np.array_equal(M, ref[0]) and wb == ref[1] and dl == ref[2], pattern


def _blue_light_frame(synthetic_frame_factory):
    """Gradient frame with a saturated blue patch (outside sRGB: negative R/G after M)."""
    fr = synthetic_frame_factory(128, 128, g=0.06, s2=2.0)
    m = fr.mosaic.copy()
    roles = fr.color_roles()
    lvl = {"R": 128 + 40, "G1": 128 + 260, "G2": 128 + 260, "B": 128 + 2600}
    for role, v in lvl.items():
        dy, dx = ((0, 0), (0, 1), (1, 0), (1, 1))[roles[role]]
        m[32 + dy:96:2, 32 + dx:96:2] = v
    return dataclasses.replace(fr, mosaic=m)


def test_gamut_guard_blends_matrix(synthetic_frame_factory, monkeypatch):
    from rawsqueeze.engines import half3

    fr = _blue_light_frame(synthetic_frame_factory)
    patch = (slice(40, 88), slice(40, 88))

    def bias(rec):
        e = rec[patch].astype(np.float64) - fr.mosaic[patch]
        return [abs(e[dy::2, dx::2].mean()) for dy, dx in ((0, 0), (0, 1), (1, 0), (1, 1))]

    out, head, _, rec = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    blk = head["codec"]["half3"]
    assert blk["matrix"] is True and 0 < blk["matrix_blend"] < 1 and blk["gamut_neg_sites"] > 0
    M = np.array(blk["M"])
    assert np.allclose(M @ np.array(blk["Minv"]), np.eye(3), atol=1e-10)
    assert max(bias(rec)) < 4.0
    # without the guard libjxl clamps the negative opsin mix: large one-sided raw bias
    monkeypatch.setattr(half3, "gamut_blend", lambda cam, M, **kw: (0.0, 0))
    _, head0, _, rec0 = _roundtrip(fr, EngineParams(quality=0.2, threads=2))
    assert head0["codec"]["half3"]["matrix_blend"] == 0
    assert max(bias(rec0)) > 20.0


def test_gamut_blend_zero_for_in_gamut(frame_iso100):
    from rawsqueeze.engines.half3 import gamut_blend

    roles = frame_iso100.color_roles()
    M, _ = srgb_from_cam(frame_iso100, roles)
    cam = [np.full((8, 8), v, np.float32) for v in (0.3, 0.4, 0.35)]
    assert gamut_blend(cam, M) == (0.0, 0)
    cam[0][0, 0], cam[1][0, 0], cam[2][0, 0] = 0.02, 0.05, 0.95  # one saturated blue site
    a, n = gamut_blend([c.copy() for c in cam], M, tolerance=0.0)
    assert n == 1 and 0 < a < 1
    assert gamut_blend(cam, M, tolerance=0.05) == (0.0, 1)  # tolerated isolated site


def test_no_false_saturation(synthetic_frame_factory):
    """Unmasked pixels just below wl must not decode to exactly wl (raw converters clip them)."""
    fr = synthetic_frame_factory(128, 128, g=0.06, s2=2.0, saturate_frac=0.1)
    m = fr.mosaic.astype(np.int32)
    rng = np.random.default_rng(3)
    band = (slice(48, 112), slice(0, 128))
    m[band] = fr.white - rng.integers(1, 6, m[band].shape)
    fr = dataclasses.replace(fr, mosaic=m.astype(np.uint16))
    sat = fr.mosaic >= fr.white
    for engine in ("half3", "gat4"):
        eng = get_engine(engine)
        out = eng.encode(fr, EngineParams(quality=0.2, threads=2, noise=[(0.06, 2.0)] * 4))
        head = {**fr.head_sections(), "codec": out.codec}
        rec = eng.decode(head, {c.name: c.payload for c in out.chunks})
        assert np.all(rec[sat] == fr.white), engine
        assert not np.any(rec[~sat] >= fr.white), engine


@pytest.mark.slow
def test_iso640_led_gamut_guard_and_auto_choice(sample_path, tmp_path):
    """ISO640 stage-light sample: auto now picks nlq (SNR18 47.7 < 60); a forced half3 must not
    clamp the saturated blue/cyan LEDs (was +3EV ss2 41.9 / ba_p3 7.56 before the gamut guard)."""
    from rawsqueeze.pipeline import encode_file

    src = sample_path("ISO640_PANA0036.RW2")
    auto = encode_file(src, tmp_path / "auto.rsq", threads=8)
    assert auto.engine == "nlq" and 40 < auto.snr18 < 60
    rep = encode_file(src, tmp_path / "h3.rsq", engine="half3", threads=8, verify=True, fallback=False)
    head = read_rsq(tmp_path / "h3.rsq").head
    assert 0.1 < head["codec"]["half3"]["matrix_blend"] < 0.5
    if rep.verify["worst"]["3.0"]["ba_p3"] is not None:  # metric tools installed
        assert rep.verify["worst"]["3.0"]["ba_p3"] < 2.0
        assert rep.verify["worst"]["3.0"]["ss2"] > 60.0
