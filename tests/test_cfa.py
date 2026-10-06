from __future__ import annotations

import itertools

import numpy as np
import pytest

from rawsqueeze import cfa

# All 4 RGGB-like 2x2 arrangements expressed with LibRaw-style indices for desc "RGBG"
# (0=R, 1=G, 2=B, 3=G2).
ARRANGEMENTS = {
    "RGGB": [[0, 1], [3, 2]],
    "GRBG": [[1, 0], [2, 3]],
    "GBRG": [[1, 2], [0, 3]],
    "BGGR": [[2, 1], [3, 0]],
}


@pytest.mark.parametrize("name", list(ARRANGEMENTS))
def test_split_merge_roundtrip(name: str) -> None:
    rng = np.random.default_rng(1)
    m = rng.integers(0, 4096, (6, 10), dtype=np.uint16)
    P = cfa.split_planes(m, ARRANGEMENTS[name])
    assert P.shape == (4, 3, 5) and P.dtype == np.uint16 and P.flags.c_contiguous
    for k, (dy, dx) in enumerate(cfa.POSITIONS):
        assert np.array_equal(P[k], m[dy::2, dx::2])
    assert np.array_equal(cfa.merge_planes(P), m)
    assert np.array_equal(cfa.merge_planes(list(P)), m)


def test_split_rejects_odd() -> None:
    with pytest.raises(ValueError):
        cfa.split_planes(np.zeros((5, 4), np.uint16))
    with pytest.raises(ValueError):
        cfa.split_planes(np.zeros((4, 4, 2), np.uint16))


def test_color_roles_rgbg() -> None:
    roles = cfa.color_roles([[0, 1], [3, 2]], b"RGBG")
    assert roles == {"R": 0, "G1": 1, "G2": 2, "B": 3}
    assert cfa.dng_cfa_pattern([[0, 1], [3, 2]], "RGBG") == [0, 1, 1, 2]


@pytest.mark.parametrize(
    "name,expected_roles,expected_dng",
    [
        ("RGGB", {"R": 0, "G1": 1, "G2": 2, "B": 3}, [0, 1, 1, 2]),
        ("GRBG", {"R": 1, "G1": 0, "G2": 3, "B": 2}, [1, 0, 2, 1]),
        ("GBRG", {"R": 2, "G1": 0, "G2": 3, "B": 1}, [1, 2, 0, 1]),
        ("BGGR", {"R": 3, "G1": 1, "G2": 2, "B": 0}, [2, 1, 1, 0]),
    ],
)
def test_roles_all_arrangements(name: str, expected_roles: dict, expected_dng: list) -> None:
    pat = ARRANGEMENTS[name]
    assert cfa.color_roles(pat, "RGBG") == expected_roles
    assert cfa.dng_cfa_pattern(pat, "RGBG") == expected_dng
    assert cfa.is_bayer_rggb_like(pat, "RGBG")


def test_roles_three_color_desc() -> None:
    # Some cameras report desc "RGB" with pattern using index 1 twice.
    assert cfa.color_roles([[0, 1], [1, 2]], "RGB") == {"R": 0, "G1": 1, "G2": 2, "B": 3}


def test_non_bayer_rejected() -> None:
    assert not cfa.is_bayer_rggb_like([[0, 1], [2, 3]], "CMYG")
    with pytest.raises(ValueError):
        cfa.color_roles([[0, 0], [1, 2]], "RGBG")  # 2R
    with pytest.raises(ValueError):
        cfa.dng_cfa_pattern([[0, 1], [2, 3]], "CMYG")
    with pytest.raises(ValueError):
        cfa.color_roles(np.zeros((6, 6), int), "RGBG")


def test_black_per_position() -> None:
    assert cfa.black_per_position([[0, 1], [3, 2]], [10, 11, 12, 13]) == [10, 11, 13, 12]
    assert cfa.black_per_position([[2, 1], [3, 0]], [10, 11, 12, 13]) == [12, 11, 13, 10]


@pytest.mark.parametrize("h,w", list(itertools.product([3, 4, 7], [3, 6, 9])))
def test_pad_even_and_crop(h: int, w: int) -> None:
    rng = np.random.default_rng(h * 10 + w)
    m = rng.integers(0, 4096, (h, w), dtype=np.uint16)
    p, orig = cfa.pad_even(m)
    assert orig == (h, w)
    assert p.shape[0] % 2 == 0 and p.shape[1] % 2 == 0
    assert p.shape == (h + h % 2, w + w % 2)
    assert np.array_equal(p[:h, :w], m)
    if h % 2:
        assert np.array_equal(p[h, :w], m[h - 1])  # edge replication
    if w % 2:
        assert np.array_equal(p[:h, w], m[:, w - 1])
    assert np.array_equal(cfa.crop_to(p, orig), m)
    # full chain through planes
    assert np.array_equal(cfa.crop_to(cfa.merge_planes(cfa.split_planes(p)), orig), m)


def test_pad_even_noop_returns_same_object() -> None:
    m = np.zeros((4, 6), np.uint16)
    p, orig = cfa.pad_even(m)
    assert p is m and orig == (4, 6)
    with pytest.raises(ValueError):
        cfa.crop_to(m, (5, 6))
