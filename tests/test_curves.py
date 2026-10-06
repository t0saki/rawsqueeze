"""Tests for rawsqueeze.curves (DESIGN.md 2.2 / test plan item 2)."""

from __future__ import annotations

import numpy as np
import pytest

from rawsqueeze.curves import (
    PlaneQuantizer,
    added_noise_rms,
    build_lut,
    curve_fwd,
    curve_inv,
    curve_params,
    resolve_recon,
    saturation_code,
)
from rawsqueeze.noise import NoiseParams

BLACK, WHITE = 128, 4079
X = WHITE - BLACK
PARAMS = [(0.06, 4.0), (0.6, 4.0), (2.4, 5.9), (0.13, 0.25), (1.0, 30.0)]


@pytest.mark.parametrize("g,s2", PARAMS)
@pytest.mark.parametrize("f", [0.5, 1.0, 2.0, 3.0])
def test_identity_region_exact(g: float, s2: float, f: float) -> None:
    x0, _ = curve_params(g, s2, f)
    xs = np.arange(-50, int(np.ceil(x0)), dtype=np.float64)
    xs = xs[xs < x0]
    assert np.array_equal(curve_fwd(xs, g, s2, f), xs)
    assert np.array_equal(curve_inv(xs, g, s2, f), xs)


@pytest.mark.parametrize("g,s2", PARAMS)
@pytest.mark.parametrize("f", [0.5, 1.0, 2.0])
def test_inverse_and_slope(g: float, s2: float, f: float) -> None:
    xs = np.linspace(-20, X, 5001)
    y = curve_fwd(xs, g, s2, f)
    assert np.allclose(curve_inv(y, g, s2, f), xs, rtol=1e-10, atol=1e-8)
    assert np.all(np.diff(y) > 0)
    # slope = 1/(f*sigma) above x0, never > 1 + eps (no oversampling of shadows)
    slope = np.diff(y) / np.diff(xs)
    assert slope.max() <= 1.0 + 1e-9
    x0, _ = curve_params(g, s2, f)
    xm = 0.5 * (xs[:-1] + xs[1:])
    hi = xs[:-1] > x0 + 1
    sig = np.sqrt(g * xm[hi] + s2)
    assert np.allclose(slope[hi], 1.0 / (f * sig), rtol=2e-2)


def test_continuity_at_x0() -> None:
    g, s2, f = 0.06, 4.0, 1.0
    x0, _ = curve_params(g, s2, f, identity_step=1.0)  # DESIGN.md 2.2 formula (T = 1)
    assert x0 == pytest.approx((1.0 - 4.0) / 0.06 if (1.0 - 4.0) > 0 else 0.0)
    g, s2, f = 0.06, 0.5, 1.0
    x0, _ = curve_params(g, s2, f, identity_step=1.0)
    assert x0 == pytest.approx(0.5 / 0.06)
    assert curve_params(g, s2, f)[0] == pytest.approx(0.5 / 0.06)  # f >= 1: spec rule (T = 1)
    x0d, _ = curve_params(g, s2, 0.5)  # f < 1: identity step 2 DN (integrator change)
    assert x0d == pytest.approx((16.0 - 0.5) / 0.06)
    x0, _ = curve_params(g, s2, f)
    e = 1e-7
    assert curve_fwd(x0 + e, g, s2, f) == pytest.approx(x0, abs=1e-6)


@pytest.mark.parametrize("g,s2", PARAMS)
@pytest.mark.parametrize("f", [0.5, 1.0, 2.0, 3.0])
@pytest.mark.parametrize("offset", [0, 7])
def test_quantizer_table_matches_curve(g: float, s2: float, f: float, offset: int) -> None:
    pq = PlaneQuantizer.create(g, s2, f, BLACK, WHITE, offset)
    xs = np.arange(-offset, X, dtype=np.float64)
    expect = np.rint(curve_fwd(xs, g, s2, f)).astype(np.int64) + offset
    assert np.array_equal(pq.fwd[:-1], expect)
    assert pq.q_sat == saturation_code(g, s2, f, BLACK, WHITE, offset)
    assert pq.q_sat == int(expect[-1]) + 1
    assert pq.fwd[-1] == pq.q_sat
    # no skipped codes (slope <= 1) and monotone
    d = np.diff(pq.fwd)
    assert d.min() >= 0 and d.max() <= 1
    assert pq.dtype == (np.uint8 if pq.q_sat < 256 else np.uint16)


@pytest.mark.parametrize("g,s2", PARAMS)
@pytest.mark.parametrize("f", [0.5, 1.0, 2.0, 3.0])
def test_mid_lut_monotone_and_saturation(g: float, s2: float, f: float) -> None:
    pq = PlaneQuantizer.create(g, s2, f, BLACK, WHITE, 3)
    lut = pq.lut_mid()
    assert lut.dtype == np.uint16
    assert lut.size == pq.q_sat + 1
    assert lut[pq.q_sat] == WHITE
    assert np.all(np.diff(lut.astype(np.int64)) >= 0)
    assert lut[:-1].max() <= WHITE
    # reconstruction of every non-saturated raw value lands in its own bin
    raw = np.arange(BLACK - 3, WHITE, dtype=np.uint16)
    q = pq.quantize(raw.reshape(1, -1)).ravel()
    lo, hi = pq.bin_edges()
    rec = lut[q]
    assert np.all(rec >= lo[q]) and np.all(rec <= hi[q])
    # identity region reconstructs exactly
    x0 = pq.x0
    ident = (raw.astype(np.float64) - BLACK) < x0
    assert np.array_equal(rec[ident], raw[ident])
    # max error <= half a step (+1 DN rounding)
    err = np.abs(rec.astype(np.float64) - raw)
    top = np.maximum(raw.astype(np.float64), rec) - BLACK  # step grows across a bin
    step = f * np.sqrt(g * np.maximum(top, 0) + s2)
    assert np.all(err <= np.maximum(step, 1.0) * 0.5 + 1.0)


def test_saturated_values_get_q_sat() -> None:
    pq = PlaneQuantizer.create(0.6, 4.0, 1.0, BLACK, WHITE, 0)
    raw = np.array([[WHITE - 1, WHITE, WHITE + 1, 4095, 65535]], dtype=np.uint16)
    q = pq.quantize(raw).ravel()
    assert q[0] < pq.q_sat
    assert np.all(q[1:] == pq.q_sat)
    assert np.all(pq.lut_mid()[q[1:]] == WHITE)


def test_negative_offset_reversible() -> None:
    # sensor outputs values below black; identity region extends to negative x
    pq = PlaneQuantizer.create(0.6, 9.0, 1.0, BLACK, WHITE, offset=40)
    raw = np.arange(BLACK - 40, BLACK + 1, dtype=np.uint16).reshape(1, -1)
    q = pq.quantize(raw)
    assert q.min() == 0
    assert np.array_equal(pq.lut_mid()[q], raw)
    with pytest.raises(ValueError):
        pq.quantize(np.array([[BLACK - 41]], dtype=np.uint16))


def test_f_to_zero_degenerates_to_lossless() -> None:
    xs = np.arange(-10, X + 1, dtype=np.float64)
    assert np.array_equal(curve_fwd(xs, 0.6, 4.0, 0.0), xs)
    assert np.array_equal(curve_inv(xs, 0.6, 4.0, 0.0), xs)
    pq = PlaneQuantizer.create(0.6, 4.0, 1e-3, BLACK, WHITE, 0)
    raw = np.arange(BLACK, WHITE, dtype=np.uint16).reshape(1, -1)
    assert np.array_equal(pq.lut_mid()[pq.quantize(raw)], raw)
    with pytest.raises(ValueError):
        PlaneQuantizer.create(0.6, 4.0, 0.0, BLACK, WHITE, 0)


def test_centroid_lut_in_bins_and_monotone() -> None:
    rng = np.random.default_rng(1)
    g, s2, f = 2.4, 5.0, 3.0
    clean = rng.uniform(0, 0.9 * X, size=(256, 256))
    plane = np.clip(np.rint(g * rng.poisson(clean / g) + rng.normal(0, np.sqrt(s2), clean.shape) + BLACK), 0, 4095)
    plane = plane.astype(np.uint16)
    plane[:4, :4] = 4095
    pq = PlaneQuantizer.create(g, s2, f, BLACK, WHITE, max(0, BLACK - int(plane.min())))
    q = pq.quantize(plane)
    lut = pq.lut_centroid(plane, q)
    lo, hi = pq.bin_edges()
    k = np.arange(pq.q_sat)
    assert np.all(lut[k] >= lo[k]) and np.all(lut[k] <= hi[k])
    assert np.all(np.diff(lut.astype(np.int64)) >= 0)
    assert lut[pq.q_sat] == WHITE
    # centroid reduces bias vs mid on skewed bins
    nonsat = plane < WHITE
    e_c = lut[q].astype(np.float64)[nonsat] - plane[nonsat]
    assert abs(e_c.mean()) < 0.1
    # build_lut wrapper agrees
    assert np.array_equal(build_lut(NoiseParams(g, s2), f, BLACK, WHITE, pq.offset, pq.q_sat, "centroid", plane, q), lut)
    assert np.array_equal(build_lut((g, s2), f, BLACK, WHITE, pq.offset, None, "mid"), pq.lut_mid())
    with pytest.raises(ValueError):
        build_lut((g, s2), f, BLACK, WHITE, pq.offset, pq.q_sat + 1, "mid")
    with pytest.raises(ValueError):
        pq.lut("centroid")


def test_resolve_recon_and_misc() -> None:
    assert resolve_recon("auto", 1.0) == "mid"
    assert resolve_recon(None, 2.0) == "centroid"
    assert resolve_recon("mid", 3.0) == "mid"
    with pytest.raises(ValueError):
        resolve_recon("median", 1.0)
    assert added_noise_rms(1.0) == pytest.approx(0.0408, abs=1e-3)
    assert added_noise_rms(2.0) == pytest.approx(0.1547, abs=1e-3)
    with pytest.raises(ValueError):
        curve_params(0.6, 4.0, -1.0)
    with pytest.raises(ValueError):
        curve_params(0.0, 4.0, 1.0)
