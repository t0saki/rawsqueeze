"""Tests for rawsqueeze.pipeline (HEAD assembly, encode/decode glue) on fixtures."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import sys
from collections.abc import Callable

import numpy as np
import pytest

import rawsqueeze
from rawsqueeze.container import make_head_chunk, read_rsq, write_rsq
from rawsqueeze.pipeline import (
    EncodeError,
    IntegrityError,
    bench_decode_fn,
    bench_encode_fn,
    decode_chunks,
    decode_mosaic,
    encode_frame,
    encode_frame_ex,
)
from rawsqueeze.presets import resolve
from rawsqueeze.rawio import RawFrame
from rawsqueeze.select import DEFAULT_SNR_THRESHOLD


def _roundtrip(chunks: list) -> tuple[bytes, object]:
    bio = io.BytesIO()
    write_rsq(bio, chunks)
    blob = bio.getvalue()
    return blob, read_rsq(blob)


def test_public_api_exports() -> None:
    for name in ("encode_file", "decode_file", "encode_frame", "decode_mosaic", "verify_file",
                 "EncodeReport", "DecodeReport", "VerifyReport", "EncodeParams"):
        assert getattr(rawsqueeze, name) is not None
    assert rawsqueeze.__version__ == "0.1.0"
    with pytest.raises(AttributeError):
        rawsqueeze.no_such_thing  # noqa: B018


def test_lossless_roundtrip_and_head(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(preset="lossless", threads=4))
    h = enc.head
    assert enc.chunks[0].name == "HEAD"
    assert h["format"] == "rsq" and h["version"] == [1, 0] and h["encoder"] == "rawsqueeze 0.1.0"
    assert (h["mode"], h["engine"], h["preset"]) == ("lossless", "nlq", "lossless")
    assert "noise" not in h
    assert h["mosaic"]["sha256"] == frame_iso100.mosaic_sha256() == h["mosaic"]["recon_sha256"]
    for k in ("codec", "source", "mosaic", "cfa", "levels", "color", "geometry", "meta", "selection"):
        assert k in h
    assert h["codec"]["libjxl_version"] and h["codec"]["imagecodecs"]
    assert h["source"]["iso"] == 100
    _, rsq = _roundtrip(enc.chunks)
    rec = decode_mosaic(rsq)
    assert rec.dtype == np.uint16 and np.array_equal(rec, frame_iso100.mosaic)


def test_head_is_json_roundtrip_exact(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(engine="half3", threads=4))
    _, rsq = _roundtrip(enc.chunks)
    assert rsq.head == json.loads(json.dumps(enc.head))
    assert rsq.head["codec"]["half3"]["M"] == enc.head["codec"]["half3"]["M"]


def test_auto_selection_on_fixtures(frame_iso100: RawFrame, frame_iso4000: RawFrame) -> None:
    lo = encode_frame_ex(frame_iso100, resolve(threads=4))
    hi = encode_frame_ex(frame_iso4000, resolve(threads=4))
    assert lo.engine == "half3" and hi.engine == "nlq"
    assert lo.head["noise"]["snr18"] >= DEFAULT_SNR_THRESHOLD > hi.head["noise"]["snr18"]
    assert lo.head["selection"]["threshold"] == DEFAULT_SNR_THRESHOLD == 60.0
    assert lo.head["noise"]["model"] == "auto+iso_cap"
    assert lo.head["selection"]["requested"] == "auto"
    assert hi.head["mosaic"]["recon_sha256"]  # nlq: free recon
    assert "recon_sha256" not in lo.head["mosaic"]  # half3 without recon_hash
    for enc, frame in ((lo, frame_iso100), (hi, frame_iso4000)):
        _, rsq = _roundtrip(enc.chunks)
        rec = decode_mosaic(rsq)
        assert rec.shape == frame.mosaic.shape
        sat = frame.mosaic >= frame.white
        assert np.all(rec[sat] == frame.white)
    _, rsq = _roundtrip(hi.chunks)
    assert np.array_equal(decode_mosaic(rsq), hi.recon)


def test_half3_recon_hash(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(engine="half3", recon_hash=True, threads=4))
    _, rsq = _roundtrip(enc.chunks)
    rec = decode_mosaic(rsq)
    assert enc.head["mosaic"]["recon_sha256"] == hashlib.sha256(rec.tobytes()).hexdigest()


def test_snr_threshold_and_forced_engines(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(snr_threshold=1000, threads=4))
    assert enc.engine == "nlq"
    g = encode_frame_ex(frame_iso100, resolve(engine="gat4", threads=4))
    assert g.engine == "gat4" and {c.name for c in g.chunks} >= {"G4P0", "G4P3", "SATM"}
    _, rsq = _roundtrip(g.chunks)
    assert decode_mosaic(rsq).shape == frame_iso100.mosaic.shape


def test_forced_half3_without_noise(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(engine="half3", noise=False, threads=4))
    assert enc.engine == "half3" and "noise" not in enc.head and enc.noise is None


@pytest.mark.parametrize("preset", ["lossless", "vl"])
def test_odd_size_padding(synthetic_frame_factory: Callable[..., RawFrame], preset: str) -> None:
    fr = synthetic_frame_factory(h=255, w=257, iso=800, seed=3)
    enc = encode_frame_ex(fr, resolve(preset=preset, engine="nlq" if preset == "vl" else "auto", threads=2))
    m = enc.head["mosaic"]
    assert (m["height"], m["width"], m["orig_height"], m["orig_width"]) == (256, 258, 255, 257)
    assert any("padded" in w for w in enc.warnings)
    _, rsq = _roundtrip(enc.chunks)
    rec = decode_mosaic(rsq)
    assert rec.shape == (255, 257)
    if preset == "lossless":
        assert np.array_equal(rec, fr.mosaic)
    else:
        assert hashlib.sha256(rec.tobytes()).hexdigest() == m["recon_sha256"]


def test_lossless_integrity_error(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(preset="lossless", threads=2))
    head = dict(enc.head)
    head["mosaic"] = dict(head["mosaic"], sha256="0" * 64)
    payloads = {c.name: c.payload for c in enc.chunks[1:]}
    with pytest.raises(IntegrityError):
        decode_chunks(head, payloads)
    assert decode_chunks(head, payloads, check=False).shape == frame_iso100.mosaic.shape


def test_recon_mismatch_is_a_warning(frame_iso4000: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso4000, resolve(threads=2))
    head = dict(enc.head)
    head["mosaic"] = dict(head["mosaic"], recon_sha256="0" * 64)
    warn: list[str] = []
    decode_chunks(head, {c.name: c.payload for c in enc.chunks[1:]}, warnings=warn)
    assert any("recon_sha256" in w for w in warn)


def test_non_bayer_lossy_refused(frame_iso100: RawFrame) -> None:
    fr = dataclasses.replace(frame_iso100, pattern=np.array([[0, 1], [2, 3]]), color_desc="CMYG")
    with pytest.raises(EncodeError, match="lossless"):
        encode_frame_ex(fr, resolve(threads=2))
    enc = encode_frame_ex(fr, resolve(preset="lossless", threads=2))
    _, rsq = _roundtrip(enc.chunks)
    assert np.array_equal(decode_mosaic(rsq), fr.mosaic)


def test_encode_frame_spec_signature(frame_iso4000: RawFrame) -> None:
    chunks = encode_frame(frame_iso4000, resolve(preset="compact", threads=2))
    assert chunks[0].name == "HEAD" and {c.name for c in chunks} >= {"LUTS", "PLN0"}


def test_extra_chunks_and_meta_info(frame_iso100: RawFrame) -> None:
    from rawsqueeze.container import make_chunk

    enc = encode_frame_ex(frame_iso100, resolve(preset="lossless"), extra_chunks=[make_chunk("META", b"abc")],
                          meta_info={"strategy": "skeleton", "previews": []})
    assert enc.chunks[-1].name == "META" and enc.head["meta"]["strategy"] == "skeleton"
    _, rsq = _roundtrip(enc.chunks)
    assert rsq["META"] == b"abc"


def test_bench_plugins(frame_iso4000: RawFrame) -> None:
    chunks, head = bench_encode_fn(frame_iso4000, "nlq", {"f": 2.0, "effort": 1})
    assert head["engine"] == "nlq" and head["codec"]["nlq"]["f"] == 2.0 and head["codec"]["effort"] == 1
    _, rsq = _roundtrip(chunks)
    rec = bench_decode_fn(rsq.head, rsq.chunks)
    assert rec.shape == frame_iso4000.mosaic.shape


def test_head_chunk_rebuild_keeps_order(frame_iso100: RawFrame) -> None:
    enc = encode_frame_ex(frame_iso100, resolve(preset="lossless"))
    enc.head["fallback"] = {"from": "x"}
    enc.chunks[0] = make_head_chunk(enc.head)
    _, rsq = _roundtrip(enc.chunks)
    assert rsq.head["fallback"] == {"from": "x"}


def test_decoder_does_not_need_rawpy(frame_iso4000: RawFrame, monkeypatch: pytest.MonkeyPatch) -> None:
    enc = encode_frame_ex(frame_iso4000, resolve(threads=2))
    blob, _ = _roundtrip(enc.chunks)
    monkeypatch.setitem(sys.modules, "rawpy", None)
    assert decode_mosaic(blob).shape == frame_iso4000.mosaic.shape
