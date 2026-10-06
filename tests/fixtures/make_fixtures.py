"""Regenerate the small frame fixtures from samples/ (run once; outputs are committed).

    uv run python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

from rawsqueeze.rawio import crop_frame, load_raw, save_frame_npz

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent

# (sample file, fixture stem, top, left) -- 512x512 crops.
# P1060444 (ISO100): top edge region containing a few saturated pixels and the 8 px
# border outside the default crop.  P1037920 (ISO4000): mid-frame region with some
# saturated pixels.
CROPS = [
    ("P1060444.RW2", "p1060444_iso100_512", 0, 1792),
    ("P1037920.RW2", "p1037920_iso4000_512", 2048, 2560),
]


def main() -> None:
    for name, stem, top, left in CROPS:
        frame = load_raw(ROOT / "samples" / name, want_source_sha256=True)
        sub = crop_frame(frame, top, left, 512, 512)
        npz, js = save_frame_npz(sub, OUT / stem)
        print(stem, npz.stat().st_size, js.stat().st_size, sub.crop_ltwh, sub.iso)


if __name__ == "__main__":
    main()
