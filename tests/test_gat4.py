"""Tests for the experimental gat4 engine (DESIGN.md 2.6)."""

from __future__ import annotations

import io
import json
import sys

import numpy as np
import pytest

from rawsqueeze.container import head_to_json, make_head_chunk, read_rsq, write_rsq
from rawsqueeze.engines import EngineParams, get_engine
from rawsqueeze.engines.gat4 import K_FIXED, gat_fwd, gat_inv, plane_k


def _roundtrip(frame, params):
    eng = get_engine("gat4")
    out = eng.encode(frame, params)
    head = frame.head_sections()
    head.update(engine="gat4", codec=out.codec, **out.head_extra)
    buf = io.BytesIO()
    write_rsq(buf, [make_head_chunk(head)] + out.chunks)
    f = read_rsq(buf.getvalue())
    return out, f.head, f.chunks, eng.decode(f.head, f.chunks, threads=4)


def test_gat_inverse_exact():
    for g, s2 in ((0.06, 4.0), (2.4, 5.0), (0.6, 0.25)):
        floor = -(0.375 * g + s2 / g)  # below this the transform clamps to y = 0
        x = np.linspace(max(-5.0, floor + 1e-3), 4000, 1001)
        y = gat_fwd(x, g, s2)
        assert np.allclose(gat_inv(y, g, s2), x, atol=1e-6)
        assert gat_fwd(np.float64(floor - 1), g, s2) == 0.0


def test_gat_unit_variance():
    rng = np.random.default_rng(0)
    g, s2, x = 0.6, 4.0, 500.0
    raw = g * rng.poisson(x / g, 200000) + rng.normal(0, np.sqrt(s2), 200000)
    assert gat_fwd(raw, g, s2).std() == pytest.approx(1.0, rel=0.03)


def test_plane_k_fixed_and_overflow_cap():
    assert plane_k(0.06, 4.0, 3951) == K_FIXED
    k = plane_k(0.01, 0.25, 3951)
    assert k < K_FIXED
    assert gat_fwd(np.float64(3951), 0.01, 0.25) * k <= 65535


@pytest.mark.parametrize("g,s2", [(0.06, 2.0), (2.4, 5.0)])
def test_roundtrip_error_relative_to_noise(synthetic_frame_factory, g, s2):
    fr = synthetic_frame_factory(256, 256, g=g, s2=s2, saturate_frac=0.02)
    noise = [(g, s2)] * 4
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, noise=noise, threads=4))
    assert {"G4P0", "G4P1", "G4P2", "G4P3", "SATM"} <= set(chunks)
    blk = head["codec"]["gat4"]
    assert blk["K"] == 100.0 and len(blk["planes"]) == 4
    sat = fr.mosaic >= fr.white
    assert np.all(rec[sat] == fr.white)
    e = (rec.astype(np.int64) - fr.mosaic.astype(np.int64))[~sat]
    x = fr.mosaic[~sat].astype(np.float64) - 128
    sigma = np.sqrt(g * np.maximum(x, 0) + s2)
    rel = e / sigma
    assert abs(rel.mean()) < 0.1
    assert np.sqrt((rel**2).mean()) < 1.0


def test_noise_param_objects_and_dicts(synthetic_frame_factory):
    class P:
        def __init__(self, g, s2):
            self.g, self.s2 = g, s2

    fr = synthetic_frame_factory(64, 64)
    eng = get_engine("gat4")
    a = eng.encode(fr, EngineParams(quality=0.2, noise=[P(0.6, 4.0)] * 4, threads=1))
    b = eng.encode(fr, EngineParams(quality=0.2, noise=[{"g": 0.6, "s2": 4.0}] * 4, threads=1))
    assert [c.payload for c in a.chunks] == [c.payload for c in b.chunks]


def test_requires_noise(synthetic_frame_factory):
    with pytest.raises(ValueError, match="noise"):
        get_engine("gat4").encode(synthetic_frame_factory(64, 64), EngineParams(quality=0.2))


def test_rejects_non_bayer(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64, pattern=((0, 1), (2, 3)), color_desc="CMYG")
    with pytest.raises(ValueError, match="Bayer"):
        get_engine("gat4").encode(fr, EngineParams(quality=0.2, noise=[(0.6, 4.0)] * 4))


def test_decode_without_rawpy_and_json_head(synthetic_frame_factory, monkeypatch):
    fr = synthetic_frame_factory(64, 64, saturate_frac=0.05)
    eng = get_engine("gat4")
    out = eng.encode(fr, EngineParams(quality=0.2, noise=[(0.6, 4.0)] * 4, threads=1))
    head = json.loads(head_to_json({**fr.head_sections(), "codec": out.codec}))
    for p, q in zip(head["codec"]["gat4"]["planes"], out.codec["gat4"]["planes"]):
        assert p == q  # float64 exact through JSON
    monkeypatch.setitem(sys.modules, "rawpy", None)
    rec = eng.decode(head, {c.name: c.payload for c in out.chunks})
    assert rec.shape == (64, 64) and rec.dtype == np.uint16


def test_low_g_uses_capped_k(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64, g=0.01, s2=0.25)
    out, head, _, rec = _roundtrip(fr, EngineParams(quality=0.2, noise=[(0.01, 0.25)] * 4, threads=1))
    ks = [p["K"] for p in head["codec"]["gat4"]["planes"]]
    assert all(k < 100 for k in ks)
    e = rec.astype(np.int64) - fr.mosaic
    assert abs(e.mean()) < 2


@pytest.mark.slow
def test_full_sample_p1060444(sample_path):
    from rawsqueeze.noise import estimate_all
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path("P1060444.RW2"), want_exif=True)
    noise = list(estimate_all(fr, threads=8))
    out, head, chunks, rec = _roundtrip(fr, EngineParams(quality=0.2, noise=noise, threads=8))
    total = sum(len(c.payload) for c in out.chunks if c.name != "SATM")
    assert 6.5e6 < total < 8.5e6  # measured 7.58 MB; B's prototype (K=65535/ymax) 7.73 MB
    sat = fr.mosaic >= fr.white
    assert int((rec[sat] != fr.white).sum()) == 0
    e = rec.astype(np.int64) - fr.mosaic
    assert abs(e.mean()) < 0.5
