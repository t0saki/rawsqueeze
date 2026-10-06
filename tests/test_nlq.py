"""Tests for the nlq engine, lossy mode (DESIGN.md 2.2 / test plan item 4)."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from rawsqueeze import jxl
from rawsqueeze.cfa import split_planes
from rawsqueeze.container import head_to_json, make_head_chunk, read_rsq, unpack_luts, write_rsq
from rawsqueeze.engines import EngineParams, get_engine
from rawsqueeze.engines.nlq import ENGINE, decode_mosaic, encode_mosaic, resolve_layout
from rawsqueeze.noise import NoiseParams, estimate_all
from rawsqueeze.rawio import RawFrame


def true_noise(g: float, s2: float) -> list[NoiseParams]:
    return [NoiseParams(g, s2) for _ in range(4)]


def roundtrip(frame: RawFrame, params: EngineParams) -> tuple[np.ndarray, dict[str, Any], dict[str, bytes]]:
    """Encode with ENGINE, store in a real .rsq container, read back and decode."""
    out = ENGINE.encode(frame, params)
    head: dict[str, Any] = frame.head_sections()
    head["codec"] = out.codec
    head.update(out.head_extra)
    buf = io.BytesIO()
    write_rsq(buf, [make_head_chunk(head)] + out.chunks)
    rf = read_rsq(buf.getvalue())
    rec = ENGINE.decode(rf.head, rf.chunks, threads=2)
    assert out.recon is not None
    assert np.array_equal(rec, out.recon)  # LUT reconstruction is exact
    return rec, rf.head, rf.chunks


def z_errors(frame: RawFrame, rec: np.ndarray, noise: list[NoiseParams]) -> tuple[np.ndarray, np.ndarray]:
    P = split_planes(frame.mosaic).astype(np.float64)
    R = split_planes(rec).astype(np.float64)
    zs, es = [], []
    for k in range(4):
        blk = frame.black_per_position[k]
        ok = P[k] < frame.white
        x = P[k][ok] - blk
        sig = np.sqrt(noise[k].g * np.maximum(x, 0) + noise[k].s2)
        e = R[k][ok] - P[k][ok]
        zs.append(e / sig)
        es.append(e)
    return np.concatenate(zs), np.concatenate(es)


@pytest.mark.parametrize("f", [1.0, 2.0, 3.0])
def test_error_matches_theory(synthetic_frame_factory: Callable[..., RawFrame], f: float) -> None:
    g, s2 = 2.4, 4.0
    fr = synthetic_frame_factory(1024, 1024, g=g, s2=s2, seed=4)
    noise = true_noise(g, s2)
    rec, head, _ = roundtrip(fr, EngineParams(quality=f, noise=noise, threads=4))
    z, e = z_errors(fr, rec, noise)
    rms = float(np.sqrt(np.mean(z**2)))
    assert rms == pytest.approx(f / np.sqrt(12), rel=0.10)
    assert abs(float(np.mean(z))) < 0.02 * f
    assert head["mode"] == "lossy"
    assert head["codec"]["nlq"]["recon"] == ("centroid" if f >= 2 else "mid")


def test_uint8_path_and_head(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(256, 256, g=2.4, s2=4.0, seed=1)
    out = ENGINE.encode(fr, EngineParams(quality=1.0, noise=true_noise(2.4, 4.0), threads=2))
    nlq = out.codec["nlq"]
    assert out.codec["layout"] == "planes" and out.codec["effort"] == 3
    names = [c.name for c in out.chunks]
    assert names == ["LUTS", "PLN0", "PLN1", "PLN2", "PLN3"]
    for k, rec in enumerate(nlq["planes"]):
        assert rec["q_sat"] < 256 and rec["dtype"] == "uint8"
        assert rec["offset"] >= 0 and rec["black"] == 128
        assert jxl.decode(out.chunks[1 + k].payload).dtype == np.uint8
    luts = unpack_luts(out.chunks[0].payload)
    assert [lut.size for lut in luts] == [r["q_sat"] + 1 for r in nlq["planes"]]
    assert out.chunks[0].zstd and out.chunks[0].critical and not out.chunks[1].zstd
    json.loads(head_to_json({"codec": out.codec, **out.head_extra}))
    # low noise -> uint16 codes
    fr2 = synthetic_frame_factory(256, 256, g=0.06, s2=4.0, seed=1)
    out2 = ENGINE.encode(fr2, EngineParams(quality=1.0, noise=true_noise(0.06, 4.0), threads=2))
    assert all(r["dtype"] == "uint16" and r["q_sat"] >= 256 for r in out2.codec["nlq"]["planes"])


def test_saturation_restored(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(512, 512, g=0.6, s2=4.0, saturate_frac=0.05, seed=2)
    m = fr.mosaic
    m[300:310, 300:310] = fr.white  # exactly white
    m[320:330, 300:310] = fr.white - 1  # just below white: must stay below
    rec, _, _ = roundtrip(fr, EngineParams(quality=2.0, noise=true_noise(0.6, 4.0), threads=2))
    sat = m >= fr.white
    assert sat.sum() > 1000
    assert np.all(rec[sat] == fr.white)
    assert np.all(rec[~sat] < fr.white)
    assert rec.max() == fr.white


def test_negative_values_below_black(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(256, 256, g=0.6, s2=16.0, signal="zeros", seed=3)
    below = fr.mosaic < 128
    assert below.sum() > 1000
    rec, head, _ = roundtrip(fr, EngineParams(quality=1.0, noise=true_noise(0.6, 16.0), threads=2))
    offs = [p["offset"] for p in head["codec"]["nlq"]["planes"]]
    assert all(o > 0 for o in offs)
    assert offs[0] == 128 - int(fr.mosaic[0::2, 0::2].min())
    # identity region extends into negative x: exact
    assert np.array_equal(rec[below], fr.mosaic[below])


def test_centroid_lut_within_bins(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(512, 512, g=2.4, s2=4.0, seed=5, saturate_frac=0.01)
    noise = true_noise(2.4, 4.0)
    enc = encode_mosaic(fr.mosaic, black=fr.black_per_position, white=fr.white, f=3.0, noise=noise, recon="centroid")
    luts = unpack_luts(enc.chunks[0].payload)
    assert enc.quantizers is not None
    for pq, lut in zip(enc.quantizers, luts):
        lo, hi = pq.bin_edges()
        k = np.arange(pq.q_sat)
        assert np.all(lut[k] >= lo[k]) and np.all(lut[k] <= hi[k])
        assert np.all(np.diff(lut.astype(np.int64)) >= 0)
        assert lut[pq.q_sat] == fr.white
    # centroid is less biased than mid at f=3
    enc_mid = encode_mosaic(fr.mosaic, black=fr.black_per_position, white=fr.white, f=3.0, noise=noise, recon="mid")
    _, e_c = z_errors(fr, enc.recon, noise)
    _, e_m = z_errors(fr, enc_mid.recon, noise)
    assert abs(e_c.mean()) <= abs(e_m.mean()) + 0.05


def test_stack4_layout(synthetic_frame_factory: Callable[..., RawFrame]) -> None:
    fr = synthetic_frame_factory(256, 384, g=0.6, s2=4.0, seed=6)
    noise = true_noise(0.6, 4.0)
    assert resolve_layout(None, 5) == "stack4" and resolve_layout(None, 3) == "planes"
    rec, head, chunks = roundtrip(fr, EngineParams(quality=1.0, noise=noise, effort=5, threads=2))
    assert head["codec"]["layout"] == "stack4" and head["codec"]["effort"] == 5
    assert "PLNS" in chunks and "PLN0" not in chunks
    rec_p, _, _ = roundtrip(fr, EngineParams(quality=1.0, noise=noise, threads=2))
    assert np.array_equal(rec, rec_p)  # layout does not change the reconstruction
    # explicit layout overrides effort
    out = ENGINE.encode(fr, EngineParams(quality=1.0, noise=noise, effort=5, layout="planes", threads=2))
    assert out.codec["layout"] == "planes"
    with pytest.raises(ValueError):
        resolve_layout("tiles", 3)


def test_noise_estimated_when_missing(frame_iso4000: RawFrame) -> None:
    out = ENGINE.encode(frame_iso4000, EngineParams(quality=1.0, threads=2))
    noise = out.head_extra["noise"]
    assert noise["model"] == "auto+iso_cap" and noise["iso"] == 4000
    assert len(noise["planes"]) == 4
    assert [p["g"] for p in out.codec["nlq"]["planes"]] == [p["g_used"] for p in noise["planes"]]


def test_fixture_roundtrips(fixture_frames: dict[str, RawFrame]) -> None:
    for fr in fixture_frames.values():
        ne = estimate_all(fr, 2)
        for f in (0.5, 1.0, 2.0):
            rec, head, _ = roundtrip(fr, EngineParams(quality=f, noise=ne, threads=2))
            sat = fr.mosaic >= fr.white
            assert np.all(rec[sat] == fr.white)
            assert rec.dtype == np.uint16 and rec.shape == fr.mosaic.shape
            assert head["noise"]["planes"][0]["g_used"] == ne[0].g


def test_registry_and_errors(synthetic_frame: RawFrame) -> None:
    assert get_engine("nlq") is ENGINE
    assert get_engine("lossless") is ENGINE
    with pytest.raises(ValueError):
        encode_mosaic(synthetic_frame.mosaic, black=[128] * 4, white=4079, f=1.0, noise=None)
    with pytest.raises(ValueError):
        encode_mosaic(synthetic_frame.mosaic[:-1], black=[128] * 4, white=4079, f=0.0)
    with pytest.raises(ValueError):
        encode_mosaic(synthetic_frame.mosaic, black=[128] * 4, white=4079, f=-1.0)
    enc = encode_mosaic(synthetic_frame.mosaic, black=[128] * 4, white=4079, f=1.0, noise=true_noise(0.6, 4.0))
    chunks = {c.name: c.payload for c in enc.chunks}
    h, w = synthetic_frame.mosaic.shape
    bad = dict(chunks)
    del bad["LUTS"]
    with pytest.raises(ValueError):
        decode_mosaic(enc.codec, h, w, bad)
    bad = dict(chunks)
    del bad["PLN2"]
    with pytest.raises(ValueError):
        decode_mosaic(enc.codec, h, w, bad)
    with pytest.raises(ValueError):
        decode_mosaic(enc.codec, h + 2, w, chunks)
    short = dict(chunks)
    luts = unpack_luts(chunks["LUTS"])
    from rawsqueeze.container import pack_luts

    short["LUTS"] = pack_luts([lut[:10] for lut in luts])
    with pytest.raises(ValueError):
        decode_mosaic(enc.codec, h, w, short)


def test_decoder_does_not_use_rawpy(monkeypatch: pytest.MonkeyPatch, synthetic_frame: RawFrame) -> None:
    import rawpy

    out = ENGINE.encode(synthetic_frame, EngineParams(quality=1.0, noise=true_noise(0.6, 4.0), threads=1))
    head = {"mosaic": {"height": 256, "width": 256}, "codec": out.codec}
    chunks = {c.name: c.payload for c in out.chunks}

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("decoder touched rawpy")

    monkeypatch.setattr(rawpy, "imread", boom)
    assert np.array_equal(ENGINE.decode(head, chunks), out.recon)


# ---------------------------------------------------------------------------------------
# slow: full samples

NLQ_EXPECT = {"P1060444.RW2": 11.83e6, "P1037920.RW2": 8.53e6}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(NLQ_EXPECT))
def test_samples_nlq_f1(sample_path: Callable[[str], object], name: str) -> None:
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path(name))
    ne = estimate_all(fr, 8)
    t = time.perf_counter()
    out = ENGINE.encode(fr, EngineParams(quality=1.0, noise=ne, threads=8))
    t_enc = time.perf_counter() - t
    chunks = {c.name: c.payload for c in out.chunks}
    head = {"mosaic": {"height": fr.height, "width": fr.width}, "codec": out.codec}
    t = time.perf_counter()
    rec = ENGINE.decode(head, chunks, threads=8)
    t_dec = time.perf_counter() - t
    assert np.array_equal(rec, out.recon)
    size = sum(len(c.payload) for c in out.chunks if c.name != "LUTS")
    assert size == pytest.approx(NLQ_EXPECT[name], rel=0.10)
    assert t_enc < 2.0 and t_dec < 1.0
    z, e = z_errors(fr, rec, list(ne))
    rms = float(np.sqrt(np.mean(z**2)))
    assert 0.27 < rms < 0.33
    assert abs(float(e.mean())) < 0.1
    sat = fr.mosaic >= fr.white
    assert np.all(rec[sat] == fr.white)
    assert np.all(rec[~sat] < fr.white)
