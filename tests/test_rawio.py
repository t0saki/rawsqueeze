from __future__ import annotations

import json

import numpy as np
import pytest

from rawsqueeze import rawio
from rawsqueeze.rawio import RawFrame, crop_frame, load_frame_npz, resolve_wb, save_frame_npz


def _assert_frames_equal(a: RawFrame, b: RawFrame) -> None:
    assert np.array_equal(a.mosaic, b.mosaic) and a.mosaic.dtype == b.mosaic.dtype == np.uint16
    assert np.array_equal(a.pattern, b.pattern)
    assert np.array_equal(a.xyz_to_cam, b.xyz_to_cam)
    for name in (
        "color_desc", "black_per_channel", "black_per_position", "white", "camera_wb",
        "daylight_wb", "margins", "visible_hw", "crop_ltwh", "flip", "wb_source", "camera_wb_raw",
        "raw_type", "num_colors", "source_name", "source_size", "source_sha256", "make", "model",
        "iso", "raw_data_offset", "libraw_version", "exif", "crop_origin",
    ):
        assert getattr(a, name) == getattr(b, name), name


def test_fixture_frames_load(frame_iso100: RawFrame, frame_iso4000: RawFrame) -> None:
    for f, iso in ((frame_iso100, 100), (frame_iso4000, 4000)):
        assert f.mosaic.shape == (512, 512) and f.mosaic.dtype == np.uint16
        assert f.pattern.tolist() == [[0, 1], [3, 2]] and f.color_desc == "RGBG"
        assert f.black_per_position == [128, 128, 128, 128] and f.white == 4079
        assert f.iso == iso and f.make == "Panasonic" and f.model == "DC-S9"
        assert f.is_bayer and f.color_roles() == {"R": 0, "G1": 1, "G2": 2, "B": 3}
        assert f.dng_cfa() == [0, 1, 1, 2]
        assert f.xyz_to_cam.shape == (3, 3)
        assert f.bits == 12
        assert (f.mosaic >= f.white).any()  # crops chosen to contain saturated pixels
    assert frame_iso100.crop_ltwh == (0, 8, 512, 504)  # top 8 px border outside default crop
    assert frame_iso100.crop_origin == (0, 1792)


def test_npz_roundtrip(tmp_path, frame_iso100: RawFrame) -> None:
    npz, js = save_frame_npz(frame_iso100, tmp_path / "f.npz")
    assert npz.name == "f.npz" and js.name == "f.json"
    for p in (tmp_path / "f", tmp_path / "f.npz", tmp_path / "f.json"):
        _assert_frames_equal(load_frame_npz(p), frame_iso100)


def test_npz_sha_check(tmp_path, synthetic_frame: RawFrame) -> None:
    save_frame_npz(synthetic_frame, tmp_path / "s")
    meta = json.loads((tmp_path / "s.json").read_text())
    meta["mosaic_sha256"] = "0" * 64
    (tmp_path / "s.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        load_frame_npz(tmp_path / "s")
    assert load_frame_npz(tmp_path / "s", verify=False).height == synthetic_frame.height


def test_crop_frame_consistency(frame_iso100: RawFrame) -> None:
    sub = crop_frame(frame_iso100, 3, 5, 101, 64)  # odd origin rounded down to even
    assert sub.crop_origin == (0 + 2, 1792 + 4)
    assert sub.mosaic.shape == (101, 64)
    assert np.array_equal(sub.mosaic, frame_iso100.mosaic[2:103, 4:68])
    assert sub.pattern.tolist() == frame_iso100.pattern.tolist()
    # parent crop_ltwh (0, 8, 512, 504) -> top 8 rows excluded; window rows 2..103
    assert sub.crop_ltwh == (0, 6, 64, 95)
    assert sub.visible_hw == (101, 64) and sub.margins == (0, 0)
    assert not np.shares_memory(sub.mosaic, frame_iso100.mosaic)  # independent copy
    clipped = crop_frame(frame_iso100, 500, 500, 100, 100)
    assert clipped.mosaic.shape == (12, 12)
    with pytest.raises(ValueError):
        crop_frame(frame_iso100, 600, 0, 10, 10)


def test_crop_frame_margins() -> None:
    f = RawFrame(
        mosaic=np.zeros((40, 60), np.uint16), pattern=np.array([[0, 1], [3, 2]]),
        color_desc="RGBG", black_per_channel=[0] * 4, black_per_position=[0] * 4, white=4095,
        camera_wb=[2, 1, 1.5, 1], daylight_wb=[2, 1, 1.5, 0], xyz_to_cam=np.eye(3),
        margins=(4, 6), visible_hw=(30, 50), crop_ltwh=(8, 6, 40, 26),
    )
    s = crop_frame(f, 2, 2, 20, 20)
    assert s.margins == (2, 4) and s.visible_hw == (18, 16)
    assert s.crop_ltwh == (6, 4, 14, 16)


def test_resolve_wb() -> None:
    assert resolve_wb([534, 256, 431, 0], [2, 1, 1.5, 0]) == ([534.0, 256.0, 431.0, 256.0], "camera")
    assert resolve_wb([0, 0, 0, 0], [2, 1, 1.5, 0]) == ([2.0, 1.0, 1.5, 1.0], "daylight")
    assert resolve_wb([float("nan"), 1, 1, 1], [0, 0, 0, 0]) == ([1.0] * 4, "unity")


def test_head_sections_json(frame_iso4000: RawFrame) -> None:
    hs = frame_iso4000.head_sections()
    s = json.dumps(hs, allow_nan=False)
    back = json.loads(s)
    assert back["cfa"] == {"pattern": [[0, 1], [3, 2]], "color_desc": "RGBG", "dng_cfa": [0, 1, 1, 2]}
    assert back["levels"]["black_per_position"] == [128] * 4 and back["levels"]["white"] == 4079
    assert back["mosaic"]["sha256"] == frame_iso4000.mosaic_sha256()
    assert np.array_equal(np.array(back["color"]["xyz_to_cam"]), frame_iso4000.xyz_to_cam)
    assert back["source"]["name"] == "P1037920.RW2"


def test_synthetic_factory(synthetic_frame_factory) -> None:
    f = synthetic_frame_factory(63, 81, seed=3, saturate_frac=0.01)
    assert f.mosaic.shape == (63, 81) and f.mosaic.dtype == np.uint16
    assert (f.mosaic == 4095).any()
    g = synthetic_frame_factory(63, 81, seed=3, saturate_frac=0.01)
    assert np.array_equal(f.mosaic, g.mosaic)  # deterministic
    flat = synthetic_frame_factory(256, 256, g=2.0, s2=9.0, signal="flat", seed=1)
    x = flat.mosaic[0::2, 0::2].astype(float) - 128
    mu = 0.25 * (4079 - 128) * 0.55
    assert abs(x.mean() - mu) < 3
    assert abs(x.var() / (2.0 * mu + 9.0) - 1) < 0.1


@pytest.mark.slow
def test_load_raw_sample(sample_path) -> None:
    f = rawio.load_raw(sample_path("P1060444.RW2"), want_source_sha256=False)
    assert f.mosaic.shape == (4016, 6016) and f.mosaic.dtype == np.uint16
    assert f.pattern.tolist() == [[0, 1], [3, 2]] and f.color_desc == "RGBG"
    assert f.black_per_channel == [128] * 4 and f.black_per_position == [128] * 4
    assert f.white == 4079
    assert f.camera_wb_raw == [534.0, 256.0, 431.0, 0.0] and f.wb_source == "camera"
    assert f.crop_ltwh == (8, 8, 6000, 4000) and f.margins == (0, 0) and f.flip == 0
    assert f.visible_hw == (4016, 6016)
    assert f.xyz_to_cam[0, 0] == pytest.approx(0.9983)
    assert f.libraw_version == "0.22.1"
    assert f.source_name == "P1060444.RW2" and f.source_size > 20_000_000
    from rawsqueeze.tools import have

    if have("exiftool"):
        assert f.iso == 100 and f.make == "Panasonic" and f.model == "DC-S9"
        assert f.raw_data_offset == 6823936
    g = rawio.load_raw(sample_path("P1060444.RW2"), want_exif=False)
    assert g.iso is None and g.exif == {} and g.make is None
    assert f.mosaic.max() <= 4095
