"""Tests for rawsqueeze.meta (skeleton, JPEG scan stripping, previews, EXIF transfer)."""

from __future__ import annotations

import io
import struct
import time
from pathlib import Path

import numpy as np
import pytest

from rawsqueeze import meta
from rawsqueeze.dng import dng_encode
from rawsqueeze.tools import ExifTool, have

SAMPLES = ["P1060444.RW2", "PANA9831.RW2", "P1060384.RW2", "P1037920.RW2", "PANA0003.RW2"]
needs_exiftool = pytest.mark.skipif(not have("exiftool"), reason="exiftool not installed")


# ---------------------------------------------------------------------------------------
# synthetic JPEG streams


def _seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _entropy(rng: np.random.Generator, n: int) -> bytes:
    """Random entropy-coded bytes with JPEG byte stuffing (every 0xFF followed by 0x00)."""
    raw = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
    return raw.replace(b"\xff", b"\xff\x00")


def crafted_jpeg(seed: int = 0, *, with_trailer: bool = True) -> bytes:
    """Hand-made stream: APP1, DQT, SOF2, DHT, two SOS (one with RSTn), fill bytes, TEM, EOI,
    then an MPF-style second JPEG and trailing bytes."""
    rng = np.random.default_rng(seed)
    out = b"\xff\xd8"
    out += _seg(0xE1, b"Exif\x00\x00" + b"MM\x00*" + bytes(range(40)))
    out += b"\xff\x01"  # TEM (parameterless)
    out += _seg(0xDB, bytes(65))
    out += _seg(0xC2, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00")
    out += b"\xff\xff\xff" + _seg(0xC4, bytes(20))[1:]  # fill bytes before DHT
    out += _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    scan = b""
    for k in range(5):
        scan += _entropy(rng, 300) + bytes([0xFF, 0xD0 + k])
    scan += _entropy(rng, 200)
    out += scan
    out += _seg(0xC4, bytes(10))
    out += _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    out += _entropy(rng, 500)
    out += b"\xff\xff\xd9"  # fill + EOI
    if with_trailer:
        second = b"\xff\xd8" + _seg(0xE2, b"MPF\x00" + bytes(8)) + _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00") + _entropy(rng, 400) + b"\xff\xd9"
        out += b"\x00\x00" + second + b"TRAILER"
    return out


def _non_scan_equal(a: bytes, b: bytes) -> bool:
    return meta.jpeg_markers(a) == meta.jpeg_markers(b)


def test_strip_crafted_keeps_all_markers() -> None:
    src = crafted_jpeg()
    out, info = meta.jpeg_strip_scan(src)
    assert len(out) == len(src)
    assert _non_scan_equal(src, bytes(out))
    mk_src = [m for _, m, _ in meta.jpeg_markers(src)]
    assert mk_src.count(0xDA) == 3 and 0xE1 in mk_src and 0xE2 in mk_src and 0x01 in mk_src
    assert info.n_scans == 3
    assert info.n_rst == 5
    assert info.n_images == 2
    assert info.trailing == len(b"TRAILER")
    assert bytes(out).endswith(b"\xff\xd9TRAILER")
    # all RST markers survive
    for k in range(5):
        assert bytes([0xFF, 0xD0 + k]) in bytes(out)
    # most bytes are now zero: entropy data was zeroed
    assert info.zeroed > 1500
    assert bytes(out).count(0) > info.zeroed


def test_strip_header_offset_and_idempotent() -> None:
    src = crafted_jpeg(with_trailer=False)
    out, info = meta.jpeg_strip_scan(src)
    first_sos = src.index(b"\xff\xda")
    sos_len = struct.unpack(">H", src[first_sos + 2 : first_sos + 4])[0]
    assert info.header_bytes == first_sos + 2 + sos_len
    assert bytes(out[: info.header_bytes]) == src[: info.header_bytes]
    out2, info2 = meta.jpeg_strip_scan(out)
    assert out2 == out
    assert info2.zeroed == info.zeroed


def test_strip_baseline_imagecodecs() -> None:
    import imagecodecs

    a = (np.random.default_rng(1).random((96, 120, 3)) * 255).astype(np.uint8)
    src = bytes(imagecodecs.jpeg8_encode(a, level=90))
    out, info = meta.jpeg_strip_scan(src)
    assert info.n_scans == 1
    assert _non_scan_equal(src, bytes(out))
    assert bytes(out[-2:]) == b"\xff\xd9"
    assert bytes(out[info.header_bytes : -2]) == bytes(len(src) - 2 - info.header_bytes)


def test_strip_progressive_restart_pillow() -> None:
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    del PIL
    a = (np.random.default_rng(2).random((80, 96, 3)) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, "JPEG", progressive=True, restart_marker_blocks=4, quality=90)
    src = buf.getvalue()
    out, info = meta.jpeg_strip_scan(src)
    assert info.n_scans == src.count(b"\xff\xda") > 1
    assert info.n_rst > 0
    assert _non_scan_equal(src, bytes(out))
    # header still parses
    im = Image.open(io.BytesIO(bytes(out)))
    assert im.size == (96, 80)


@pytest.mark.parametrize("bad", [b"", b"\x00\x01\x02\x03", b"\xff\xd8\xff\xe1\x00", b"\xff\xd8" + b"\xff\xdb\x00\x04ab" + b"\xff\xd9"])
def test_strip_rejects_invalid(bad: bytes) -> None:
    with pytest.raises(ValueError):
        meta.jpeg_strip_scan(bad)


# ---------------------------------------------------------------------------------------
# skeleton on synthetic TIFF-like files


def test_skeleton_on_dng_stripoffsets(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    data = dng_encode(frame_iso100.mosaic, frame_iso100.head_sections(), compression="none16")
    payload, info = meta.make_skeleton(None, data)
    assert info["strategy"] == "skeleton"
    assert info["format"] == "tiff"
    assert info["method"] == "StripOffsets"
    (off, ln), = info["raw_regions"]
    assert ln == frame_iso100.mosaic.size * 2
    assert info["truncated"] is True
    assert payload == data[:off]
    layout = meta.locate_raw_layout(data)
    assert layout.raw_regions == [(off, ln)]


def test_skeleton_on_tiled_lj92_dng(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    data = dng_encode(frame_iso100.mosaic, frame_iso100.head_sections(), compression="lj92")
    layout = meta.locate_raw_layout(data)
    assert layout.method == "TileOffsets"
    from rawsqueeze.dng import effective_tile

    tw, th = effective_tile(512, (256, 256))  # 128x256: LibRaw quirk at 2*tw == width
    assert len(layout.raw_regions) == (512 // tw) * (512 // th)
    payload, info = meta.make_skeleton(None, data)
    assert info["strategy"] == "skeleton"
    assert len(payload) <= len(data)


def test_skeleton_tiff_with_embedded_preview(tmp_path: Path) -> None:
    tifffile = pytest.importorskip("tifffile")
    import imagecodecs

    jpg = bytes(imagecodecs.jpeg8_encode((np.random.default_rng(3).random((64, 64, 3)) * 255).astype(np.uint8), level=85))
    img = (np.random.default_rng(4).random((64, 128)) * 4000).astype(np.uint16)
    p = tmp_path / "x.tif"
    # raw data as the main image (CFA photometric), JPEG blob in an UNDEFINED private tag
    tifffile.imwrite(
        p, img, photometric="minisblack",
        extratags=[(50000, 7, len(jpg), jpg, True), (262, 3, 1, 32803, True)],
    )
    data = p.read_bytes()
    layout = meta.locate_raw_layout(data)
    assert layout.fmt == "tiff"
    assert any(pr.length == len(jpg) for pr in layout.previews)
    payload, info = meta.make_skeleton(None, data)
    assert info["strategy"] == "skeleton"
    pv = [x for x in info["previews"] if x["length"] == len(jpg)][0]
    assert pv["stripped"] and pv["n_scans"] == 1
    stripped = payload[pv["offset"] : pv["offset"] + pv["length"]]
    assert _non_scan_equal(jpg, stripped)
    # raw strips zeroed or truncated
    for o, ln in info["raw_regions"]:
        assert payload[o : o + ln].count(0) == len(payload[o : o + ln])


def test_exif_only_pack_roundtrip() -> None:
    js, ex = b'[{"Make":"X"}]', b"MM\x00*" + bytes(30)
    blob = meta.pack_exif_only(js, ex)
    mb = meta.parse_meta(blob)
    assert mb.strategy == "exif-only" and mb.exif_json == js and mb.exif_blob == ex
    assert meta.parse_meta(b"").strategy == "none"
    assert meta.parse_meta(b"II*\x00rest").strategy == "skeleton"
    with pytest.raises(ValueError):
        meta.parse_meta(blob[:-3])


def test_non_tiff_falls_back(tmp_path: Path) -> None:
    p = tmp_path / "x.raw"
    p.write_bytes(b"NOTATIFF" + bytes(2000))
    payload, info = meta.make_skeleton(p, allow_exif_only=False)
    assert info["strategy"] == "none" and payload == b""
    assert info["warnings"]
    if have("exiftool"):
        payload, info = meta.make_skeleton(p)
        assert info["strategy"] == "exif-only"
        mb = meta.parse_meta(payload)
        assert mb.strategy == "exif-only" and mb.exif_json.strip().startswith(b"[")


def test_compressed_size_small() -> None:
    assert meta.compressed_size(bytes(1_000_000)) < 1000


# ---------------------------------------------------------------------------------------
# previews (cjxl/djxl)


@pytest.mark.skipif(not (have("cjxl") and have("djxl")), reason="cjxl/djxl not installed")
def test_preview_transcode_roundtrip() -> None:
    import imagecodecs

    a = (np.random.default_rng(5).random((128, 160, 3)) * 255).astype(np.uint8)
    jpg = bytes(imagecodecs.jpeg8_encode(a, level=92))
    jxl = meta.transcode_preview(jpg, verify=True)
    assert jxl[:2] in (b"\xff\x0a", b"\x00\x00")
    assert meta.restore_preview(jxl) == jpg


# ---------------------------------------------------------------------------------------
# real samples (slow)


@pytest.mark.slow
@needs_exiftool
@pytest.mark.parametrize("name", SAMPLES)
def test_skeleton_real_sample_tags(name: str, sample_path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    src = sample_path(name)
    t0 = time.perf_counter()
    payload, info = meta.make_skeleton(src)
    dt = time.perf_counter() - t0
    assert info["strategy"] == "skeleton" and info["format"] == "rw2"
    assert info["method"] == "RawDataOffset"
    assert {p["tag"] for p in info["previews"]} == {"JpgFromRaw", "JpgFromRaw2"}
    assert all(p["stripped"] for p in info["previews"])
    zs = meta.compressed_size(payload)
    assert 18_000 < zs < 36_000, zs
    assert dt < 2.0
    skel = meta.write_skeleton_file(payload, tmp_path / "skel.rw2")
    with ExifTool() as et:
        a = meta.count_tags(src, et)
        b = meta.count_tags(skel, et)
        rdo = et.run_json(["-n", "-RawDataOffset", str(src)])[0]["RawDataOffset"]
    assert info["skeleton_len"] == rdo
    for g in ("Panasonic", "GPS", "ExifIFD", "PanasonicRaw", "IFD0", "IFD1"):
        assert a.get(g) == b.get(g), g
    assert a["all"] == b["all"]
    if name in ("P1060444.RW2",):
        assert a["all"] == 329


@pytest.mark.slow
@pytest.mark.skipif(not (have("cjxl") and have("djxl")), reason="cjxl/djxl not installed")
def test_preview_payloads_real(sample_path) -> None:  # type: ignore[no-untyped-def]
    src = sample_path("P1060384.RW2")
    data = src.read_bytes()
    prv = meta.extract_previews(src, data)
    assert set(prv) == {"JpgFromRaw", "JpgFromRaw2"}
    payloads, info = meta.preview_payloads(src, "small", data)
    assert list(payloads) == ["PRV0"]
    assert info[0]["jxl_size"] < info[0]["jpeg_size"]
    assert meta.restore_preview(payloads["PRV0"]) == prv["JpgFromRaw"]
