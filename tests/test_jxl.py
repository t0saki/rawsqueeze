from __future__ import annotations

import numpy as np
import pytest

from rawsqueeze import cfa, jxl


def test_versions() -> None:
    v = jxl.libjxl_version()
    assert v and v[0].isdigit() and v.count(".") >= 1
    assert jxl.imagecodecs_version()


@pytest.mark.parametrize(
    "dtype,shape",
    [
        (np.uint8, (37, 53)),
        (np.uint16, (37, 53)),
        (np.uint16, (32, 40, 4)),
        (np.float32, (37, 53)),
        (np.float32, (37, 53, 3)),
    ],
)
def test_lossless_roundtrip_dtypes(dtype: type, shape: tuple) -> None:
    rng = np.random.default_rng(0)
    if dtype == np.float32:
        a = rng.random(shape).astype(np.float32)  # [0, 1]
    else:
        hi = 256 if dtype == np.uint8 else 4096
        a = rng.integers(0, hi, shape).astype(dtype)
    b = jxl.encode_lossless(a, effort=3, threads=2)
    assert isinstance(b, bytes)
    d = jxl.decode(b, threads=2)
    assert d.dtype == a.dtype
    assert d.shape == a.shape
    assert np.array_equal(d, a)


def test_uint16_codestream_forms() -> None:
    rng = np.random.default_rng(0)
    a = rng.integers(0, 4096, (32, 32)).astype(np.uint16)
    b_cont = jxl.encode_lossless(a)
    # libjxl wraps 16-bit lossless in an ISO-BMFF container (jxll level box) even with
    # usecontainer=False; bitspersample=12 yields a bare codestream.
    assert b_cont[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n"
    b_bare = jxl.encode_lossless(a, bitspersample=12)
    assert b_bare[:2] == b"\xff\x0a"
    assert np.array_equal(jxl.decode(b_cont), a) and np.array_equal(jxl.decode(b_bare), a)
    a8 = rng.integers(0, 256, (32, 32)).astype(np.uint8)
    assert jxl.encode_lossless(a8)[:2] == b"\xff\x0a"


def test_lossy_float_gray_and_rgb() -> None:
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:64, 0:64] / 63.0
    gray = (0.2 + 0.6 * xx * yy + 0.01 * rng.standard_normal((64, 64))).astype(np.float32)
    b = jxl.encode_lossy(gray, distance=0.2, effort=5, threads=2)
    d = jxl.decode(b)
    assert d.dtype == np.float32 and d.shape == gray.shape
    assert np.abs(d - gray).mean() < 0.01
    rgb = np.stack([gray, gray * 1.5, gray * 0.5], -1).astype(np.float32)  # values > 1 kept
    rgb[0, 0, 1] = 2.5
    d = jxl.decode(jxl.encode_lossy(rgb, distance=0.2))
    assert d.dtype == np.float32 and d.shape == rgb.shape
    assert d.max() > 1.0
    assert np.abs(d - rgb).mean() < 0.02


def test_lossy_rejects_zero_distance_and_bad_dtype() -> None:
    with pytest.raises(ValueError):
        jxl.encode_lossy(np.zeros((8, 8), np.float32), distance=0)
    with pytest.raises(TypeError):
        jxl.encode_lossless(np.zeros((8, 8), np.int32))


def test_planes_of_real_fixture_lossless(frame_iso100) -> None:
    P = cfa.split_planes(frame_iso100.mosaic)
    for k in range(4):
        b = jxl.encode_lossless(P[k], effort=3, threads=1)
        d = jxl.decode(b)
        assert d.dtype == np.uint16 and np.array_equal(d, P[k])
    stack = np.ascontiguousarray(P.transpose(1, 2, 0))
    d = jxl.decode(jxl.encode_lossless(stack, effort=3))
    assert d.shape == stack.shape and np.array_equal(d, stack)


def test_real_fixture_float_paths(frame_iso100) -> None:
    f = frame_iso100
    P = cfa.split_planes(np.minimum(f.mosaic, f.white)).astype(np.float32)
    n = np.clip((P - 128) / (f.white - 128), 0, 1).astype(np.float32)
    # float32 gray lossless ([0,1]) and lossy
    d = jxl.decode(jxl.encode_lossless(n[1], effort=3))
    assert d.dtype == np.float32 and np.array_equal(d, n[1])
    d = jxl.decode(jxl.encode_lossy(n[1], distance=0.2))
    assert d.dtype == np.float32 and np.abs(d - n[1]).mean() < 5e-3
    # float32 RGB lossy (half3-like, values may exceed 1)
    rgb = np.ascontiguousarray(np.stack([n[0] * 2.09, (n[1] + n[2]) / 2, n[3] * 1.68], -1))
    d = jxl.decode(jxl.encode_lossy(rgb, distance=0.2, effort=5))
    assert d.dtype == np.float32 and d.shape == rgb.shape
    assert np.abs(d - rgb).mean() < 1e-2
    # uint8 gray real data
    u8 = (n[1] * 255).astype(np.uint8)
    d = jxl.decode(jxl.encode_lossless(u8))
    assert d.dtype == np.uint8 and np.array_equal(d, u8)
