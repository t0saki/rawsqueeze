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


def _ljpeg_headers(tile: bytes) -> dict[str, Any]:
    """SOF3 / SOS fields of one LJ92 tile."""
    i = tile.index(b"\xff\xc3")
    P, Y, X, Nf = tile[i + 4], int.from_bytes(tile[i + 5 : i + 7], "big"), int.from_bytes(tile[i + 7 : i + 9], "big"), tile[i + 9]
    j = tile.index(b"\xff\xda")
    Ns = tile[j + 4]
    return {"P": P, "Y": Y, "X": X, "Nf": Nf, "Ns": Ns, "Ss": tile[j + 5 + 2 * Ns], "Al": tile[j + 7 + 2 * Ns] & 15}


@pytest.mark.parametrize("width", [512, 6016 // 8, 300])
def test_lj92_rawspeed_compatible_layout(width: int, synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    """rawspeed (darktable) needs predictor 1 and a JPEG frame whose width x components tiles the DNG tile."""
    import imagecodecs

    fr = synthetic_frame_factory(h=272, w=width, g=2.0, s2=30.0)
    buf = dng.dng_encode(fr.mosaic, fr.head_sections(), compression="lj92", threads=2)
    t = _tags(buf)
    tw, th = t[322][0], t[323][0]
    assert 2 * tw != width  # LibRaw reads 2 tile rows per JPEG row in that case
    info = meta.parse_tiff(buf)
    offs, cnts = info.values(buf, info.ifds[0].entries[324]), info.values(buf, info.ifds[0].entries[325])
    tiles_x = -(-width // tw)
    for k, (o, n) in enumerate(zip(offs, cnts)):
        tile = buf[o : o + n]
        h = _ljpeg_headers(tile)
        assert h["Ss"] == 1 and h["Al"] == 0, h  # predictor 1, no point transform
        assert h["Nf"] == h["Ns"] == 2 and h["X"] * h["Nf"] == tw and h["Y"] == th, h
        assert tw % (h["X"] * h["Nf"]) == 0
        dec = np.asarray(imagecodecs.ljpeg_decode(tile)).reshape(th, tw)  # independent decoder
        y, x = (k // tiles_x) * th, (k % tiles_x) * tw
        ref = np.zeros((th, tw), np.uint16)
        part = fr.mosaic[y : y + th, x : x + tw]
        ref[: part.shape[0], : part.shape[1]] = part
        assert np.array_equal(dec, ref), k
    assert np.array_equal(_read(buf)["raw"], fr.mosaic)


def test_lj92_extreme_differences() -> None:
    """16-bit data with +-65535 jumps (SSSS 16, modulo-2^16 differences) round-trips."""
    import imagecodecs

    t = np.zeros((32, 32), np.uint16)
    t[:, 2::4] = 65535
    t[5, 7] = 1
    t[9, :] = np.arange(32) * 2047
    tile = dng._lj92_tile(t, 16)
    assert np.array_equal(np.asarray(imagecodecs.ljpeg_decode(tile)).reshape(32, 32), t)
    flat = dng._lj92_tile(np.full((16, 16), 7, np.uint16), 12)  # single-symbol histogram
    assert np.array_equal(np.asarray(imagecodecs.ljpeg_decode(flat)).reshape(16, 16), np.full((16, 16), 7))


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


@needs_exiftool
@pytest.mark.parametrize("bad", [b"\0" * 4096, b"II*\x00" + b"\x07" * 3000], ids=["zeros", "junk-tiff"])
def test_failed_exif_transfer_is_reported(tmp_path: Path, frame_iso100, bad: bytes) -> None:  # type: ignore[no-untyped-def]
    """An unusable skeleton must produce a warning, not a silent 'success' with no EXIF."""
    out = tmp_path / "noexif.dng"
    rep = dng.write_dng(frame_iso100.mosaic, frame_iso100.head_sections(), out, meta=bad)
    assert any("copied no EXIF tags" in w for w in rep.warnings), rep.warnings
    assert rep.exif and any("copied no EXIF tags" in w for w in rep.exif["warnings"])
    assert out.exists()


def test_xmp_extra_escaped() -> None:
    x = dng.xmp_packet("sw", "half3", "d0.2", {"Note": 'a "b" <c> & d'}).decode()
    assert 'rawsqueeze:Note="a &quot;b&quot; &lt;c&gt; &amp; d"' in x


# ---------------------------------------------------------------------------------------
# CFA / BlackLevel phase for odd ActiveArea origins


@pytest.mark.parametrize("margins", [(0, 0), (1, 0), (0, 1), (1, 1), (2, 2)])
@pytest.mark.parametrize("comp", ["lj92", "none16"])
def test_cfa_phase_odd_margins(margins: tuple[int, int], comp: str, synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    """CFAPattern/BlackLevel are relative to the ActiveArea origin: LibRaw must find R where it is."""
    top, left = margins
    fr = synthetic_frame_factory(h=64, w=64)  # pattern RGGB at mosaic (0,0): [[0,1],[3,2]]
    head = copy.deepcopy(fr.head_sections())
    head["geometry"].update(margins=[top, left], visible_hw=[60 - top, 60 - left], crop_ltwh=[left, top, 60 - left, 60 - top])
    blk = [100, 110, 120, 130]
    head["levels"]["black_per_position"] = blk
    buf = dng.dng_encode(fr.mosaic, head, compression=comp)
    t = _tags(buf)
    assert t[50829] == [top, left, 60, 60]
    exp_cfa = [fr.dng_cfa()[2 * ((i + top) % 2) + (j + left) % 2] for i in (0, 1) for j in (0, 1)]
    assert t[33422] == exp_cfa
    assert t[50714] == [blk[2 * ((i + top) % 2) + (j + left) % 2] for i in (0, 1) for j in (0, 1)]
    with rawpy.imread(io.BytesIO(buf)) as r:
        colors = r.raw_colors.copy()
        desc = r.color_desc.decode()
        assert np.array_equal(r.raw_image, fr.mosaic)
    yy, xx = np.mgrid[0:64, 0:64]
    expected = np.asarray(fr.pattern)[yy % 2, xx % 2]  # colour index relative to mosaic (0,0)
    vis = (yy >= top) & (yy < 60) & (xx >= left) & (xx < 60)
    assert np.array_equal(colors[vis], expected[vis])
    assert desc[colors[0 + 2, 0 + 2]] == "R"  # the R pixel of the 2x2 cell at (2,2)


def test_cfa_phase_odd_margins_uniform_black(synthetic_frame_factory) -> None:  # type: ignore[no-untyped-def]
    """With a uniform black level LibRaw reads it unchanged for any origin."""
    fr = synthetic_frame_factory(h=64, w=64)
    for top, left in ((1, 0), (0, 1), (1, 1)):
        head = copy.deepcopy(fr.head_sections())
        head["geometry"].update(margins=[top, left], visible_hw=[60 - top, 60 - left], crop_ltwh=[left, top, 60 - left, 60 - top])
        with rawpy.imread(io.BytesIO(dng.dng_bytes16(fr.mosaic, head))) as r:
            assert list(r.black_level_per_channel) == fr.black_per_channel
            assert r.raw_pattern.tolist() == fr.pattern.tolist()


def test_rotate_cfa_phase() -> None:
    v = [0, 1, 2, 3]
    assert dng.rotate_cfa_phase(v, 0, 0) == v
    assert dng.rotate_cfa_phase(v, 1, 0) == [2, 3, 0, 1]
    assert dng.rotate_cfa_phase(v, 0, 1) == [1, 0, 3, 2]
    assert dng.rotate_cfa_phase(v, 1, 1) == [3, 2, 1, 0]
    assert dng.rotate_cfa_phase(v, 2, 4) == v


# ---------------------------------------------------------------------------------------
# lens distortion: opcode lists, WarpRectilinear, Panasonic DistortionInfo

# DistortionInfo (0x0119) of two DC-S9 samples: P1060444 (60 mm, pincushion) and
# ISO2000_PANA9996 (24 mm, barrel).
PANA_60MM = bytes.fromhex("cc12496b410069000400c2fb050001e103fc11005a010c00160e05025a48c311")
PANA_24MM = bytes.fromhex("eb14619004016f02dcfae3ffd70001f1860f80005a0192fe160e7402cdfb6eca")


def test_opcode_list_binary_layout_roundtrip() -> None:
    op = dng.WarpRectilinear(planes=[(0.99, 0.01, -0.002, 0.0003, 1e-5, -2e-5)], cx=0.4999, cy=0.5001)
    b = dng.opcode_list_bytes([op])
    assert len(b) == 4 + 16 + 4 + 48 + 16 == 88
    n, oid, ver, flags, size, planes = struct.unpack_from(">6I", b, 0)
    assert (n, oid, ver, flags, size, planes) == (1, 1, 0x01030000, dng.OPCODE_FLAG_OPTIONAL, 68, 1)
    assert struct.unpack_from(">6d", b, 24) == op.planes[0]
    assert struct.unpack_from(">2d", b, 72) == (0.4999, 0.5001)
    (back,) = dng.parse_opcode_list(b)
    assert back == op
    # 3 planes + an unknown opcode kept verbatim
    op3 = dng.WarpRectilinear(planes=[(1.0, 0.1, 0.0, 0.0, 0.0, 0.0), (1.0, 0.2, 0.0, 0.0, 0.0, 0.0),
                                      (1.0, 0.3, 0.0, 0.0, 0.0, 0.0)], flags=0)
    raw = dng.RawOpcode(opcode_id=9, version=0x01030000, flags=1, params=b"\x00\x01\x02\x03")
    b3 = dng.opcode_list_bytes([op3, raw])
    assert len(b3) == 4 + (16 + 4 + 3 * 48 + 16) + (16 + 4)
    assert dng.parse_opcode_list(b3) == [op3, raw]
    for bad in (b[:3], b[:-1], b + b"\x00", b"\x00\x00\x00\x01" + b[4:20] + b[24:]):
        with pytest.raises(ValueError):
            dng.parse_opcode_list(bad)


def test_warp_rectilinear_src_matches_dng_sdk_formula() -> None:
    W, H = 600, 400
    op = dng.WarpRectilinear(planes=[(0.97, 0.03, -0.01, 0.002, 0.001, -0.002)], cx=0.45, cy=0.55)
    cx, cy, nr = dng.warp_rectilinear_norm(op, W, H)
    assert (cx, cy) == (pytest.approx(270.0), pytest.approx(220.0))
    assert nr == pytest.approx(np.hypot(600 - 270, 0 - 220))  # farthest corner (r, t), r exclusive
    rng = np.random.default_rng(1)
    x, y = rng.uniform(0, W, 50), rng.uniform(0, H, 50)
    sx, sy = dng.warp_rectilinear_src(x, y, op, W, H)
    k0, k1, k2, k3, t0, t1 = op.planes[0]
    for i in range(50):
        dx, dy = (x[i] - cx) / nr, (y[i] - cy) / nr
        r2 = min(dx * dx + dy * dy, 1.0)
        f = k0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
        ex = cx + nr * (dx * f + t1 * (r2 + 2 * dx * dx) + 2 * t0 * dx * dy)
        ey = cy + nr * (dy * f + t0 * (r2 + 2 * dy * dy) + 2 * t1 * dx * dy)
        assert (sx[i], sy[i]) == (pytest.approx(ex), pytest.approx(ey))
    c = dng.warp_rectilinear_src(np.array([cx]), np.array([cy]), op, W, H)
    assert (c[0][0], c[1][0]) == (pytest.approx(cx), pytest.approx(cy))


def test_parse_panasonic_distortion() -> None:
    d = meta.parse_panasonic_distortion(PANA_60MM)
    assert d is not None and d.checksum_ok and d.enabled
    assert d.scale == pytest.approx(1.03427813900638)  # exiftool DistortionScale
    assert d.coeffs == (-0.031158447265625, 0.0001220703125, 0.0003662109375)  # Param08, 04, 11
    assert d.norm_radius == 3606
    assert meta.parse_panasonic_distortion(PANA_60MM[:31]) is None
    assert meta.parse_panasonic_distortion(None) is None
    bad = bytearray(PANA_60MM)
    bad[8] ^= 1
    assert meta.parse_panasonic_distortion(bytes(bad)).checksum_ok is False  # type: ignore[union-attr]
    off = bytearray(PANA_60MM)
    off[14] &= 0xF0  # DistortionCorrection = Off (word 7 low nibble)
    od = meta.parse_panasonic_distortion(bytes(off))
    assert od is not None and not od.enabled
    assert dng.panasonic_warp_rectilinear(od, active_hw=(4016, 6016), crop=(8, 8, 6000, 4000)) is None


@pytest.mark.parametrize("blob", [PANA_60MM, PANA_24MM], ids=["60mm", "24mm"])
def test_panasonic_to_warp_rectilinear_inverts_model(blob: bytes) -> None:
    d = meta.parse_panasonic_distortion(blob)
    assert d is not None
    W, H = 6016, 4016
    op, err = dng.panasonic_warp_rectilinear(d, active_hw=(H, W), crop=(8, 8, 6000, 4000))  # type: ignore[misc]
    assert err < 0.5
    ccx, ccy = 8 + 5999 / 2, 8 + 3999 / 2
    assert (op.cx * W, op.cy * H) == (pytest.approx(ccx), pytest.approx(ccy))
    # output pixel p samples source s: the Panasonic forward model must map |s| back to |p|
    rng = np.random.default_rng(0)
    x, y = rng.uniform(8, 6008, 2000), rng.uniform(8, 4008, 2000)
    sx, sy = dng.warp_rectilinear_src(x, y, op, W, H)
    n = d.norm_radius
    r_out = np.hypot(x - ccx, y - ccy)
    r_back = d.forward(np.hypot(sx - ccx, sy - ccy) / n) * n
    assert np.max(np.abs(r_back - r_out)) < 0.6
    # radial only: directions preserved
    assert np.allclose(np.arctan2(sy - ccy, sx - ccx), np.arctan2(y - ccy, x - ccx), atol=1e-9)


def _dng_with_fake_meta(monkeypatch, tmp_path: Path, frame, name: str, **kw):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(meta, "panasonic_distortion_info", lambda payload: PANA_24MM)
    out = tmp_path / name
    rep = dng.write_dng(frame.mosaic, frame.head_sections(), out, meta=b"fake", exif=False, **kw)
    return out, rep


def test_write_dng_lens_opcode_flag(monkeypatch, tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    import base64

    monkeypatch.delenv("RAWSQUEEZE_DNG_LENS_OPCODE", raising=False)
    on, rep_on = _dng_with_fake_meta(monkeypatch, tmp_path, frame_iso100, "on.dng")
    off, rep_off = _dng_with_fake_meta(monkeypatch, tmp_path, frame_iso100, "off.dng", lens_opcode=False)
    monkeypatch.setenv("RAWSQUEEZE_DNG_LENS_OPCODE", "0")
    env_off, rep_env = _dng_with_fake_meta(monkeypatch, tmp_path, frame_iso100, "env.dng")
    forced, rep_forced = _dng_with_fake_meta(monkeypatch, tmp_path, frame_iso100, "forced.dng", lens_opcode=True)
    assert dng.LENS_OPCODE_DEFAULT is True
    for path, rep, has in ((on, rep_on, True), (off, rep_off, False), (env_off, rep_env, False), (forced, rep_forced, True)):
        buf = path.read_bytes()
        t = _tags(buf)
        assert (51022 in t) is has, path.name
        assert rep.lens is not None and rep.lens["opcode"] is has
        assert any("not written as a DNG WarpRectilinear" in w for w in rep.warnings) is (not has)
        # XMP keeps the raw parameters either way
        xmp = bytes(t[700])
        assert b'rawsqueeze:PanasonicDistortionInfo="' + base64.b64encode(PANA_24MM) + b'"' in xmp
        with rawpy.imread(str(path)) as r:
            assert np.array_equal(r.raw_image, frame_iso100.mosaic)
            assert r.postprocess(half_size=True, user_flip=0).shape[:2] == (frame_iso100.height // 2, frame_iso100.width // 2)
        if has:
            (op,) = dng.parse_opcode_list(bytes(t[51022]))
            assert isinstance(op, dng.WarpRectilinear) and len(op.planes) == 1
            assert op.planes[0][0] == pytest.approx(rep.lens["k"][0])
    assert rep_off.lens["reason"] == "disabled"


def test_write_dng_no_distortion_no_opcode(tmp_path: Path, frame_iso100) -> None:  # type: ignore[no-untyped-def]
    rep = dng.write_dng(frame_iso100.mosaic, frame_iso100.head_sections(), tmp_path / "x.dng")
    assert rep.lens is None
    assert 51022 not in _tags((tmp_path / "x.dng").read_bytes())


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
        assert 19.5e6 < rep.size < 21.5e6, rep.size  # 2-comp predictor-1 LJ92 (was 21.6-22.0 MB, row-pair p6)
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
        # IFD0 tags beyond Make/Model/Orientation, original file name, distortion parameters kept
        g1 = et.run_json(["-G1", "-IFD0:ModifyDate", "-OriginalRawFileName", "-XMP-rawsqueeze:all", str(out)])[0]
        src_mod = et.run_json(["-G1", "-IFD0:ModifyDate", str(src)])[0]
        assert g1["IFD0:ModifyDate"] == src_mod["IFD0:ModifyDate"]
        assert g1["IFD0:OriginalRawFileName"] == name
        dist = meta.panasonic_distortion_info(payload)
        assert dist and g1["XMP-rawsqueeze:PanasonicDistortionInfo"] == __import__("base64").b64encode(dist).decode()
        # lens distortion -> OpcodeList3 WarpRectilinear (survives the exiftool transfer)
        assert rep.lens and rep.lens["opcode"] is True and rep.lens["fit_err_px"] < dng.LENS_FIT_MAX_ERR_PX
        assert not any("WarpRectilinear" in w for w in rep.warnings)
        assert et.run_json(["-OpcodeList3", str(out)])[0]["OpcodeList3"] == "WarpRectilinear"
        buf = out.read_bytes()
        e = meta.parse_tiff(buf).ifds[0].entries[51022]
        (op,) = dng.parse_opcode_list(buf[e.data_offset : e.data_offset + e.count])
        pd = meta.parse_panasonic_distortion(dist)
        assert pd is not None and pd.checksum_ok and pd.enabled
        exp, _ = dng.panasonic_warp_rectilinear(pd, active_hw=(fr.height, fr.width),
                                                crop=(fr.crop_ltwh[0] - fr.margins[1], fr.crop_ltwh[1] - fr.margins[0],
                                                      fr.crop_ltwh[2], fr.crop_ltwh[3]))
        assert op.planes == exp.planes and (op.cx, op.cy) == (exp.cx, exp.cy)
        assert not any("copied no EXIF" in w for w in rep.warnings)
    with rawpy.imread(str(out)) as r:
        assert np.array_equal(r.raw_image, fr.mosaic)
        assert list(r.black_level_per_channel) == fr.black_per_channel
        assert r.white_level == fr.white
        s = r.sizes
        assert (s.crop_left_margin, s.crop_top_margin, s.crop_width, s.crop_height) == fr.crop_ltwh
        img = r.postprocess(use_camera_wb=True, user_flip=0, half_size=True)
        assert img.shape == (fr.height // 2, fr.width // 2, 3)
    assert not any(p.name.endswith(".tmp.dng") for p in tmp_path.iterdir())
