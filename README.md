# rawsqueeze

Adaptive dual-engine camera RAW compressor. Encodes LibRaw-readable 2x2 Bayer raw files into a
`.rsq` container (JPEG XL payloads) and decodes back to standard DNG (LJ92) with full metadata.

See `docs/DESIGN.md` for the specification and `docs/STATUS.md` for measured results and
known issues.

## Requirements

Python >= 3.12 with the dependencies in `pyproject.toml` (`uv sync`). External command-line
tools are found on `PATH`; `RAWSQUEEZE_<TOOL>` (e.g. `RAWSQUEEZE_EXIFTOOL=/opt/bin/exiftool`)
points at a specific binary.

| tool | used for | without it |
|---|---|---|
| `exiftool` | ISO/Make/Model at encode, EXIF/MakerNotes/GPS transfer into the DNG | DNGs carry only the core DNG tags (no EXIF/MakerNotes/GPS); ISO-based noise cap and `UniqueCameraModel` are unavailable |
| `cjxl`, `djxl` | `--keep-preview` / `--extract-preview` (camera JPEG stored as a lossless JXL transcode) | previews are not stored (warning) |
| `ssimulacra2`, `butteraugli_main` | `verify`, `encode --verify` (perceptual criteria of DESIGN.md 6.4) | those criteria are *not measured*: `verify` exits 2, and `encode --verify` treats half3 as failed (auto falls back to nlq) |

## Usage

```
uv run rawsqueeze encode IMG.RW2                     # -> IMG.rsq (preset vl, engine auto)
uv run rawsqueeze encode photos/ -r -o out/ -j 2     # a directory tree, 2 files in parallel
uv run rawsqueeze encode IMG.RW2 --preset lossless   # bit-exact
uv run rawsqueeze encode IMG.RW2 --verify            # quick verify (+3EV), half3 -> nlq fallback
uv run rawsqueeze decode IMG.rsq                     # -> IMG.dng
uv run rawsqueeze info IMG.rsq
uv run rawsqueeze verify IMG.rsq --original IMG.RW2  # full DESIGN.md 6.4 check
uv run rawsqueeze bench IMG.RW2 --sweep 'engine=half3;d=0.1,0.2,0.3' --crop 2048 --csv out.csv
uv run rawsqueeze --help
```

Presets (`--preset`; `auto` picks half3 when SNR at 18 % grey >= 60, otherwise nlq):

| preset | half3 | nlq | notes |
|---|---|---|---|
| `lossless` | – | f = 0 | bit-exact mosaic |
| `archival` | – | f = 0 | lossless + camera JPEG kept |
| `high` | d = 0.1 | f = 0.5 | survives > +3 EV and heavy grading |
| `vl` (default) | d = 0.2 | f = 1.0 | near visually lossless after +2..+3 EV |
| `compact` | d = 0.3 | f = 2.0 | higher compression |

Exit codes: 0 ok; 1 error (any file); 2 verify below the DESIGN.md 6.4 thresholds or a required
criterion could not be measured; 130 interrupted (Ctrl-C stops a batch immediately and prints
the partial summary).

## Tests

```
uv run pytest -q -m "not slow"   # fast tests (~10 s, no samples needed)
uv run pytest -q -m slow         # tests that need samples/*.RW2
uv run pytest -q                 # everything (~1.5 min)
```
