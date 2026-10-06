"""Tests for the lossless mode of the nlq engine (f == 0; DESIGN.md 2.2 step 10 / test plan item 5)."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import time
import zlib
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from rawsqueeze import jxl
from rawsqueeze.cfa import crop_to, pad_even
from rawsqueeze.container import make_head_chunk, read_rsq, write_rsq
from rawsqueeze.engines import EngineParams, get_engine
from rawsqueeze.engines.nlq import ENGINE
from rawsqueeze.rawio import RawFrame


def sha(a: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def lossless_roundtrip(frame: RawFrame, **kw: Any) -> tuple[np.ndarray, dict[str, Any], dict[str, bytes]]:
    """Pipeline-like round trip: pad to even, encode, container, decode, crop."""
    orig = frame.mosaic
    padded, (h, w) = pad_even(orig)
    fr = dataclasses.replace(frame, mosaic=padded)
    out = get_engine("lossless").encode(fr, EngineParams(quality=0.0, **kw))
    head: dict[str, Any] = frame.head_sections()
    head["mosaic"]["height"], head["mosaic"]["width"] = padded.shape
    head["codec"] = out.codec
    head.update(out.head_extra)
    buf = io.BytesIO()
    write_rsq(buf, [make_head_chunk(head)] + out.chunks)
    rf = read_rsq(buf.getvalue())
    rec = ENGINE.decode(rf.head, rf.chunks, threads=kw.get("threads"))
    assert rec.dtype == np.uint16 and rec.shape == padded.shape
    rec = crop_to(rec, (rf.head["mosaic"]["orig_height"], rf.head["mosaic"]["orig_width"]))
    assert sha(rec) == rf.head["mosaic"]["sha256"] == sha(orig)
    return rec, rf.head, rf.chunks


def test_fixtures_bit_exact(fixture_frames: dict[str, RawFrame]) -> None:
    for fr in fixture_frames.values():
        rec, head, chunks = lossless_roundtrip(fr, threads=4)
        assert np.array_equal(rec, fr.mosaic)
        assert head["mode"] == "lossless"
        assert head["codec"] == {
            "layout": "planes",
            "effort": 3,
            "nlq": {"f": 0.0, "recon": None, "planes": [{"dtype": "uint16"}] * 4},
        }
        assert "noise" not in head
        assert sorted(chunks) == ["PLN0", "PLN1", "PLN2", "PLN3"]
        assert all(jxl.decode(chunks[c]).dtype == np.uint16 for c in chunks)


def _frame_with(base: RawFrame, m: np.ndarray) -> RawFrame:
    return dataclasses.replace(base, mosaic=np.ascontiguousarray(m.astype(np.uint16)), visible_hw=m.shape)


CASES: dict[str, Callable[[np.random.Generator], np.ndarray]] = {
    "3x3": lambda r: r.integers(0, 4096, (3, 3)),
    "1x1": lambda r: r.integers(0, 4096, (1, 1)),
    "odd_5x7": lambda r: r.integers(0, 4096, (5, 7)),
    "odd_rows_401x300": lambda r: r.integers(100, 4096, (401, 300)),
    "odd_cols_300x401": lambda r: r.integers(100, 4096, (300, 401)),
    "all_saturated": lambda r: np.full((64, 64), 4095),
    "all_white": lambda r: np.full((64, 64), 4079),
    "all_black": lambda r: np.full((64, 64), 128),
    "all_zero": lambda r: np.zeros((64, 64)),
    "above_white": lambda r: r.integers(4079, 4096, (128, 128)),
    "full_16bit": lambda r: r.integers(0, 65536, (128, 130)),
    "below_black": lambda r: r.integers(0, 140, (64, 64)),
}


@pytest.mark.parametrize("case", list(CASES))
def test_edge_cases_bit_exact(synthetic_frame: RawFrame, case: str) -> None:
    rng = np.random.default_rng(zlib.crc32(case.encode()))
    m = CASES[case](rng)
    fr = _frame_with(synthetic_frame, m)
    rec, _, _ = lossless_roundtrip(fr, threads=2)
    assert np.array_equal(rec, fr.mosaic)


def test_large_6000x4002(synthetic_frame: RawFrame) -> None:
    rng = np.random.default_rng(0)
    base = rng.integers(128, 1200, (4002 // 2, 6000 // 2), dtype=np.uint16)
    m = np.repeat(np.repeat(base, 2, 0), 2, 1) + rng.integers(0, 8, (4002, 6000), dtype=np.uint16)
    fr = _frame_with(synthetic_frame, m)
    rec, head, _ = lossless_roundtrip(fr, threads=8)
    assert rec.shape == (4002, 6000)


@pytest.mark.parametrize("effort,layout", [(1, None), (3, "stack4"), (5, None)])
def test_layouts_and_efforts(frame_iso100: RawFrame, effort: int, layout: str | None) -> None:
    rec, head, chunks = lossless_roundtrip(frame_iso100, effort=effort, layout=layout, threads=2)
    expect_layout = layout or ("stack4" if effort >= 5 else "planes")
    assert head["codec"]["layout"] == expect_layout and head["codec"]["effort"] == effort
    assert ("PLNS" in chunks) == (expect_layout == "stack4")
    assert np.array_equal(rec, frame_iso100.mosaic)


def test_threads_do_not_change_stream(frame_iso4000: RawFrame) -> None:
    a = ENGINE.encode(frame_iso4000, EngineParams(quality=0.0, threads=1))
    b = ENGINE.encode(frame_iso4000, EngineParams(quality=0.0, threads=8))
    assert [c.payload for c in a.chunks] == [c.payload for c in b.chunks]
    assert a.recon is frame_iso4000.mosaic or np.array_equal(a.recon, frame_iso4000.mosaic)


# ---------------------------------------------------------------------------------------
# slow: all 5 samples

LL_EXPECT = {  # bytes of the 4 JXL planes at e3 (DESIGN.md 1.3, measured)
    "P1060444.RW2": 17.81e6,
    "P1037920.RW2": 20.17e6,
    "P1060384.RW2": 15.30e6,
    "PANA9831.RW2": 22.36e6,
    "PANA0003.RW2": 19.20e6,
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(LL_EXPECT))
def test_samples_bit_exact(sample_path: Callable[[str], object], name: str) -> None:
    from rawsqueeze.rawio import load_raw

    fr = load_raw(sample_path(name), want_exif=False)
    t = time.perf_counter()
    out = ENGINE.encode(fr, EngineParams(quality=0.0, threads=8))
    t_enc = time.perf_counter() - t
    chunks = {c.name: c.payload for c in out.chunks}
    head = {"mosaic": {"height": fr.height, "width": fr.width}, "codec": out.codec}
    t = time.perf_counter()
    rec = ENGINE.decode(head, chunks, threads=8)
    t_dec = time.perf_counter() - t
    assert sha(rec) == fr.mosaic_sha256()
    size = sum(len(b) for b in chunks.values())
    assert size == pytest.approx(LL_EXPECT[name], rel=0.02)
    assert t_enc < 1.5 and t_dec < 1.0
