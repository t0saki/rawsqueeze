"""Tests for rawsqueeze.dng (tags, LJ92 tiles, uncompressed variants, EXIF transfer)."""

from __future__ import annotations

import copy
import io

import struct
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from rawsqueeze import dng, meta
from rawsqueeze.tools import ExifTool, have

rawpy = pytest.importorskip("rawpy")
needs_exiftool = pytest.mark.skipif(not have("exiftool"), reason="exiftool not installed")


def _tags(buf: bytes) -> dict[int, Any]:
    """{tag: values} of IFD0 using the meta TIFF parser."""
    info = meta.parse_tiff(buf)
    ifd = info.ifds[0]
    out: dict[int, Any] = {}
    for tag, e in ifd.entries.items():
        if e.type in (1, 3, 4, 7):
            out[tag] = info.values(buf, e)
        elif e.type in (5, 10):
            fmt = "I" if e.type == 5 else "i"
            v = struct.unpack_from(f"<{2 * e.count}{fmt}", buf, e.data_offset)
            out[tag] = [(v[2 * i], v[2 * i + 1]) for i in range(e.count)]
        elif e.type == 12:
            out[tag] = list(struct.unpack_from(f"<{e.count}d", buf, e.data_offset))
        elif e.type == 2:
            out[tag] = buf[e.data_offset : e.data_offset + e.count].rstrip(b"\x00").decode()
    return out


def _read(buf: bytes) -> dict[str, Any]:
    with rawpy.imread(io.BytesIO(buf)) as r:
        s = r.sizes
        return {
            "raw": r.raw_image.copy(),
            "black": list(r.black_level_per_channel),
            "white": int(r.white_level),
            "pattern": r.raw_pattern.tolist(),
            "desc": r.color_desc.decode(),
            "wb": list(r.camera_whitebalance),
            "xyz": np.array(r.rgb_xyz_matrix)[:3],
            "crop": (s.crop_left_margin, s.crop_top_margin, s.crop_width, s.crop_height),
            "margins": (s.top_margin, s.left_margin, s.height, s.width),
            "flip": s.flip,
        }


@pytest.mark.parametrize("comp", ["lj92", "none12", "none16"])
def test_roundtrip_fixture(comp: str, fixture_frames) -> None:  # type: ignore[no-untyped-def]
    for fr in fixture_frames.values():
        head = fr.head_sections()
        buf = dng.dng_encode(fr.mosaic, head, compression=comp)
        r = _read(buf)
        assert np.array_equal(r["raw"], fr.mosaic)
        assert r["black"] == fr.black_per_channel
        assert r["white"] == fr.white
        assert r["pattern"] == fr.pattern.tolist()
        assert r["desc"] == fr.color_desc
        wb = np.array(fr.camera_wb[:3]) / fr.camera_wb[1]
        assert np.allclose(r["wb"][:3], wb, rtol=1e-6)  # LibRaw stores float32
        assert np.allclose(r["xyz"], fr.xyz_to_cam, atol=5e-5)
        t = _tags(buf)
        cl, ct, cw, ch = fr.crop_ltwh
        assert t[50719] == [cl - fr.margins[1], ct - fr.margins[0]]  # DefaultCropOrigin
        assert t[50720] == [cw, ch]
        assert t[50829] == [0, 0, fr.height, fr.width]  # ActiveArea
        assert t[33422] == fr.dng_cfa()
        assert t[50714] == fr.black_per_position
        bps = {"lj92": 12, "none12": 12, "none16": 16}[comp]
        assert t[258] == [bps]
        assert t[259] == [7 if comp == "lj92" else 1]
        assert t[305].startswith("rawsqueeze ")
        assert t[50706] == [1, 4, 0, 0]
        # AsShotNeutral = exact ratios of the camera integers
        asn = t[50728]
        assert asn[0][0] * fr.camera_wb[0] == asn[0][1] * fr.camera_wb[1]
        assert asn[2][0] * fr.camera_wb[2] == asn[2][1] * fr.camera_wb[1]


def test_lj92_smaller_than_uncompressed(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso100.head_sections()
    sizes = {c: len(dng.dng_encode(frame_iso100.mosaic, head, compression=c)) for c in dng.COMPRESSIONS}
    assert sizes["lj92"] < sizes["none12"] < sizes["none16"]


@pytest.mark.parametrize("shape", [(300, 437), (258, 256), (64, 64), (256, 128), (64, 100), (300, 250), (40, 34)])
def test_lj92_edge_tiles(shape: tuple[int, int], synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    fr = synthetic_frame_factory(h=shape[0], w=shape[1], saturate_frac=0.01)
    head = fr.head_sections()
    for threads in (1, 4):
        buf = dng.dng_encode(fr.mosaic, head, compression="lj92", threads=threads)
        r = _read(buf)
        assert r["raw"].shape == shape
        assert np.array_equal(r["raw"], fr.mosaic)
    # tile grid
    t = _tags(buf)
    tw, th = dng.effective_tile(shape[1], (256, 256))
    nt = -(-shape[0] // th) * -(-shape[1] // tw)
    assert len(t[324]) == nt and len(t[325]) == nt
    assert t[322] == [tw] and t[323] == [th]
    assert tw % 16 == 0 and (tw <= shape[1])


def test_lj92_tile_size_option(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso4000.head_sections()
    buf = dng.dng_encode(frame_iso4000.mosaic, head, compression="lj92", tile=128)
    assert len(_tags(buf)[324]) == 16
    assert np.array_equal(_read(buf)["raw"], frame_iso4000.mosaic)
    with pytest.raises(ValueError):
        dng.dng_encode(frame_iso4000.mosaic, head, tile=100)


def test_16bit_values(synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    fr = synthetic_frame_factory(h=64, w=300)
    m = fr.mosaic.copy()
    m[0, :4] = [65535, 40000, 5000, 4096]
    head = fr.head_sections()
    buf = dng.dng_encode(m, head, compression="lj92")
    assert _tags(buf)[258] == [16]
    assert np.array_equal(_read(buf)["raw"], m)
    with pytest.raises(ValueError):
        dng.dng_encode(m, head, compression="none12")


def test_orientation_noise_profile_software(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = copy.deepcopy(frame_iso100.head_sections())
    head["geometry"]["flip"] = 6
    head["engine"] = "nlq"
    head["mode"] = "lossy"
    head["codec"] = {"nlq": {"f": 1.0}}
    head["noise"] = {"planes": [{"g_used": 0.06, "s2": 4.0}, {"g_used": 0.05, "s2": 3.0}, {"g_used": 0.07, "s2": 5.0}, {"g_used": 0.04, "s2": 2.0}]}
    buf = dng.dng_encode(frame_iso100.mosaic, head)
    t = _tags(buf)
    assert t[274] == [6]
    assert t[305] == f"rawsqueeze {dng.__version__} (nlq f1.0)"
    npf = t[51041]
    X = frame_iso100.white - 128
    fac = 1 + 1 / 12
    assert npf[0] == pytest.approx(fac * 0.06 / X)  # R at (0,0)
    assert npf[1] == pytest.approx(fac * 4.0 / X**2)
    assert npf[2] == pytest.approx(fac * 0.06 / X)  # G mean of 0.05, 0.07
    assert npf[4] == pytest.approx(fac * 0.04 / X)
    assert 700 in t  # XMP packet with rawsqueeze:Engine
    xmp = buf[meta.parse_tiff(buf).ifds[0].entries[700].data_offset :][:2000]
    assert b'rawsqueeze:Engine="nlq"' in xmp
    with rawpy.imread(io.BytesIO(buf)) as r:
        assert r.sizes.flip == 6
    no_np = dng.dng_encode(frame_iso100.mosaic, head, noise_profile=False)
    assert 51041 not in _tags(no_np)
    for flip, ori in ((0, 1), (3, 3), (5, 8)):
        head["geometry"]["flip"] = flip
        assert _tags(dng.dng_encode(frame_iso100.mosaic, head))[274] == [ori]


def test_active_area_and_crop(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    head = copy.deepcopy(frame_iso100.head_sections())
    head["geometry"].update(margins=[4, 6], visible_hw=[500, 500], crop_ltwh=[10, 8, 480, 490])
    buf = dng.dng_bytes16(frame_iso100.mosaic, head)
    t = _tags(buf)
    assert t[50829] == [4, 6, 504, 506]
    assert t[50719] == [4, 4]
    assert t[50720] == [480, 490]
    r = _read(buf)
    assert r["crop"] == (10, 8, 480, 490)  # LibRaw reports full-sensor coordinates
    assert r["margins"] == (4, 6, 500, 500)
    # invalid crop falls back to the active area
    head["geometry"]["crop_ltwh"] = [0, 0, 9999, 9999]
    t = _tags(dng.dng_bytes16(frame_iso100.mosaic, head))
    assert t[50719] == [0, 0] and t[50720] == [500, 500]


def test_build_tags_from_rawframe(frame_iso100) -> None:  # type: ignore[no-untyped-def]
    tags = dng.build_tags(frame_iso100, 12, tile=(256, 256))
    from pidng.dng import Tag

    assert tags.get(Tag.ImageWidth).rawValue == [512]
    assert tags.get(Tag.BitsPerSample).rawValue == [12]
    assert tags.get(Tag.Software) is None  # added by LJ92DNG only (no duplicates)


def test_dng_bytes16_fast_and_deterministic(frame_iso4000) -> None:  # type: ignore[no-untyped-def]
    head = frame_iso4000.head_sections()
    a = dng.dng_bytes16(frame_iso4000.mosaic, head)
    b = dng.dng_bytes16(frame_iso4000.mosaic, head)
    assert a == b
    assert _tags(a)[258] == [16]
    kw: dict[str, Any] = dict(use_camera_wb=True, no_auto_bright=True, output_bps=16, user_flip=0,
                              demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD)
    with rawpy.imread(io.BytesIO(a)) as r1, rawpy.imread(io.BytesIO(b)) as r2:
        assert np.array_equal(r1.postprocess(**kw), r2.postprocess(**kw))


def test_write_dng_atomic(tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "sub" / "out.DNG"
    rep = dng.write_dng(frame_iso100.mosaic, frame_iso100.head_sections(), out)
    assert out.exists() and rep.size == out.stat().st_size
    assert sorted(p.name for p in out.parent.iterdir()) == ["out.DNG"]  # no tmp left, no '.dng' appended
    with rawpy.imread(str(out)) as r:
        assert np.array_equal(r.raw_image, frame_iso100.mosaic)
    with pytest.raises(ValueError):
        dng.write_dng(frame_iso100.mosaic, frame_iso100.head_sections(), tmp_path / "x.dng", compression="jxl")
    assert not (tmp_path / "x.dng").exists()


@needs_exiftool
def test_exiftool_validate_no_errors(tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    for comp in dng.COMPRESSIONS:
        out = tmp_path / f"v_{comp}.dng"
        dng.write_dng(frame_iso100.mosaic, frame_iso100.head_sections(), out, compression=comp)
        with ExifTool() as et:
            txt = et.run(["-validate", "-error", "-warning", "-a", str(out)])
        assert "Error" not in txt, txt


# ---------------------------------------------------------------------------------------
# slow: real samples


@pytest.mark.slow
@needs_exiftool
@pytest.mark.parametrize("name", ["P1060444.RW2", "PANA0003.RW2"])
def test_real_sample_lj92_with_exif(name: str, sample_path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from rawsqueeze.rawio import load_raw

    src = sample_path(name)
    with ExifTool() as et:
        fr = load_raw(src, exiftool=et)
        head = fr.head_sections()
        payload, _ = meta.make_skeleton(src)
        out = tmp_path / (Path(name).stem + ".dng")
        t0 = time.perf_counter()
        rep = dng.write_dng(fr.mosaic, head, out, meta=payload, et=et)
        dt = time.perf_counter() - t0
        assert 20.5e6 < rep.size < 23.0e6, rep.size
        assert dt < 5.0
        assert rep.exif and rep.exif["used_jpg_from_raw"]
        v = et.run(["-validate", "-error", "-warning", "-a", str(out)])
        assert "Error" not in v, v
        a, d = meta.count_tags(src, et), meta.count_tags(out, et)
        for g in ("Panasonic", "GPS"):
            assert a[g] == d[g], (g, a[g], d[g])
        assert d["ExifIFD"] >= 40
        info = et.run_json(["-n", "-Make", "-Model", "-ISO", "-LensModel", "-Orientation", str(out)])[0]
        assert info["Make"] == fr.make and info["Model"] == fr.model and info["ISO"] == fr.iso
    with rawpy.imread(str(out)) as r:
        assert np.array_equal(r.raw_image, fr.mosaic)
        assert list(r.black_level_per_channel) == fr.black_per_channel
        assert r.white_level == fr.white
        s = r.sizes
        assert (s.crop_left_margin, s.crop_top_margin, s.crop_width, s.crop_height) == fr.crop_ltwh
        img = r.postprocess(use_camera_wb=True, user_flip=0, half_size=True)
        assert img.shape == (fr.height // 2, fr.width // 2, 3)
    assert not any(p.name.endswith(".tmp.dng") for p in tmp_path.iterdir())
