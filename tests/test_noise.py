"""Tests for rawsqueeze.noise / noise_table (DESIGN.md 2.3 / test plan item 3)."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable

import numpy as np
import pytest

from rawsqueeze import noise_table
from rawsqueeze.noise import (
    NoiseEstimate,
    NoiseParams,
    apply_iso_cap,
    as_noise_params,
    block_sigma,
    estimate_all,
    estimate_noise,
    iso_prior,
    parse_noise_model,
    percentile_bias,
)
from rawsqueeze.rawio import RawFrame

BLACK, WHITE = 128, 4079
X = WHITE - BLACK


def ramp_plane(n: int, g: float, s2: float, seed: int, texture: float = 0.0) -> np.ndarray:
    """n x n plane: horizontal ramp 0..0.9X (uniform level distribution) + Poisson-Gaussian."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    clean = xx / (n - 1) * 0.9 * X
    if texture:
        clean = np.clip(clean + texture * np.sin(xx * 1.3) * np.sin(yy * 0.9), 0, None)
    noisy = g * rng.poisson(clean / g) + rng.normal(0, np.sqrt(s2), clean.shape) + BLACK
    return np.clip(np.rint(noisy), 0, 4095).astype(np.uint16)


def test_percentile_bias_cached_and_plausible() -> None:
    cf = percentile_bias(8, 5)
    assert cf is percentile_bias(8, 5) or cf == percentile_bias(8, 5)
    # P5 of the robust sigma of 64 samples is well below 1
    assert 0.3 < cf < 0.8


def test_block_sigma_unbiased_after_correction() -> None:
    rng = np.random.default_rng(7)
    cf = percentile_bias(8, 5)
    for sig in (2.0, 10.0):
        z = rng.normal(500, sig, (1024, 1024)).astype(np.float32)
        _, s, _, _ = block_sigma(z, 8)
        assert np.percentile(s, 5) ** 2 / cf / sig**2 == pytest.approx(1.0, abs=0.03)


@pytest.mark.parametrize("g", [0.06, 0.6, 2.4])
@pytest.mark.parametrize("s2", [1.0, 4.0, 16.0])
def test_estimate_noise_synthetic(g: float, s2: float) -> None:
    p = ramp_plane(1024, g, s2, seed=int(g * 100 + s2))
    est = estimate_noise(p, BLACK, WHITE)
    assert not est.fallback
    assert est.nbins == 48
    assert est.g == pytest.approx(g, rel=0.10)
    assert 0.25 <= est.s2 <= 1.5 * s2 + 1.0
    if g == 0.06 and s2 >= 4.0:  # s2 identifiable only when not swamped by shot noise
        assert est.s2 == pytest.approx(s2, rel=0.3, abs=0.5)


@pytest.mark.parametrize("g", [0.06, 0.6, 2.4])
def test_estimate_all_conftest_gradient(synthetic_frame_factory: Callable[..., RawFrame], g: float) -> None:
    """Diagonal-gradient frames from conftest (wide first bin -> estimator biased high)."""
    fr = synthetic_frame_factory(2048, 2048, g=g, s2=4.0, seed=3)
    ne = estimate_all(fr, 4, noise_model="auto")
    for p in ne:
        assert p.g == pytest.approx(g, rel=0.10)


def test_texture_does_not_underestimate() -> None:
    g, s2 = 0.6, 4.0
    p = ramp_plane(1024, g, s2, seed=11, texture=60.0)
    est = estimate_noise(p, BLACK, WHITE)
    assert est.g >= 0.95 * g


def test_small_plane_reduces_bins() -> None:
    p = ramp_plane(256, 0.6, 4.0, seed=2)  # 31x31 = 961 blocks -> 19 bins of >= 50
    est = estimate_noise(p, BLACK, WHITE)
    assert not est.fallback
    assert est.nbins == 19
    assert est.g == pytest.approx(0.6, rel=0.25)


def test_fallback_on_dark_frame(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(512, 512, g=0.6, s2=9.0, signal="zeros", iso=800)
    ne = estimate_all(fr, 2)
    assert ne.fallback
    for p in ne:
        assert p.fallback
        assert p.g == pytest.approx(1e-4 * 800)  # synthetic camera not in table
        assert p.s2 == pytest.approx(9.0, rel=0.2)
    head = ne.to_head()
    assert head["fallback"] is True and head["planes"][0]["fallback"] is True
    # camera in the table -> table prior
    fr2 = dataclasses.replace(fr, make="Panasonic", model="DC-S9")
    ne2 = estimate_all(fr2, 2)
    assert [p.g for p in ne2] == pytest.approx([6.5e-4 * 800] * 3 + [4e-4 * 800])
    # direct call without prior
    est = estimate_noise(fr.mosaic[0::2, 0::2], BLACK, WHITE)
    assert est.fallback and est.g == pytest.approx(0.01)


def test_iso_cap() -> None:
    params = [NoiseParams(g=0.14, s2=1.0, g_est=0.14), NoiseParams(g=0.05, s2=1.0)] * 2
    capped = apply_iso_cap(params, "Panasonic", "DC-S9", 100)
    assert [p.g for p in capped] == pytest.approx([0.065, 0.05, 0.065, 0.04])
    assert [p.capped for p in capped] == [True, False, True, True]
    assert [p.g_est for p in capped] == pytest.approx([0.14, 0.05, 0.14, 0.05])
    # unknown camera / ISO -> unchanged
    assert [p.g for p in apply_iso_cap(params, "Foo", "Bar", 100)] == pytest.approx([0.14, 0.05] * 2)
    assert [p.g for p in apply_iso_cap(params, "Panasonic", "DC-S9", None)] == pytest.approx([0.14, 0.05] * 2)
    assert iso_prior("panasonic ", " DC-S9", 4000) == pytest.approx([2.6, 2.6, 2.6, 1.6])
    assert noise_table.has_camera("PANASONIC", "dc-s9")
    assert noise_table.lookup_k(None, "DC-S9") is None


def test_estimate_all_iso_cap_applied(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(1024, 1024, g=0.6, s2=4.0, seed=5, iso=100)
    fr = dataclasses.replace(fr, make="Panasonic", model="DC-S9")
    ne = estimate_all(fr, 4)
    assert ne.model == "auto+iso_cap"
    assert all(p.capped for p in ne)
    assert [p.g for p in ne] == pytest.approx([0.065] * 3 + [0.04])
    assert all(p.g_est > 0.5 for p in ne)
    ne_auto = estimate_all(fr, 4, noise_model="auto")
    assert ne_auto.model == "auto" and not any(p.capped for p in ne_auto)
    # camera without table entry -> silently plain auto
    ne3 = estimate_all(dataclasses.replace(fr, make="X", model="Y"), 4)
    assert ne3.model == "auto"


def test_manual_and_parse() -> None:
    assert parse_noise_model("auto") == ("auto", None)
    assert parse_noise_model("auto+iso_cap") == ("auto+iso_cap", None)
    assert parse_noise_model("manual:0.5,3") == ("manual", (0.5, 3.0))
    for bad in ("manual:0.5", "manual:-1,2", "magic"):
        with pytest.raises(ValueError):
            parse_noise_model(bad)


def test_manual_estimate_all(synthetic_frame: RawFrame) -> None:
    ne = estimate_all(synthetic_frame, 1, noise_model="manual:0.7,5")
    assert ne.model == "manual"
    assert [(p.g, p.s2) for p in ne] == [(0.7, 5.0)] * 4


def test_noise_estimate_sequence_and_head() -> None:
    ne = NoiseEstimate([NoiseParams(0.1, 2.0, g_est=0.2, capped=True)] * 4, "auto+iso_cap", 100.0)
    assert len(ne) == 4 and ne[0].g == 0.1 and len(list(ne)) == 4 and ne[1:3][0].g == 0.1
    head = ne.to_head(snr18=107.25)
    assert head["model"] == "auto+iso_cap" and head["iso"] == 100 and head["snr18"] == 107.25
    assert head["planes"][0] == {"g_est": 0.2, "g_used": 0.1, "s2": 2.0, "capped": True}
    json.dumps(head)
    nps = as_noise_params([(0.1, 2.0), NoiseParams(0.3, 1.0)])
    assert nps[0].g == 0.1 and nps[1].g == 0.3
    assert float(NoiseParams(1.0, 4.0).sigma(12.0)) == pytest.approx(4.0)


def test_fixture_estimates(frame_iso4000: RawFrame, frame_iso100: RawFrame) -> None:
    ne = estimate_all(frame_iso4000, 4)
    for p in ne:
        assert not p.fallback
        assert 1.0 < p.g < 3.5
    ne = estimate_all(frame_iso100, 4)
    for p in ne:
        assert p.g < 0.2


def test_estimate_all_rejects_odd(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(65, 64)
    with pytest.raises(ValueError):
        estimate_all(fr, 1)


# ---------------------------------------------------------------------------------------
# slow: real samples (designer reference values from DESIGN.md / estimate_noise2)

SAMPLE_EXPECT = {
    # name: (iso, approx g_est per position, s2 per position)
    "P1060444.RW2": (100, [0.0571, 0.0624, 0.0626, 0.0389], [1.28, 4.12, 4.05, 1.37]),
    "PANA9831.RW2": (100, [0.1305, 0.1432, 0.1434, 0.1537], [0.36, 0.53, 2.34, 0.25]),
    "P1060384.RW2": (320, [0.1261, 0.1338, 0.1356, 0.0907], [5.15, 6.09, 5.76, 2.47]),
    "P1037920.RW2": (4000, [2.4138, 2.4135, 2.4119, 1.4889], [5.88, 3.38, 3.27, 5.71]),
    "PANA0003.RW2": (4000, [2.7995, 2.5892, 2.6479, 1.6862], [0.25, 4.11, 0.25, 3.24]),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(SAMPLE_EXPECT))
def test_samples_noise(sample_path: Callable[[str], object], name: str) -> None:
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path(name))
    iso, g_ref, s2_ref = SAMPLE_EXPECT[name]
    assert fr.iso == iso
    t = time.perf_counter()
    ne = estimate_all(fr, 8)
    dt = time.perf_counter() - t
    assert dt < 2.0
    assert ne.model == "auto+iso_cap"
    for k, p in enumerate(ne):
        assert p.g_est == pytest.approx(g_ref[k], rel=0.01)
        assert p.s2 == pytest.approx(s2_ref[k], rel=0.02, abs=0.02)
        cap = noise_table.K_CAM[("Panasonic", "DC-S9")][k] * iso
        assert p.g == pytest.approx(min(p.g_est, cap))
    if name == "PANA9831.RW2":  # textured ISO100 frame: g overestimated ~2.2x, cap applies
        assert all(p.capped for p in ne)
        assert ne[1].g_est / ne[1].g > 2.0
