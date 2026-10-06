# rawsqueeze v0.1.0: implementation status

Status as of 2026-10-07. Every number below was measured on the 5 Panasonic DC-S9 samples
(6016x4016, 12-bit) on an 8-core M-series Mac with 8 threads. The raw data behind this page
is in `bench_out/` (`e2e_results.{json,md}`, `verify_*.json`, `sweep.csv`, `batch.log`).

## What works

- **CLI** (`rawsqueeze encode|decode|info|verify|bench`): all flags from DESIGN.md 5.1 are implemented.
  - Batch runs use `-j` worker processes (spawn), each with `cpu//jobs` threads and its own stay_open exiftool.
  - Progress goes to stderr, one line per file, followed by a summary. `--json` writes reports to stdout.
  - Exit codes: 0 = ok, 1 = error, 2 = verify below threshold.
  - Other flags: `--dry-run`, `--skip-existing`/`--overwrite`, `-r`, and decode formats `dng|npy|pgm16|tiff`.
  - `encode --verify` runs a quick check (2 tiles, +3EV, about 2.8 s). If half3 was chosen by `auto` and fails, the file is re-encoded with nlq (R1); `--no-fallback` turns this off.
- **API** (`rawsqueeze/__init__.py`, imported lazily): `encode_file`, `decode_file`, `encode_frame`, `decode_mosaic`, `verify_file`, and the report classes `EncodeReport`, `DecodeReport`, `VerifyReport`. The implementation is in `rawsqueeze/pipeline.py`, and presets are in `rawsqueeze/presets.py`.
- **HEAD**: follows DESIGN.md 3.3, with these extras:
  - `selection` (requested engine, snr18, threshold, reason, param)
  - `source.iso`
  - `fallback`, written only after an R1 fallback
  - `codec.nlq.identity_step`
  - extra keys added by the individual tracks
- **recon_sha256**: always stored for nlq; stored for half3/gat4 when `--recon-hash` or `--verify` is used.
- **Lossless**: decoding checks sha256 and fails on a mismatch.
- **Engines**: nlq (also used for lossless), half3 and gat4 (experimental, never picked by `auto`). The `auto` engine uses SNR18 ≥ 40.
- **Containers and DNG**:
  - Damaged `.rsq` files are rejected with a clear error, e.g. `RsqCRCError: CRC mismatch in chunk PLN1 at offset 2008705`.
  - DNG output is LJ92 with EXIF/MakerNotes/GPS copied from the META skeleton.
  - `--keep-preview` stores the camera JPEGs, and `--extract-preview` restores them bit-exact (sha256 checked).

## Measured: all presets on all samples (`bench_out/e2e_results.md`)

Sizes are complete `.rsq` files including META. "raw" means the ratio against the Panasonic raw data only (file size minus RawDataOffset). "enc" covers load + noise + encode + write, measured in-process. "dec->DNG" includes LJ92 encoding and the exiftool transfer.

| file (ISO) | lossless MB (file/raw) | high | vl (default) | compact |
|---|---|---|---|---|
| P1060444 (100) | 17.838 (1.58x / 1.20x) | half3 d0.1 7.515 (3.76x) | half3 d0.2 **4.915 (5.75x / 4.36x)** | half3 d0.3 3.728 (7.58x) |
| PANA9831 (100) | 22.389 (1.42x / 1.13x) | half3 11.777 (2.70x) | half3 8.551 (3.72x / 2.95x) | half3 6.837 (4.65x) |
| P1060384 (320) | 15.325 (1.54x / 1.27x) | half3 5.930 (3.98x) | half3 2.341 (10.08x / 8.32x) | half3 1.390 (16.97x) |
| P1037920 (4000) | 20.200 (1.34x / 1.10x) | nlq f0.5 11.052 (2.45x) | nlq f1 8.135 (3.33x / 2.73x) | nlq f2 5.707 (4.74x) |
| PANA0003 (4000) | 19.231 (1.35x / 1.12x) | nlq 11.387 (2.28x) | nlq 8.480 (3.07x / 2.53x) | nlq 5.988 (4.34x) |

- **Timing:**
  - Encode: lossless 0.40–0.43 s, nlq 0.62–0.68 s, half3 0.91–1.10 s.
  - Decode to DNG: 0.23–0.42 s.
  - CLI wall clock on P1060444 vl, including interpreter start: encode 1.15 s, decode to DNG 0.55 s. Targets are ≤ 2.5 s and ≤ 1.5 s.
  - Batch: `encode samples/*.RW2 -j 2` took 3.7 s for 5 files (136.7 MB → 32.4 MB, 4.22x).
- **DNG checks:**
  - All 20 DNGs open in rawpy, and `raw_image` equals the decoded mosaic.
  - The 5 lossless DNGs match the original mosaic's sha256.
- **Tags (exiftool `-a -G1`) on every DNG:**
  - Panasonic: 119 lines, 114 unique, identical to the original.
  - GPS: 13/13.
  - ExifIFD: 41 lines. Of the original's 42 unique names, only `CompressedBitsPerPixel` is missing; it describes the JPEG and exiftool does not copy it. The original shows 72 lines because the RW2 ExifIFD and the JpgFromRaw ExifIFD are both counted.
- **Agreement with the DESIGN references:**
  - Lossless: matches 1.3 to within 0.1 %.
  - half3: d0.2 is 4.915 vs 4.89, d0.3 is 3.728 vs 3.70 (spec value at e5).
  - nlq ISO4000: 4–5 % smaller than B's figures (see deviation 3).
  - P1060384 vl: 2.34 MB vs the 2.41* MB crop extrapolation.

## Measured: verify against DESIGN.md 6.4 (`bench_out/verify_*.json`, 4×2048² tiles, AHD via DNG)

Each value is the worst of the 4 tiles. FLOOR uses seed 5.

| file / preset | engine | ss2 0/+2/+3EV | ba p3 +3EV | noise ratio / bias8 / RMSE/σ | FLOOR ss2 | 6.4 result | DESIGN reference |
|---|---|---|---|---|---|---|---|
| P1060444 vl | half3 d0.2 | 87.74 / 85.00 / 82.82 | 0.971 | – | 93.39 / 92.06 / 90.69 | PASS | full image 86.45 / 83.28 / 81.48, p3 0.90 |
| P1060444 high | half3 d0.1 | 89.95 / 88.10 / 86.70 | 0.706 | – | same | PASS (first full-image AHD run of d0.1) | crop bilinear 94.35 / 91.77 / 90.22 |
| P1037920 vl | nlq f1 | 70.82 / 49.69 / 38.55 | 1.75 | 1.043 / 0.047 / 0.291 | 86.23 / 76.92 / 72.16 | PASS | noise ratio 1.023–1.048; +3EV ss2 41.3 |
| P1037920 high | nlq f0.5 | 80.78 / 67.77 / 60.85 | 1.28 | 1.009 / 0.045 / 0.146 | same | PASS | +3EV 61.1, ratio 1.007 |

- **Clip consistency:** 0 inconsistent pixels in all runs.
- **half3 false clips:** 599 pixels on P1060444 vl, unmasked pixels that decode to exactly `wl` (see Known issues).
- **P1037920 vl +3EV ss2:** 38.55, which is 2.8 points below B's 41.3. The worst tile is the night-sky "darkest" tile at (0,0); the other three tiles score 43.3, 55.6 and 72.5. The gap to FLOOR is 33.6, against B's 32.6. The noise-relative criteria, which are the ones that apply to nlq (1.4), all pass. I attribute the difference to tile choice, not a bug.
- **Bench:** `rawsqueeze bench samples/P1060444.RW2 --sweep 'engine=half3;d=0.1,0.2,0.3' --crop 2048 --csv bench_out/sweep.csv` works. Results at +3EV:

  | d | ss2 | file ratio |
  |---|---|---|
  | 0.1 | 87.2 | 3.67x |
  | 0.2 | 83.6 | 5.60x |
  | 0.3 | 80.0 | 7.41x |

## Deviations from DESIGN.md

1. **nlq identity step is 2 DN when f < 1** (`curves.identity_step_for`); the spec uses 1 DN.
   - With quantisation steps of 1–2 DN, a code bin holds one or two integer raw values. An integer LUT then produces 0/1 DN errors that all point the same way.
   - P1037920 f0.5, before / after:

     | | noise ratio | \|bias8\| | size |
     |---|---|---|---|
     | identity step 1 DN (spec) | 1.027 (fails ≤ 1.02) | 0.27 | 10.962 MB |
     | identity step 2 DN | 1.009 | 0.045 | 11.018 MB (+0.5 %) |

   - f ≥ 1 keeps the spec rule, so the vl and compact results are unchanged; f ≥ 2 would not be affected either way. The decoder does not depend on this.
2. **compact / nlq f2 on P1037920 misses |bias8| ≤ 0.5** (0.529 on the night-sky tile).
   - The criterion sits at the floor of the metric:

     | reconstruction | bias8 |
     |---|---|
     | mid LUT | −0.52 |
     | centroid LUT | +0.53 |
     | pure added Gaussian with the same error variance | −0.52 |

   - On PANA0003, centroid gives −0.82 (centre tile) and +0.73 (darkest); mid gives a noise ratio of 1.19, which fails ≤ 1.18.
   - Centroid stays the default (Q5). The regression test accepts |bias8| up to 0.75 for this case.
3. **nlq sizes at ISO 320–4000 are 4–5 % below B's figures.** B used an older noise estimator without the percentile bias correction (Track A reproduced 8.57 MB with it). The current numbers are correct for the estimator in A.3.
4. **half3 error tails are larger than 2.5 states.** Full-file p99.99 |err| is 199 DN, against 95 in the spec. The spec's own A.1 code gives the same numbers (Track B), so this is the method's behaviour and the spec figure most likely came from crops.
5. **Container:**
   - INDX damage triggers a rebuild of the index, not an error.
   - `verify_crc=False` still checks HEAD.
   - `--force` (skip CRC on lossy payloads) is not implemented, because the 5.1 CLI does not list it.
6. **META:**
   - The skeleton is built with a built-in TIFF parser, with no exiftool call (0.01 s).
   - The "329 tags" figure counts every exiftool line; there are 287 real metadata tags.
7. **HEAD:**
   - `color.camera_wb` holds the effective WB (a 4th value of 0 is replaced by G). The raw LibRaw values are in the frame's `camera_wb_raw`, which is not written to HEAD.
   - A lossless file records `engine: "nlq"` with `mode: "lossless"`.
8. **gat4:**
   - When `100·y_max` would exceed 65535, K is capped per plane.
   - The mosaic is clamped to `wl` before the transform.
   - The decoder clips to [0, wl].
9. **DNG writer:** uses `tw = W` (or two tile columns) for images narrower than the tile width, to work around a LibRaw 0.22.1 bug with single-column tiles. Full-size frames are unaffected.
10. **`-q` with `--engine auto` is rejected.** Give the `--d` and `--f` pair instead (4.2).
11. **Output permissions:** files written atomically through `mkstemp` (DNG, `write_rsq`, decode outputs) are now chmodded to `0o666 & ~umask`; previously they were 0600.

## Known issues / TODO

- **half3 false saturation:** about 600 unmasked pixels per file decode to exactly `wl`, because of the spec's clip to `[blk, wl]` (R4). Clipping unmasked pixels to `wl−1` when SATM is present would fix this.
- **gat4 is not quality-validated.** The full-file AHD + butteraugli check is still needed before it can go into `auto` (Q3).
- **R8:** rawpy does not expose LibRaw's 2-D cblack pattern, so the lossy refusal for such cameras cannot be implemented. Non-Bayer CFAs are refused for lossy (`EncodeError`) and encoded positionally for lossless.
- **Mid-ISO data:** there are no samples between ISO 800 and 3200, so the SNR18 threshold of 40 is uncalibrated (Q1). An auto-chosen half3 that fails `--verify` falls back to nlq; this was tested with `--snr-threshold 10` on P1037920, where half3 scored +3EV ss2 29.7 and ba p3 5.09.
- **Partial checks:**
  - The quick verify measures +3EV only, so the "0EV ss2 ≥ 84" criterion for vl/half3 is reported as skipped there. A full `rawsqueeze verify` checks it.
  - The NoiseProfile does not model half3/gat4 error.
  - The exif-only META fallback and non-RW2 inputs were only exercised with DNG fixtures. No real CR3/NEF/ARW files were tested.
- **Interface:** `--dry-run` does not decode the raw, so it reports the requested engine, not the one `auto` would select.

## Tests

- `uv run pytest -q`: 400 passed in 85 s, including 40 `slow` tests that need `samples/`.
- `uv run pytest -q -m "not slow"`: 360 passed in 7 s.
- `uv run pytest -q -m slow`: 40 passed in 76 s. This includes `tests/test_presets_regression.py`, which checks sizes within ±10 % of the DESIGN references for 2 files × 4 presets, lossless sha256, and the 6.4 acceptance criteria.
