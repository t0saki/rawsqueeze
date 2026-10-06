"""Shared pytest fixtures.

Fixtures for other test modules:

* ``frame_iso100`` / ``frame_iso4000``: 512x512 RawFrame crops of P1060444 / P1037920
  (committed in tests/fixtures/, no samples needed).  Function-scoped copies.
* ``fixture_frames``: dict name -> RawFrame of both.
* ``synthetic_frame_factory``: callable ``make_synthetic_frame(**kw) -> RawFrame``
  (seeded Poisson-Gaussian mosaic, var = g*x + s2 per position); see its docstring.
* ``synthetic_frame``: 256x256 default synthetic frame.
* ``sample_path``: callable ``name -> Path`` that skips the test if samples/<name> is absent.
* ``tmp_scratch``: alias of ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest

from rawsqueeze.cfa import POSITIONS, black_per_position
from rawsqueeze.rawio import RawFrame, load_frame_npz

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = ROOT / "samples"

FIXTURE_STEMS = {
    "iso100": "p1060444_iso100_512",
    "iso4000": "p1037920_iso4000_512",
}

# DC-S9 colour data (from P1060444) used for synthetic frames.
_DCS9_XYZ_TO_CAM = np.array(
    [[0.9983, -0.389, -0.0841], [-0.418, 1.2164, 0.2263], [-0.0249, 0.1139, 0.5766]]
)


def make_synthetic_frame(
    h: int = 256,
    w: int = 256,
    *,
    g: float | Sequence[float] = 0.6,
    s2: float | Sequence[float] = 4.0,
    black: int = 128,
    white: int = 4079,
    clip_value: int = 4095,
    pattern: Sequence[Sequence[int]] = ((0, 1), (3, 2)),
    color_desc: str = "RGBG",
    seed: int = 0,
    signal: str = "gradient",
    max_level: float = 0.9,
    saturate_frac: float = 0.0,
    iso: float | None = None,
) -> RawFrame:
    """Seeded synthetic raw frame with Poisson-Gaussian noise.

    Clean signal ``x`` (DN above black, per position): ``signal="gradient"`` -- a smooth
    diagonal ramp 0..max_level*(white-black) plus a mild low-frequency texture;
    ``"flat"`` -- constant 0.25*(white-black); ``"zeros"`` -- 0.
    Noise: ``g * Poisson(x / g) + N(0, s2)`` (var = g*x + s2); ``g``/``s2`` may be per-position
    sequences.  Result = rint(noisy + black) clipped to [0, clip_value].  ``saturate_frac``
    sets a top-left square block (that fraction of the area) to ``clip_value`` (>= white).
    """
    rng = np.random.default_rng(seed)
    hh, ww = (h + 1) // 2, (w + 1) // 2
    X = white - black
    gs = [float(v) for v in (g if isinstance(g, Sequence) else [g] * 4)]
    s2s = [float(v) for v in (s2 if isinstance(s2, Sequence) else [s2] * 4)]
    yy, xx = np.mgrid[0:hh, 0:ww].astype(np.float64)
    if signal == "gradient":
        base = (yy / max(hh - 1, 1) + xx / max(ww - 1, 1)) / 2.0
        tex = 0.03 * np.sin(xx / 7.0) * np.cos(yy / 11.0)
        clean = np.clip(base + tex, 0.0, 1.0) * max_level * X
    elif signal == "flat":
        clean = np.full((hh, ww), 0.25 * X)
    elif signal == "zeros":
        clean = np.zeros((hh, ww))
    else:
        raise ValueError(f"unknown signal {signal!r}")
    m = np.empty((2 * hh, 2 * ww), dtype=np.uint16)
    color_gain = {0: 0.55, 1: 1.0, 2: 0.7, 3: 1.0}
    pat = np.asarray(pattern, dtype=np.int64)
    for k, (dy, dx) in enumerate(POSITIONS):
        x = clean * color_gain.get(int(pat[dy, dx]), 1.0)
        gk = gs[k]
        noisy = gk * rng.poisson(np.maximum(x, 0) / gk) + rng.normal(0.0, np.sqrt(s2s[k]), x.shape)
        m[dy::2, dx::2] = np.clip(np.rint(noisy + black), 0, clip_value).astype(np.uint16)
    m = np.ascontiguousarray(m[:h, :w])
    if saturate_frac > 0:
        side = int(round(np.sqrt(saturate_frac * h * w)))
        m[:side, :side] = clip_value
    blk_ch = [black] * 4
    return RawFrame(
        mosaic=m,
        pattern=pat.copy(),
        color_desc=color_desc,
        black_per_channel=blk_ch,
        black_per_position=black_per_position(pat, blk_ch),
        white=int(white),
        camera_wb=[534.0, 256.0, 431.0, 256.0],
        daylight_wb=[2.135, 0.9385, 1.3927, 0.0],
        xyz_to_cam=_DCS9_XYZ_TO_CAM.copy(),
        margins=(0, 0),
        visible_hw=(h, w),
        crop_ltwh=(0, 0, w, h),
        flip=0,
        wb_source="camera",
        camera_wb_raw=[534.0, 256.0, 431.0, 0.0],
        source_name=f"synthetic_{h}x{w}_s{seed}",
        make="Synthetic",
        model="PoissonGaussian",
        iso=iso,
        libraw_version="",
        exif={"synthetic": {"g": gs, "s2": s2s, "seed": seed, "signal": signal}},
    )


@pytest.fixture(scope="session")
def _fixture_frames_cached() -> dict[str, RawFrame]:
    return {k: load_frame_npz(FIXTURES / stem) for k, stem in FIXTURE_STEMS.items()}


def _copy(frame: RawFrame) -> RawFrame:
    return dataclasses.replace(
        frame,
        mosaic=frame.mosaic.copy(),
        pattern=frame.pattern.copy(),
        xyz_to_cam=frame.xyz_to_cam.copy(),
        exif=dict(frame.exif),
    )


@pytest.fixture
def fixture_frames(_fixture_frames_cached: dict[str, RawFrame]) -> dict[str, RawFrame]:
    return {k: _copy(v) for k, v in _fixture_frames_cached.items()}


@pytest.fixture
def frame_iso100(_fixture_frames_cached: dict[str, RawFrame]) -> RawFrame:
    return _copy(_fixture_frames_cached["iso100"])


@pytest.fixture
def frame_iso4000(_fixture_frames_cached: dict[str, RawFrame]) -> RawFrame:
    return _copy(_fixture_frames_cached["iso4000"])


@pytest.fixture
def synthetic_frame_factory() -> Callable[..., RawFrame]:
    return make_synthetic_frame


@pytest.fixture
def synthetic_frame() -> RawFrame:
    return make_synthetic_frame()


@pytest.fixture
def sample_path() -> Callable[[str], Path]:
    def get(name: str) -> Path:
        p = SAMPLES / name
        if not p.exists():
            pytest.skip(f"sample {name} not available")
        return p

    return get


@pytest.fixture
def tmp_scratch(tmp_path: Path) -> Path:
    return tmp_path
