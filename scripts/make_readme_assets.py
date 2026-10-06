#!/usr/bin/env python3
"""Regenerate the README figures in ``docs/images/``.

Usage (from the repository root)::

    uv run python scripts/make_readme_assets.py             # everything (needs samples/*.RW2)
    uv run python scripts/make_readme_assets.py --only charts   # charts only (CSV, no samples)
    uv run python scripts/make_readme_assets.py --table zh      # print the README data table

Photo figures use the real package: every sample is encoded with the real preset
(:func:`rawsqueeze.encode_file`), decoded (:func:`rawsqueeze.decode_mosaic`), and the original
and the reconstruction are developed through the *same* pipeline as ``rawsqueeze verify``
(:func:`rawsqueeze.verify.develop_many`: in-memory DNG -> rawpy/LibRaw AHD, camera WB, LibRaw's
BT.709-like tone curve, highlight clip) at the stated EV.  Crops are 1:1 sensor pixels shown 2x
with nearest-neighbour upscaling, so no pixel is invented or smoothed.

Charts read ``docs/results/final_results.csv`` (the numbers of docs/STATUS.md).

Dev dependencies: matplotlib (charts, colour maps), Pillow (raster output; installed with
scikit-image).  A CJK-capable system font is needed for the Chinese labels (PingFang SC,
Hiragino Sans GB, Noto Sans SC or Arial Unicode MS).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
CSV = ROOT / "docs" / "results" / "final_results.csv"
SAMPLES = ROOT / "samples"

PRESETS = ("lossless", "high", "vl", "compact")
LANGS = ("zh", "en")

# --------------------------------------------------------------------------------------
# what to show


@dataclass(frozen=True)
class Crop:
    key: str
    sample: str
    cx: int
    """Crop centre in developed (DefaultCrop, 6000x4000) coordinates."""
    cy: int
    edit: str = "ev3"
    """'ev3' = +3 EV push in LibRaw; 'grade' = +2 EV, warmer WB and lifted shadows (numpy)."""
    size: int = 384


# All crops were checked by eye: no people, faces, cockpit windows or artwork.
CROPS = (
    Crop("lamp", "PANA9831", 1480, 1400),  # ISO 100, half3: lamp highlight against dark foliage
    Crop("engine", "P1037920", 2600, 2440),  # ISO 4000, nlq: engine fan in shadow, night grain
    Crop("trees", "P1060444", 420, 1700, edit="grade"),  # ISO 100, half3: trunks and leaves, graded
)
FLICKER = (  # (crop key, output stem, size at 1:1, centre)
    ("engine", "flicker_engine", 300, (2610, 2500)),  # more of the fan, less of the clipped nacelle
    ("lamp", "flicker_lamp", 300, (1480, 1400)),
)
HEATMAP = (("lamp", 24), ("engine", 8))  # (crop key, amplification of |difference|)
THUMBS = (("P1060444", "trees"), ("PANA9831", "lamp"), ("P1037920", "engine"))
ISO = {"PANA9831": 100, "P1060444": 100, "P1037920": 4000}

# --------------------------------------------------------------------------------------
# text

T = {
    "zh": {
        "rw2": "RW2 原片",
        "rsq": "rawsqueeze 压缩后",
        "ev3": "后期 +3 EV",
        "grade": "后期 +2 EV · 白平衡偏暖 · 提亮阴影",
        "crop": "1:1 局部，最近邻放大 2 倍",
        "orig_short": "原片",
        "rec_short": "压缩后",
        "hm_img": "画面（{edit}）",
        "hm_err": "压缩误差 |原片 − 压缩后| × {k}",
        "hm_noise": "参照：再加一份同等传感器噪声 × {k}",
        "hm_bar": "8-bit 显示值之差 × 放大倍数 k",
        "hm_zero": "0 = 完全相同",
        "hm_end": "≥ 255÷k 级（到顶）",
    },
    "en": {
        "rw2": "RW2 original",
        "rsq": "rawsqueeze",
        "ev3": "pushed +3 EV",
        "grade": "+2 EV · warmer WB · lifted shadows",
        "crop": "1:1 crop, 2x nearest-neighbour",
        "orig_short": "original",
        "rec_short": "compressed",
        "hm_img": "image ({edit})",
        "hm_err": "compression error |orig − rsq| × {k}",
        "hm_noise": "reference: one more dose of sensor noise × {k}",
        "hm_bar": "8-bit display difference × amplification k",
        "hm_zero": "0 = identical",
        "hm_end": "≥ 255÷k levels (saturated)",
    },
}

# photo panels sit on a neutral dark "viewer" background that works in both GitHub themes
P_BG = (24, 24, 24)
P_FG = (240, 240, 240)
P_FG2 = (170, 170, 170)

# chart palette (validated against GitHub light / dark surfaces)
THEMES = {
    "light": {"bg": "#ffffff", "text": "#0b0b0b", "text2": "#52514e", "grid": "#e6e5e1", "band": "#f3f2ee",
                  "series": {"lossless": "#2a78d6", "high": "#eb6834", "vl": "#1baf7a", "compact": "#eda100"},
                  "bar": "#2a78d6", "base": "#a3a29c"},
    "dark": {"bg": "#0d1117", "text": "#ffffff", "text2": "#c3c2b7", "grid": "#30363d", "band": "#161b22",
                 "series": {"lossless": "#3987e5", "high": "#d95926", "vl": "#199e70", "compact": "#c98500"},
                 "bar": "#3987e5", "base": "#6e7681"},
}

# --------------------------------------------------------------------------------------
# fonts


def _font_file(families: tuple[str, ...]) -> tuple[str, str] | None:
    from matplotlib import font_manager as fm

    for fam in families:
        try:
            path = fm.findfont(fm.FontProperties(family=fam), fallback_to_default=False)
        except ValueError:
            continue
        return fam, path
    return None


CJK_FAMILIES = ("PingFang SC", "Hiragino Sans GB", "Noto Sans SC", "Arial Unicode MS")
_FONTS: dict[tuple[int, bool], object] = {}


def pil_font(size: int, bold: bool = False):
    """A CJK-capable PIL font (picks the right face inside .ttc collections)."""
    from PIL import ImageFont

    key = (size, bold)
    if key in _FONTS:
        return _FONTS[key]
    found = _font_file(CJK_FAMILIES)
    font = None
    if found:
        fam, path = found
        want = "Semibold" if bold else "Regular"
        for idx in range(16):
            try:
                f = ImageFont.truetype(path, size, index=idx)
            except OSError:
                break
            name, style = f.getname()
            if name == fam and (style == want or not path.endswith(".ttc")):
                font = f
                break
            if font is None and name == fam:
                font = f
        if font is None:
            font = ImageFont.truetype(path, size)
    if font is None:
        print("warning: no CJK font found; Chinese labels will not render", file=sys.stderr)
        font = ImageFont.load_default(size)
    _FONTS[key] = font
    return font


# --------------------------------------------------------------------------------------
# samples: encode -> decode -> develop


class Sample:
    """One RW2 encoded with ``preset``; original and reconstruction mosaics in memory."""

    def __init__(self, name: str, preset: str, cache: Path) -> None:
        import rawsqueeze
        from rawsqueeze.container import read_rsq
        from rawsqueeze.rawio import load_raw

        self.name = name
        self.src = SAMPLES / f"{name}.RW2"
        if not self.src.exists():
            raise SystemExit(f"{self.src} not found (photo figures need samples/*.RW2; try --only charts)")
        self.rsq = cache / f"{name}.{preset}.rsq"
        if not self.rsq.exists():
            rep = rawsqueeze.encode_file(self.src, self.rsq, preset=preset)
            print(f"  encoded {name} ({preset}): {rep.engine} {rep.param}, {rep.out_size:,} B, "
                  f"{rep.ratio_file:.2f}x, {rep.enc_s:.2f} s")
        r = read_rsq(self.rsq)
        self.head = r.head
        self.engine = f"{r.head['engine']} {r.head['selection'].get('param', '')}".strip()
        self.src_mb = self.src.stat().st_size / 1e6
        self.rsq_mb = self.rsq.stat().st_size / 1e6
        self.ratio = self.src_mb / self.rsq_mb
        self.orig = load_raw(self.src, want_exif=False).mosaic
        self.rec = rawsqueeze.decode_mosaic(r)
        self._dev: dict[tuple[str, float], np.ndarray] = {}

    def noisy(self, seed: int = 7) -> np.ndarray:
        """Original + one fresh realisation of the sensor noise model of HEAD (same sigma(x))."""
        planes = self.head["noise"]["planes"]
        blk = self.head["levels"]["black_per_position"]
        white = int(self.head["levels"]["white"])
        rng = np.random.default_rng(seed)
        m = self.orig.astype(np.float64)
        out = m.copy()
        for pos, (dy, dx) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            p = planes[pos]
            sub = m[dy::2, dx::2]
            var = float(p["g_used"]) * np.maximum(sub - blk[pos], 0) + float(p["s2"])
            out[dy::2, dx::2] = sub + rng.standard_normal(sub.shape) * np.sqrt(var)
        return np.clip(np.rint(out), 0, white).astype(np.uint16)

    def develop(self, which: tuple[str, ...], evs: tuple[float, ...]) -> None:
        from rawsqueeze.verify import develop_many

        todo = [w for w in which if any((w, float(e)) not in self._dev for e in evs)]
        if not todo:
            return
        src = {"orig": lambda: self.orig, "rec": lambda: self.rec, "noisy": self.noisy}
        self._dev.update(develop_many({w: src[w]() for w in todo}, self.head, evs))

    def img(self, which: str, ev: float) -> np.ndarray:
        self.develop((which,), (ev,))
        return self._dev[(which, float(ev))]


# --------------------------------------------------------------------------------------
# edits (all on the 16-bit developed output)


def grade(img16: np.ndarray) -> np.ndarray:
    """+2 EV, warmer white balance, lifted shadows -- a heavy but ordinary grade.

    The 0 EV render is linearised with LibRaw's own tone curve, edited in linear light and
    re-encoded with the same curve, i.e. what a raw editor does after demosaicking.
    """
    from rawsqueeze.verify import libraw_eotf, libraw_oetf

    lin = libraw_eotf(img16.astype(np.float64) / 65535.0)
    lin = lin * 4.0 * np.array([1.18, 1.0, 0.82])  # +2 EV, warmer
    y = libraw_oetf(np.clip(lin, 0, 1))
    y = y + 0.35 * y * (1.0 - y) ** 2  # shadow / midtone lift
    return np.clip(np.rint(y * 255.0), 0, 255).astype(np.uint8)


def to8(img16: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(img16.astype(np.float64) / 257.0), 0, 255).astype(np.uint8)


def rendered(s: Sample, which: str, edit: str) -> np.ndarray:
    if edit == "grade":
        return grade(s.img(which, 0.0))
    return to8(s.img(which, 3.0))


def crop(img: np.ndarray, c: Crop, size: int | None = None) -> np.ndarray:
    n = size or c.size
    y0, x0 = c.cy - n // 2, c.cx - n // 2
    return np.ascontiguousarray(img[y0 : y0 + n, x0 : x0 + n])


def up2(a: np.ndarray) -> np.ndarray:
    return a.repeat(2, axis=0).repeat(2, axis=1)


def fmt_mb(mb: float) -> str:
    return f"{mb:.1f} MB" if mb >= 10 else f"{mb:.2f} MB"


# --------------------------------------------------------------------------------------
# figures: comparison panel, flicker, heatmap, thumbnails


def save_jpeg(im, path: Path, quality: int = 88, subsampling: int = 0) -> None:
    im.save(path, quality=quality, subsampling=subsampling, optimize=True, progressive=True)
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} kB)")


def panel(s: Sample, c: Crop, lang: str, out: Path) -> None:
    from PIL import Image, ImageDraw

    t = T[lang]
    a = up2(crop(rendered(s, "orig", c.edit), c))
    b = up2(crop(rendered(s, "rec", c.edit), c))
    n = a.shape[0]
    m, gap, head, lab = 16, 12, 44, 36
    W, H = 2 * n + gap + 2 * m, head + lab + n + m
    im = Image.new("RGB", (W, H), P_BG)
    im.paste(Image.fromarray(a), (m, head + lab))
    im.paste(Image.fromarray(b), (m + n + gap, head + lab))
    d = ImageDraw.Draw(im)
    title = f"{s.name} · ISO {ISO[s.name]} · {t[c.edit]} · {t['crop']}"
    d.text((m, 12), title, font=pil_font(19), fill=P_FG2)
    d.text((m, head + 2), f"{t['rw2']} · {fmt_mb(s.src_mb)}", font=pil_font(22, True), fill=P_FG)
    d.text((m + n + gap, head + 2), f"{t['rsq']} · {fmt_mb(s.rsq_mb)}（{s.ratio:.1f}×）" if lang == "zh"
           else f"{t['rsq']} · {fmt_mb(s.rsq_mb)} ({s.ratio:.1f}×)", font=pil_font(22, True), fill=P_FG)
    save_jpeg(im, out / f"compare_{c.key}_{lang}.jpg")


def flicker(s: Sample, c: Crop, size: int, lang: str, path: Path) -> None:
    """A/B animation (animated WebP, lossless): original / reconstruction every 0.8 s."""
    from PIL import Image, ImageDraw

    t = T[lang]
    frames = []
    for which, label in (("orig", t["orig_short"]), ("rec", t["rec_short"])):
        a = up2(crop(rendered(s, which, c.edit), c, size))
        im = Image.fromarray(a)
        d = ImageDraw.Draw(im, "RGBA")
        f = pil_font(24, True)
        x0, y0, x1, y1 = d.textbbox((0, 0), label, font=f)
        d.rounded_rectangle((10, 10, 10 + (x1 - x0) + 24, 10 + (y1 - y0) + 18), 8, fill=(0, 0, 0, 170))
        d.text((22 - x0, 19 - y0), label, font=f, fill=(255, 255, 255, 255))
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=800, loop=0,
                   lossless=True, method=6, quality=100)
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} kB)")


def heatmap(items: list[tuple[Sample, Crop, int]], lang: str, out: Path) -> None:
    from matplotlib import colormaps
    from PIL import Image, ImageDraw

    t = T[lang]
    cmap = colormaps["magma"]
    n = items[0][1].size
    m, gap, lab, rowhead, bar = 16, 10, 34, 34, 64
    W = 3 * n + 2 * gap + 2 * m
    H = m + len(items) * (rowhead + lab + n + gap) + bar
    im = Image.new("RGB", (W, H), P_BG)
    d = ImageDraw.Draw(im)
    y = m
    for s, c, k in items:
        base = crop(rendered(s, "orig", c.edit), c).astype(np.float64)
        rec = crop(rendered(s, "rec", c.edit), c).astype(np.float64)
        noisy = crop(rendered(s, "noisy", c.edit), c).astype(np.float64)
        e_rec = np.abs(base - rec).mean(axis=2)
        e_noise = np.abs(base - noisy).mean(axis=2)
        print(f"  heatmap {s.name}: mean |diff| rec {e_rec.mean():.2f}, noise {e_noise.mean():.2f} "
              f"(8-bit levels, x{k})")
        tiles = [base.astype(np.uint8)] + [
            (cmap(np.clip(e * k / 255.0, 0, 1))[..., :3] * 255).astype(np.uint8) for e in (e_rec, e_noise)
        ]
        edit = t[c.edit]
        d.text((m, y), f"{s.name} · ISO {ISO[s.name]} · {s.engine} · {fmt_mb(s.rsq_mb)}"
               f"（{s.ratio:.1f}×）" if lang == "zh" else
               f"{s.name} · ISO {ISO[s.name]} · {s.engine} · {fmt_mb(s.rsq_mb)} ({s.ratio:.1f}×)",
               font=pil_font(20, True), fill=P_FG)
        labels = (t["hm_img"].format(edit=edit), t["hm_err"].format(k=k), t["hm_noise"].format(k=k))
        for i, (tile, label) in enumerate(zip(tiles, labels)):
            x = m + i * (n + gap)
            d.text((x, y + rowhead), label, font=pil_font(16), fill=P_FG2)
            im.paste(Image.fromarray(tile), (x, y + rowhead + lab))
        y += rowhead + lab + n + gap
    # colour bar
    bw = n * 2 + gap
    x0 = m + n + gap
    grad = (cmap(np.linspace(0, 1, bw))[:, :3] * 255).astype(np.uint8)
    im.paste(Image.fromarray(np.repeat(grad[None], 14, axis=0)), (x0, y + 6))
    d.text((x0, y + 26), t["hm_zero"], font=pil_font(15), fill=P_FG2)
    end = t["hm_end"]
    tw = d.textlength(end, font=pil_font(15))
    d.text((x0 + bw - tw, y + 26), end, font=pil_font(15), fill=P_FG2)
    tw = d.textlength(t["hm_bar"], font=pil_font(15))
    d.text((x0 + (bw - tw) / 2, y + 26), t["hm_bar"], font=pil_font(15), fill=P_FG2)
    save_jpeg(im, out / f"heatmap_{lang}.jpg", quality=84, subsampling=2)


def thumbs(samples: dict[str, Sample], out: Path) -> None:
    """Context strip (language-neutral): downscaled 0 EV renders with the crop boxes marked."""
    from PIL import Image, ImageDraw

    tw, m, gap, lab = 520, 16, 12, 34
    cells = []
    for name, key in THUMBS:
        s = samples[name]
        img = Image.fromarray(to8(s.img("orig", 0.0)))
        scale = tw / img.width
        th = round(img.height * scale)
        img = img.resize((tw, th), Image.LANCZOS)
        d = ImageDraw.Draw(img)
        c = next(c for c in CROPS if c.key == key)
        h = c.size / 2 * scale
        d.rectangle((c.cx * scale - h, c.cy * scale - h, c.cx * scale + h, c.cy * scale + h),
                    outline=(255, 214, 10), width=3)
        cells.append((s, img))
    th = cells[0][1].height
    W = len(cells) * tw + (len(cells) - 1) * gap + 2 * m
    im = Image.new("RGB", (W, m + th + lab), P_BG)
    d = ImageDraw.Draw(im)
    for i, (s, img) in enumerate(cells):
        x = m + i * (tw + gap)
        im.paste(img, (x, m))
        d.text((x, m + th + 6), f"{s.name} · ISO {ISO[s.name]}", font=pil_font(17), fill=P_FG2)
    save_jpeg(im, out / "samples.jpg", quality=85)


def photo_figures(out: Path, cache: Path, langs: tuple[str, ...], what: set[str]) -> None:
    samples: dict[str, Sample] = {}
    for c in CROPS:
        if c.sample not in samples:
            print(f"{c.sample}:")
            samples[c.sample] = Sample(c.sample, "vl", cache)
    crops = {c.key: c for c in CROPS}
    for c in CROPS:
        s = samples[c.sample]
        s.develop(("orig", "rec"), (0.0, 3.0))
        if "panels" in what:
            for lang in langs:
                panel(s, c, lang, out)
    if "flicker" in what:
        for key, stem, size, (cx, cy) in FLICKER:
            c = replace(crops[key], cx=cx, cy=cy)
            for lang in langs:
                flicker(samples[c.sample], c, size, lang, out / f"{stem}_{lang}.webp")
    if "heatmap" in what:
        items = [(samples[crops[k].sample], crops[k], amp) for k, amp in HEATMAP]
        for s, c, _ in items:
            s.develop(("noisy",), (3.0,) if c.edit == "ev3" else (0.0,))
        for lang in langs:
            heatmap(items, lang, out)
    if "thumbs" in what:
        thumbs(samples, out)


# --------------------------------------------------------------------------------------
# charts


def load_csv() -> list[dict[str, str]]:
    with open(CSV, newline="") as f:
        return list(csv.DictReader(f))


def chart_data(rows: list[dict[str, str]]):
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by[r["preset"]].append(r)
    # RW2 file size of every sample: from samples/ if present, else MB x ratio from the CSV
    rw2 = {}
    for r in by["lossless"]:
        p = SAMPLES / f"{r['file']}.RW2"
        rw2[r["file"]] = p.stat().st_size / 1e6 if p.exists() else float(r["mb"]) * float(r["ratio_file"])
    totals = {p: sum(float(r["mb"]) for r in by[p]) for p in PRESETS}
    return by, rw2, totals


CHART_TXT = {
    "zh": {
        "ratio_title": "压缩率随 ISO 的变化（13 张样张，Panasonic DC-S9）",
        "ratio_y": "压缩率（RW2 文件大小 ÷ .rsq 大小，对数刻度）",
        "ratio_x": "ISO（对数刻度）",
        "half3": "half3 引擎\n（低噪声：感知编码）",
        "nlq": "nlq 引擎（高噪声：按噪声量化 + 无损编码）",
        "switch": "SNR18 = 60",
        "names": {"lossless": "lossless 无损", "high": "high", "vl": "vl（默认）", "compact": "compact"},
        "bar_title": "13 张样张的总体积",
        "base": "RW2 原文件",
        "bar_x": "总体积（MB）",
    },
    "en": {
        "ratio_title": "Compression ratio vs ISO (13 samples, Panasonic DC-S9)",
        "ratio_y": "ratio (RW2 file size ÷ .rsq size, log scale)",
        "ratio_x": "ISO (log scale)",
        "half3": "half3 engine\n(low noise: perceptual)",
        "nlq": "nlq engine (high noise: noise-relative quantisation + lossless coding)",
        "switch": "SNR18 = 60",
        "names": {"lossless": "lossless", "high": "high", "vl": "vl (default)", "compact": "compact"},
        "bar_title": "Total size of the 13 samples",
        "base": "RW2 original",
        "bar_x": "total size (MB)",
    },
}


def _mpl_setup():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # "bold" -> Semibold notice

    found = _font_file(CJK_FAMILIES)
    fams = ([found[0]] if found else []) + ["DejaVu Sans"]
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": fams,
        "svg.fonttype": "path",  # glyphs as paths: renders on GitHub without the font
        "svg.hashsalt": "rawsqueeze",  # deterministic ids
        "axes.unicode_minus": False,
    })
    return plt


def _style_axes(ax, th) -> None:
    ax.set_facecolor(th["bg"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(th["grid"])
    ax.tick_params(colors=th["text2"], labelsize=10, length=0)
    ax.grid(True, color=th["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def ratio_chart(rows, lang: str, theme: str, path: Path) -> None:
    plt = _mpl_setup()
    from matplotlib.ticker import FixedLocator, NullLocator

    th, tx = THEMES[theme], CHART_TXT[lang]
    by, _, _ = chart_data(rows)
    fig, ax = plt.subplots(figsize=(9.6, 5.2), dpi=100)
    fig.patch.set_facecolor(th["bg"])
    _style_axes(ax, th)
    ax.grid(True, axis="y", color=th["grid"], linewidth=0.8)
    ax.grid(False, axis="x")
    ax.set_xscale("log")
    ax.set_yscale("log")
    # engine regions: half3 below the SNR18 threshold (between ISO 320 and 640 on the DC-S9)
    split = 450
    ax.axvspan(70, split, color=th["band"], zorder=0, linewidth=0)
    ax.axvline(split, color=th["grid"], linewidth=1.2, linestyle=(0, (3, 3)), zorder=1)
    for p in PRESETS:
        pts = sorted(((float(r["iso"]), float(r["ratio_file"])) for r in by[p]), key=lambda v: v[0])
        xs = sorted({x for x, _ in pts})
        mean = [np.mean([y for x2, y in pts if x2 == x]) for x in xs]
        col = th["series"][p]
        lw, ms = (3.2, 9) if p == "vl" else (2.0, 8)
        ax.plot(xs, mean, color=col, linewidth=lw, zorder=3 if p == "vl" else 2, solid_capstyle="round")
        ax.scatter([x for x, _ in pts], [y for _, y in pts], s=ms**2, color=col, zorder=4,
                   edgecolors=th["bg"], linewidths=1.5)
        # direct label at the line end (text colour; the marker beside it carries identity)
        ax.annotate(f"{tx['names'][p]}  {mean[-1]:.1f}×", (xs[-1], mean[-1]), xytext=(12, 0),
                    textcoords="offset points", va="center", ha="left", fontsize=11,
                    color=th["text"], fontweight="bold" if p == "vl" else "normal")
    ax.set_xlim(70, 60000)
    ax.set_ylim(1.15, 22)
    isos = sorted({int(float(r["iso"])) for r in rows})
    major = [100, 320, 640, 1250, 2000, 4000, 10000, 51200]
    ax.xaxis.set_major_locator(FixedLocator(major))
    ax.xaxis.set_minor_locator(FixedLocator([i for i in isos if i not in major]))
    ax.set_xticklabels([str(i) for i in major])
    ax.tick_params(axis="x", which="minor", length=3, color=th["grid"])
    ax.yaxis.set_major_locator(FixedLocator([1.5, 2, 3, 4, 5, 7, 10, 15, 20]))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_yticklabels(["1.5×", "2×", "3×", "4×", "5×", "7×", "10×", "15×", "20×"])
    ax.text(190, 2.2, tx["half3"], color=th["text2"], fontsize=9.5, va="center", ha="center")
    ax.text(560, 19.5, tx["nlq"], color=th["text2"], fontsize=9.5, va="top")
    ax.text(split * 1.04, 1.27, tx["switch"], color=th["text2"], fontsize=9, va="bottom")
    ax.set_xlabel(tx["ratio_x"], color=th["text2"], fontsize=10)
    ax.set_ylabel(tx["ratio_y"], color=th["text2"], fontsize=10)
    ax.set_title(tx["ratio_title"], color=th["text"], fontsize=13, loc="left", pad=12)
    fig.subplots_adjust(left=0.085, right=0.78, top=0.90, bottom=0.12)
    fig.savefig(path, format="svg", facecolor=th["bg"], metadata={"Date": None})
    plt.close(fig)
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} kB)")


def total_chart(rows, lang: str, theme: str, path: Path) -> None:
    plt = _mpl_setup()
    th, tx = THEMES[theme], CHART_TXT[lang]
    _, rw2, totals = chart_data(rows)
    base = sum(rw2.values())
    labels = [tx["base"]] + [tx["names"][p] for p in PRESETS]
    vals = [base] + [totals[p] for p in PRESETS]
    cols = [th["base"]] + [th["bar"]] * len(PRESETS)
    fig, ax = plt.subplots(figsize=(8.6, 3.3), dpi=100)
    fig.patch.set_facecolor(th["bg"])
    _style_axes(ax, th)
    ax.grid(True, axis="x", color=th["grid"], linewidth=0.8)
    ax.grid(False, axis="y")
    ys = np.arange(len(vals))[::-1]
    ax.barh(ys, vals, height=0.62, color=cols, zorder=2)
    for y, v, lab in zip(ys, vals, labels):
        txt = f"{v:.1f} MB" if v == base else f"{v:.1f} MB  ·  {base / v:.2f}×"
        ax.text(v + base * 0.012, y, txt, va="center", ha="left", fontsize=11,
                color=th["text"], fontweight="bold" if "vl" in lab else "normal")
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=11, color=th["text"])
    for t in ax.get_yticklabels():
        if "vl" in t.get_text():
            t.set_fontweight("bold")
    ax.set_xlim(0, base * 1.22)
    ax.set_xlabel(tx["bar_x"], color=th["text2"], fontsize=10)
    ax.set_title(tx["bar_title"], color=th["text"], fontsize=13, loc="left", pad=10)
    fig.subplots_adjust(left=0.17, right=0.97, top=0.86, bottom=0.17)
    fig.savefig(path, format="svg", facecolor=th["bg"], metadata={"Date": None})
    plt.close(fig)
    print(f"  wrote {path.relative_to(ROOT)} ({path.stat().st_size / 1e3:.0f} kB)")


def charts(out: Path, langs: tuple[str, ...]) -> None:
    rows = load_csv()
    for lang in langs:
        for theme in THEMES:
            ratio_chart(rows, lang, theme, out / f"chart_ratio_iso_{lang}_{theme}.svg")
            total_chart(rows, lang, theme, out / f"chart_totals_{lang}_{theme}.svg")


# --------------------------------------------------------------------------------------
# README table


def _r2(v: str) -> str:
    """Round a CSV decimal string half-up to 2 places (float formatting would print 8.135 as 8.13)."""
    from decimal import ROUND_HALF_UP, Decimal

    return str(Decimal(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def print_table(lang: str) -> None:
    rows = load_csv()
    by_file: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for r in rows:
        by_file[r["file"]][r["preset"]] = r
    _, rw2, totals = chart_data(rows)
    if lang == "zh":
        print("| 文件 | ISO | 引擎（vl） | RW2 MB | lossless | high | vl（默认） | compact |")
    else:
        print("| file | ISO | engine (vl) | RW2 MB | lossless | high | vl (default) | compact |")
    print("|---|---:|---|---:|---:|---:|---:|---:|")
    for f, d in by_file.items():
        vl = d["vl"]
        cells = [f"{_r2(d[p]['mb'])} MB · {float(d[p]['ratio_file']):.2f}×" for p in PRESETS]
        cells[2] = f"**{cells[2]}**"
        print(f"| {f} | {vl['iso']} | {vl['engine']} {vl['param']} | {rw2[f]:.1f} | " + " | ".join(cells) + " |")
    base = sum(rw2.values())
    tot = [f"{totals[p]:.1f} MB · {base / totals[p]:.2f}×" for p in PRESETS]
    tot[2] = f"**{tot[2]}**"
    name = "**合计**" if lang == "zh" else "**total**"
    print(f"| {name} | | | {base:.1f} | " + " | ".join(tot) + " |")


# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="panels,flicker,heatmap,thumbs,charts",
                    help="comma-separated subset of panels,flicker,heatmap,thumbs,charts")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--cache", type=Path, default=None,
                    help="keep the encoded .rsq files here (default: a temporary directory)")
    ap.add_argument("--lang", default="zh,en")
    ap.add_argument("--table", choices=LANGS, help="print the README per-file table and exit")
    a = ap.parse_args(argv)
    if a.table:
        print_table(a.table)
        return 0
    what = {w.strip() for w in a.only.split(",") if w.strip()}
    langs = tuple(x.strip() for x in a.lang.split(",") if x.strip())
    a.out.mkdir(parents=True, exist_ok=True)
    if "charts" in what:
        charts(a.out, langs)
    if what & {"panels", "flicker", "heatmap", "thumbs"}:
        if a.cache:
            a.cache.mkdir(parents=True, exist_ok=True)
            photo_figures(a.out, a.cache, langs, what)
        else:
            with tempfile.TemporaryDirectory(prefix="rsq_readme_") as td:
                photo_figures(a.out, Path(td), langs, what)
    total = sum(p.stat().st_size for p in a.out.iterdir() if p.is_file())
    print(f"{a.out.relative_to(ROOT) if a.out.is_relative_to(ROOT) else a.out}: {total / 1e6:.2f} MB total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
