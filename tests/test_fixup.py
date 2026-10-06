"""Tests for the half3/gat4 max-error guard (sparse ``H3FX`` fix-up chunk)."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from rawsqueeze import cfa
from rawsqueeze.container import RsqCRCError, RsqFormatError, make_head_chunk, read_rsq, write_rsq
from rawsqueeze.engines import EngineParams, get_engine
from rawsqueeze.engines.half3 import (
    FIXUP_DEFAULT_K,
    FIXUP_DEFAULT_T,
    _varint_decode,
    _varint_encode,
    fixup_options,
    fixup_select,
    pack_fixup,
    unpack_fixup,
)
from rawsqueeze.presets import resolve

NOISE = (0.06, 2.0)


class _NP:
    def __init__(self, g: float, s2: float) -> None:
        self.g, self.s2 = g, s2


def _dclamp_frame(factory, n_sites: int = 60, seed: int = 3):
    """Gradient frame with isolated quads where G2 >> G1 (G1 - G2 < -0.5: libjxl clamps D)."""
    fr = factory(256, 256, g=NOISE[0], s2=NOISE[1])
    roles = fr.color_roles()
    rng = np.random.default_rng(seed)
    (y1, x1), (y2, x2) = cfa.POSITIONS[roles["G1"]], cfa.POSITIONS[roles["G2"]]
    for _ in range(n_sites):
        qy, qx = 2 * int(rng.integers(4, 124)), 2 * int(rng.integers(4, 124))
        fr.mosaic[qy + y1, qx + x1] = 128 + 120
        fr.mosaic[qy + y2, qx + x2] = 128 + 3700
    return fr


def _encode(fr, engine="half3", **extra):
    eng = get_engine(engine)
    noise = [_NP(*NOISE)] * 4
    out = eng.encode(fr, EngineParams(quality=0.2, threads=2, noise=noise, extra=extra))
    head = fr.head_sections()
    head.update(engine=engine, codec=out.codec, **out.head_extra)
    buf = io.BytesIO()
    write_rsq(buf, [make_head_chunk(head)] + out.chunks)
    f = read_rsq(buf.getvalue())
    return out, f, eng.decode(f.head, f.chunks, threads=2), buf.getvalue()


def _bound(fr, k=FIXUP_DEFAULT_K, t=FIXUP_DEFAULT_T):
    blk = np.array(fr.black_per_position, float)
    thr = np.empty(fr.mosaic.shape)
    for p, (dy, dx) in enumerate(cfa.POSITIONS):
        x = np.minimum(fr.mosaic[dy::2, dx::2], fr.white).astype(float) - blk[p]
        thr[dy::2, dx::2] = np.maximum(t * (fr.white - blk[p]), k * np.sqrt(np.maximum(NOISE[0] * x + NOISE[1], 0)))
    return thr


def test_guard_bounds_max_error(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    valid = fr.mosaic < fr.white
    _, f_off, rec_off, _ = _encode(fr, fixup=False)
    err_off = np.abs(rec_off.astype(np.int64) - fr.mosaic)
    assert "H3FX" not in f_off.chunks and "fixup" not in f_off.head["codec"]["half3"]
    assert err_off[valid].max() > 400  # the D clamp really produces large errors here
    out, f_on, rec_on, _ = _encode(fr)
    err_on = np.abs(rec_on.astype(np.int64) - fr.mosaic)
    assert np.all(err_on[valid] <= _bound(fr)[valid] + 1e-9)
    rec = f_on.head["codec"]["half3"]["fixup"]
    assert rec["n"] > 0 and rec["n"] == rec["n_over"] and not rec["capped"]
    assert rec["err_max_before"] == err_off[valid].max()
    assert rec["err_max_after"] == err_on[valid].max()
    assert rec["k"] == FIXUP_DEFAULT_K and rec["t"] == FIXUP_DEFAULT_T and rec["noise"] is True
    assert "H3FX" in f_on.chunks
    # the encoder's in-process reconstruction is exactly what the decoder produces
    assert out.recon is not None and np.array_equal(out.recon, rec_on)
    # untouched pixels are identical with and without the guard
    changed = rec_on != rec_off
    assert changed.sum() == rec["n"] and np.all(rec_on[changed] == fr.mosaic[changed])
    assert np.all(rec_on[fr.mosaic >= fr.white] == fr.white)


def test_guard_tunable_threshold(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    _, f1, rec1, _ = _encode(fr, fixup_t=0.01, fixup_k=0)
    valid = fr.mosaic < fr.white
    assert not f1.head["codec"]["half3"]["fixup"]["capped"]
    assert np.abs(rec1.astype(np.int64) - fr.mosaic)[valid].max() <= 0.01 * (fr.white - 128)
    assert f1.head["codec"]["half3"]["fixup"]["noise"] is True  # params present, k = 0 disables the term
    n_default = _encode(fr)[1].head["codec"]["half3"]["fixup"]["n"]
    assert f1.head["codec"]["half3"]["fixup"]["n"] > n_default


def test_missing_h3fx_old_files_decode(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    out, f, rec, _ = _encode(fr)
    eng = get_engine("half3")
    # an old file (no guard): no H3FX chunk and no fixup record -> plain decode, no error
    head_old = dict(f.head)
    head_old["codec"] = {**f.head["codec"], "half3": {k: v for k, v in f.head["codec"]["half3"].items() if k != "fixup"}}
    chunks_old = {k: v for k, v in f.chunks.items() if k != "H3FX"}
    rec_old = eng.decode(head_old, chunks_old)
    _, _, rec_off, _ = _encode(fr, fixup=False)
    assert np.array_equal(rec_old, rec_off)
    # HEAD records a fix-up but the chunk was dropped -> error, not silent quality loss
    with pytest.raises(ValueError, match="H3FX"):
        eng.decode(f.head, chunks_old)


def test_no_fixup_chunk_when_nothing_exceeds(synthetic_frame_factory):
    fr = synthetic_frame_factory(64, 64, g=1e-3, s2=0.01, signal="flat")
    out, f, rec, _ = _encode(fr)
    assert "H3FX" not in f.chunks
    assert f.head["codec"]["half3"]["fixup"]["n"] == 0
    assert np.array_equal(out.recon, rec)


def test_corrupt_h3fx_caught_by_crc(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    _, f, _, blob = _encode(fr)
    e = next(x for x in f.entries if x.fourcc == "H3FX")
    bad = bytearray(blob)
    bad[e.offset + 16 + e.length // 2] ^= 0x5A
    with pytest.raises(RsqCRCError, match="H3FX"):
        read_rsq(bytes(bad))


def test_h3fx_is_critical_zstd(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    _, f, _, _ = _encode(fr)
    e = next(x for x in f.entries if x.fourcc == "H3FX")
    assert e.critical and e.zstd


def test_pack_unpack_roundtrip():
    rng = np.random.default_rng(0)
    H, W = 4000, 6016
    idx = np.unique(rng.integers(0, H * W, 5000))
    idx = np.r_[0, idx[1:-1], H * W - 1]  # first and last pixel, deltas from 1 to > 2^21
    vals = rng.integers(0, 65536, idx.size)
    b = pack_fixup(idx, vals, (H, W))
    i2, v2 = unpack_fixup(b, (H, W))
    assert np.array_equal(i2, idx) and np.array_equal(v2, vals) and v2.dtype == np.uint16
    i3, v3 = unpack_fixup(pack_fixup(np.zeros(0, np.int64), np.zeros(0), (H, W)), (H, W))
    assert i3.size == 0 and v3.size == 0


def test_varint_edges():
    v = np.array([0, 1, 127, 128, 16383, 16384, 2**28 - 1, 2**28, 2**32 - 1], dtype=np.uint64)
    b = np.frombuffer(_varint_encode(v), np.uint8)
    out, used = _varint_decode(b, v.size)
    assert used == b.size and np.array_equal(out, v)


def test_unpack_rejects_bad_payloads():
    good = pack_fixup(np.array([5, 9, 100]), np.array([1, 2, 3]), (16, 16))
    with pytest.raises(RsqFormatError, match="mosaic"):
        unpack_fixup(good, (16, 18))
    with pytest.raises(RsqFormatError):
        unpack_fixup(good[:-1], (16, 16))
    with pytest.raises(RsqFormatError):
        unpack_fixup(good[:10], (16, 16))
    with pytest.raises(RsqFormatError, match="version"):
        unpack_fixup(b"\x07" + good[1:], (16, 16))
    # index past the end of the mosaic / duplicate index (hand-made payloads)
    hdr = struct.Struct("<BBHIII")
    with pytest.raises(RsqFormatError, match="outside"):
        unpack_fixup(hdr.pack(1, 0, 0, 1, 16, 16) + _varint_encode(np.array([256])) + b"\x01\x00", (16, 16))
    with pytest.raises(RsqFormatError, match="increasing"):
        unpack_fixup(hdr.pack(1, 0, 0, 2, 16, 16) + _varint_encode(np.array([3, 0])) + b"\x01\x02\x00\x00", (16, 16))
    with pytest.raises(ValueError):
        pack_fixup(np.array([9, 5]), np.array([1, 2]), (16, 16))


def test_select_cap_keeps_worst():
    H, W = 64, 64
    orig = np.full((H, W), 1000, np.uint16)
    rec = orig.copy()
    rng = np.random.default_rng(1)
    idx = rng.choice(H * W, 100, replace=False)
    errs = np.arange(200, 300)
    rec.ravel()[idx] = 1000 + errs
    sel, st = fixup_select(orig, rec, 4079, [128] * 4, None, k=0, t=0.01, max_frac=10 / (H * W), min_cap=0)
    assert st["n_over"] == 100 and st["capped"] and sel.size == 10
    assert set(sel.tolist()) == set(idx[-10:].tolist())
    assert st["err_max_before"] == 299 and st["err_max_after"] == 289
    assert np.all(np.diff(sel) > 0)


def test_select_respects_saturation_and_noise():
    orig = np.full((8, 8), 1000, np.uint16)
    orig[0, 0] = 4095  # saturated: SATM restores it, never patched
    rec = orig.copy()
    rec[0, 0] = 3000
    rec[2, 2] = 1000 + 200  # 200 DN error
    noisy = [(10.0, 0.0)] * 4  # sigma = sqrt(10 * 872) ~ 93 DN -> 8 sigma ~ 747 DN
    sel, _ = fixup_select(orig, rec, 4079, [128] * 4, noisy, k=8, t=0.04)
    assert sel.size == 0
    sel, _ = fixup_select(orig, rec, 4079, [128] * 4, None, k=8, t=0.04)
    assert sel.tolist() == [2 * 8 + 2]
    sel, _ = fixup_select(orig, rec, 4079, [128] * 4, None, k=8, t=0.04, masked=False)
    assert sel.tolist() == [0, 2 * 8 + 2]


def test_fixup_options_and_presets():
    assert fixup_options({}) == (True, FIXUP_DEFAULT_K, FIXUP_DEFAULT_T) == (True, 8.0, 0.04)
    assert fixup_options({}, 0.3) == (True, 8.0, 0.06) and fixup_options({"fixup_t": 0.05}, 0.3)[2] == 0.05
    assert fixup_options({"fixup": False, "fixup_k": 4, "fixup_t": 0.02}) == (False, 4.0, 0.02)
    with pytest.raises(ValueError):
        fixup_options({"fixup_t": 0})
    p = resolve(engine="half3", fixup=False, fixup_k=6, fixup_t=0.03)
    assert p.extra == {"fixup": False, "fixup_k": 6.0, "fixup_t": 0.03}
    with pytest.raises(ValueError, match="fixup_t"):
        resolve(fixup_t=0)
    with pytest.raises(ValueError, match="fixup_k"):
        resolve(fixup_k=-1)


def test_gat4_guard(synthetic_frame_factory):
    fr = _dclamp_frame(synthetic_frame_factory)
    out, f, rec, _ = _encode(fr, engine="gat4", fixup_t=0.005, fixup_k=2)
    blk = f.head["codec"]["gat4"]
    assert blk["fixup"]["n"] > 0 and "H3FX" in f.chunks
    assert np.array_equal(out.recon, rec)
    valid = fr.mosaic < fr.white
    assert np.abs(rec.astype(np.int64) - fr.mosaic)[valid].max() <= blk["fixup"]["err_max_after"]
    _, f0, rec0, _ = _encode(fr, engine="gat4", fixup=False)
    assert "H3FX" not in f0.chunks
    assert blk["fixup"]["err_max_before"] == np.abs(rec0.astype(np.int64) - fr.mosaic)[valid].max()
