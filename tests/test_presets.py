"""Tests for rawsqueeze.presets (DESIGN.md 4)."""

from __future__ import annotations

import pytest

from rawsqueeze.presets import PRESETS, EncodeParams, resolve


def test_preset_table_matches_spec() -> None:
    assert set(PRESETS) == {"lossless", "archival", "high", "vl", "compact"}
    assert (PRESETS["high"].d, PRESETS["high"].f) == (0.1, 0.5)
    assert (PRESETS["vl"].d, PRESETS["vl"].f) == (0.2, 1.0)
    assert (PRESETS["compact"].d, PRESETS["compact"].f) == (0.3, 2.0)
    assert PRESETS["lossless"].lossless and PRESETS["archival"].lossless
    assert PRESETS["archival"].keep_preview == "small"


def test_default_is_vl_auto() -> None:
    p = resolve()
    assert isinstance(p, EncodeParams)
    assert (p.preset, p.engine, p.d, p.f) == ("vl", "auto", 0.2, 1.0)
    assert not p.lossless
    assert p.quality_for("half3") == 0.2 and p.quality_for("nlq") == 1.0
    assert p.param_string("half3") == "d0.2" and p.param_string("nlq") == "f1"


@pytest.mark.parametrize("preset", ["lossless", "archival"])
def test_lossless_presets_force_lossless_engine(preset: str) -> None:
    p = resolve(preset=preset)
    assert p.engine == "lossless" and p.lossless and p.f == 0.0
    assert p.param_string("lossless") == "lossless"
    with pytest.raises(ValueError):
        resolve(preset=preset, engine="half3")


def test_quality_is_engine_relative() -> None:
    assert resolve(engine="half3", quality=0.35).d == 0.35
    assert resolve(engine="gat4", q=0.4).d == 0.4
    p = resolve(engine="nlq", quality=2.5)
    assert p.f == 2.5 and p.d == 0.2
    with pytest.raises(ValueError, match="--d and --f"):
        resolve(quality=1.0)  # auto engine
    with pytest.raises(ValueError):
        resolve(engine="lossless", quality=1.0)


def test_pair_overrides_and_nlq_f0_is_lossless() -> None:
    p = resolve({"preset": "compact", "d": 0.15, "f": 1.5})
    assert (p.d, p.f) == (0.15, 1.5)
    q = resolve(engine="nlq", f=0)
    assert q.engine == "lossless" and q.lossless


def test_options_and_validation() -> None:
    p = resolve(effort=7, layout="stack4", recon="centroid", noise_model="manual:0.5,3", snr_threshold=55,
                matrix=False, satmask=False, threads=3, keep_preview="full", meta=False, recon_hash=True, gat4_K=900)
    assert (p.effort, p.layout, p.recon, p.noise_model, p.snr_threshold) == (7, "stack4", "centroid", "manual:0.5,3", 55.0)
    assert not p.use_matrix and not p.satmask and not p.store_meta and p.recon_hash
    assert p.threads == 3 and p.keep_preview == "full" and p.extra == {"gat4_K": 900}
    for bad in ({"preset": "nope"}, {"engine": "x"}, {"effort": 12}, {"layout": "x"}, {"recon": "x"},
                {"noise_model": "bogus"}, {"keep_preview": "x"}, {"d": 0}, {"f": -1}, {"d": "abc"}):
        with pytest.raises(ValueError):
            resolve(bad)


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValueError, match="unknown option 'qualty'; did you mean 'quality'"):
        resolve(preset="vl", qualty=0.5)
    with pytest.raises(ValueError, match="'enigne'.*'engine'"):
        resolve({"enigne": "half3", "d": 0.2})
    with pytest.raises(ValueError, match="also unknown"):
        resolve(dd=0.1, keep_prview="full")
    assert resolve(foo=None).extra == {}  # None means "not given"


def test_none_values_mean_not_given() -> None:
    p = resolve(preset="high", d=None, f=None, effort=None, keep_preview=None)
    assert (p.d, p.f, p.effort, p.keep_preview) == (0.1, 0.5, None, "none")
