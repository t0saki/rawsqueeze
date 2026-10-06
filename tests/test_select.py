"""Tests for engine selection (DESIGN.md 2.4, test plan item 7)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from rawsqueeze.select import (
    DEFAULT_SNR_THRESHOLD,
    SelectOptions,
    choose_engine,
    noise_g_s2,
    select_engine,
    snr18,
)


@dataclass
class NP:
    g: float
    s2: float


def _noise(g, s2):
    return [NP(g, s2)] * 4


def test_snr18_formula():
    X = 4079 - 128
    x18 = 0.18 * X
    assert snr18(_noise(0.065, 4.0), 4079, 128) == pytest.approx(x18 / math.sqrt(0.065 * x18 + 4.0))
    # DC-S9 ballparks from the spec: ISO100 ~107, ISO4000 ~17
    assert 95 < snr18(_noise(0.065, 2.0), 4079, 128) < 115
    assert 15 < snr18(_noise(2.4, 4.0), 4079, 128) < 19


def test_snr18_uses_green_positions_only():
    noise = [NP(5.0, 100.0), NP(0.06, 4.0), NP(0.07, 4.0), NP(5.0, 100.0)]
    ref = snr18([NP(0.065, 4.0)] * 4, 4079, 128)
    assert snr18(noise, 4079, 128) == pytest.approx(ref)
    noise2 = [noise[1], noise[0], noise[3], noise[2]]
    assert snr18(noise2, 4079, 128, green_positions=(0, 3)) == pytest.approx(ref)


def test_snr18_per_position_black():
    a = snr18(_noise(0.06, 4.0), 4079, [0, 128, 128, 0])
    assert a == pytest.approx(snr18(_noise(0.06, 4.0), 4079, 128))


def test_snr18_validation():
    with pytest.raises(ValueError):
        snr18(_noise(0.06, 4.0)[:3], 4079, 128)
    with pytest.raises(ValueError):
        snr18(_noise(0.06, 4.0), 100, 128)


def test_noise_g_s2_inputs():
    assert noise_g_s2((0.1, 2.0)) == (0.1, 2.0)
    assert noise_g_s2({"g": 0.1, "s2": 2.0}) == (0.1, 2.0)
    assert noise_g_s2({"g_used": 0.05, "g": 0.1, "s2": 2.0}) == (0.05, 2.0)
    assert noise_g_s2(NP(0.1, 2.0)) == (0.1, 2.0)

    class WithUsed:
        g = 0.1
        g_used = 0.04
        s2 = 1.0

    assert noise_g_s2(WithUsed()) == (0.04, 1.0)
    with pytest.raises(ValueError):
        noise_g_s2({"s2": 1.0})


def test_threshold_both_sides(synthetic_frame):
    low_noise = _noise(0.065, 4.0)  # ~100
    high_noise = _noise(2.4, 4.0)  # ~17
    assert choose_engine(synthetic_frame, low_noise) == "half3"
    assert choose_engine(synthetic_frame, high_noise) == "nlq"
    c = select_engine(synthetic_frame, low_noise)
    assert c.engine == "half3" and c.threshold == DEFAULT_SNR_THRESHOLD and c.bayer
    # move the threshold above the measured SNR
    assert choose_engine(synthetic_frame, low_noise, SelectOptions(snr_threshold=c.snr18 + 1)) == "nlq"
    assert choose_engine(synthetic_frame, low_noise, {"snr_threshold": c.snr18 - 1}) == "half3"
    assert choose_engine(synthetic_frame, low_noise, c.snr18) == "half3"  # >= is inclusive


def test_threshold_boundary_exact(synthetic_frame):
    noise = _noise(0.5, 4.0)
    s = snr18(noise, synthetic_frame.white, synthetic_frame.black_per_position)
    assert choose_engine(synthetic_frame, noise, s) == "half3"
    assert choose_engine(synthetic_frame, noise, math.nextafter(s, math.inf)) == "nlq"


def test_non_bayer_falls_back_or_rejects(synthetic_frame_factory):
    fr = synthetic_frame_factory(32, 32, pattern=((0, 1), (2, 3)), color_desc="CMYG")
    assert not fr.is_bayer
    c = select_engine(fr, _noise(0.065, 4.0))
    assert c.engine == "nlq" and c.snr18 is None and "non-Bayer" in c.reason
    for forced in ("half3", "gat4"):
        with pytest.raises(ValueError, match="Bayer"):
            choose_engine(fr, None, {"engine": forced})
    assert choose_engine(fr, None, {"engine": "nlq"}) == "nlq"
    assert choose_engine(fr, None, {"engine": "lossless"}) == "lossless"


def test_no_noise_falls_back_to_nlq(synthetic_frame):
    c = select_engine(synthetic_frame, None)
    assert c.engine == "nlq" and c.snr18 is None


def test_forced_engine_reports_snr(synthetic_frame):
    c = select_engine(synthetic_frame, _noise(2.4, 4.0), SelectOptions(engine="half3"))
    assert c.engine == "half3" and c.reason == "forced" and c.snr18 is not None
    with pytest.raises(ValueError, match="unknown engine"):
        choose_engine(synthetic_frame, None, {"engine": "bogus"})


def test_green_roles_from_pattern(synthetic_frame_factory):
    # GRBG: greens at positions 0 and 3; make those noisy and the others clean
    fr = synthetic_frame_factory(32, 32, pattern=((1, 0), (2, 3)))
    noise = [NP(2.4, 4.0), NP(0.06, 1.0), NP(0.06, 1.0), NP(2.4, 4.0)]
    assert choose_engine(fr, noise) == "nlq"
    fr2 = synthetic_frame_factory(32, 32)  # RGGB: greens at 1 and 2
    assert choose_engine(fr2, noise) == "half3"


def test_accepts_track_a_noise_params(synthetic_frame):
    noise_mod = pytest.importorskip("rawsqueeze.noise")
    params = [noise_mod.NoiseParams(g=0.065, s2=4.0)] * 4
    assert choose_engine(synthetic_frame, params) == "half3"
