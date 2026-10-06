# rawsqueeze v0.1.0: implementation status

Status as of 2026-10-07 (after the review-fix round). Numbers in "Measured after the review
fixes" and "Threshold calibration" cover all 13 Panasonic DC-S9 samples (ISO 100–51200); the
older sections were measured on the original 5 samples (6016x4016, 12-bit). All on an 8-core
M-series Mac with 8 threads. The raw data behind this page
is in `bench_out/` (`e2e_results.{json,md}`, `verify_*.json`, `sweep.csv`, `batch.log`).

## What works

- **CLI** (`rawsqueeze encode|decode|info|verify|bench`): all flags from DESIGN.md 5.1 are implemented.
  - Batch runs use `-j` worker processes (spawn), each with `cpu//jobs` threads and its own stay_open exiftool.
  - Progress goes to stderr, one line per file, followed by a summary. `--json` writes reports to stdout.
  - Exit codes: 0 = ok, 1 = error, 2 = verify below threshold (or a required criterion not measurable), 130 = interrupted.
  - Other flags: `--dry-run`, `--skip-existing`/`--overwrite`, `-r`, and decode formats `dng|npy|pgm16|tiff`.
  - `encode --verify` runs a quick check (2 tiles, +3EV, about 2.8 s). If half3 was chosen by `auto` and fails, the file is re-encoded with nlq (R1); `--no-fallback` turns this off. A criterion that was requested but could not be measured (ssimulacra2/butteraugli_main missing) counts as a failure, so the fallback still protects quality; a warning is printed up front.
  - Batch robustness: Ctrl-C stops a `-j` batch at once (workers ignore SIGINT, the parent cancels and terminates them, prints the partial summary, exit 130). A worker that dies (OOM kill, crash in LibRaw) no longer aborts the batch: the unfinished files are re-run one at a time in a fresh worker and only a file that crashes again is reported as an error. `default_jobs` budgets 3 GB per file with `--verify` (1.5 GB otherwise).
  - Planning: when encoding a directory, `x.dng` next to a raw `x.*` (a decoded copy) is ignored; two different inputs that map to one output are a per-file error instead of aborting the batch; the duplicate check is case-folded on macOS/Windows.
  - `verify` refuses a wrong original (mosaic sha256 differs from HEAD) with exit 1 before decoding, unless `--force`; unknown `--metrics`, empty `--ev` and `--tiles < 1` are errors. Unreadable inputs give `RawReadError` ("file not found" / "not a camera raw file" / "file truncated or corrupt") instead of a traceback.
- **API** (`rawsqueeze/__init__.py`, imported lazily): `encode_file`, `decode_file`, `encode_frame`, `decode_mosaic`, `verify_file`, and the report classes `EncodeReport`, `DecodeReport`, `VerifyReport`. The implementation is in `rawsqueeze/pipeline.py`, and presets are in `rawsqueeze/presets.py`.
- **HEAD**: follows DESIGN.md 3.3, with these extras:
  - `selection` (requested engine, snr18, threshold, reason, param)
  - `source.iso`
  - `fallback`, written only after an R1 fallback
  - `codec.nlq.identity_step`
  - extra keys added by the individual tracks
- **recon_sha256**: always stored for nlq; stored for half3/gat4 when `--recon-hash` or `--verify` is used.
- **Lossless**: decoding checks sha256 and fails on a mismatch.
- **Engines**: nlq (also used for lossless), half3 and gat4 (experimental, never picked by `auto`). The `auto` engine uses SNR18 ≥ 60 (calibrated, see "Threshold calibration"; was 40). half3 has a gamut guard: when libjxl's XYB would clamp negative opsin mixes (saturated blue/cyan LEDs), the colour matrix is blended towards the identity just enough (HEAD `codec.half3.matrix_blend`). half3/gat4 never decode an unmasked pixel to exactly `wl`.
- **API**: `presets.resolve` / `encode_file(**opts)` reject unknown option keys (with a "did you mean" hint); only the experimental `gat4_K` passes through to `extra`.
- **Containers and DNG**:
  - Damaged `.rsq` files are rejected with a clear error, e.g. `RsqCRCError: CRC mismatch in chunk PLN1 at offset 2008705`.
  - DNG output is LJ92 with EXIF/MakerNotes/GPS copied from the META skeleton. Tiles use Adobe DNG Converter's layout (2 interleaved components, predictor 1, frame `th x tw/2`, own numpy encoder with per-tile optimal Huffman tables), which rawspeed/darktable accepts as well as LibRaw, the Adobe SDK and Apple ImageIO (checked with rawpy, imagecodecs and `sips`). DNGs are 5–7 % smaller than before (P1060444 lossless 20.45 vs 21.59 MB); encoding the tiles costs about 0.3 s instead of 0.1 s.
  - Metadata transfer also copies IFD0 ModifyDate/Artist/Copyright and sets OriginalRawFileName; a transfer that copies no EXIF (unusable skeleton) is reported as a warning instead of a silent success. Panasonic in-camera distortion correction parameters (DistortionInfo, 0x0119) are kept in XMP `rawsqueeze:PanasonicDistortionInfo`, with a warning (see Known issues).
  - `--keep-preview` stores the camera JPEGs, and `--extract-preview` restores them bit-exact (sha256 checked).

## Measured after the review fixes: 13 samples, vl (engine auto) and lossless

Script: `e2e.py` in the review scratch directory; raw results in its `e2e/e2e.json`. In-process `encode_file` / `decode_file` with 8 threads; "enc" covers load + noise + encode + write, "dec→DNG" includes LJ92 encoding and the exiftool transfer. Bit-exact means the decoded mosaic *and* the decoded DNG (read back with rawpy) equal the original `raw_image`.

| file | ISO | SNR18 | vl engine | vl MB | vl ratio file / raw | vl enc s | vl dec→DNG s | lossless MB | lossless ratio file / raw | lossless enc s | lossless dec→DNG s | bit-exact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1060444 | 100 | 102.1 | half3 d0.2 | 4.915 | 5.75x / 4.36x | 0.99 | 0.67 | 17.838 | 1.58x / 1.20x | 0.42 | 0.49 | yes |
| PANA9831 | 100 | 103.0 | half3 d0.2 | 8.551 | 3.72x / 2.95x | 1.00 | 0.54 | 22.389 | 1.42x / 1.13x | 0.43 | 0.50 | yes |
| P1060384 | 320 | 70.5 | half3 d0.2 | 2.341 | 10.08x / 8.32x | 0.94 | 0.49 | 15.325 | 1.54x / 1.27x | 0.40 | 0.47 | yes |
| ISO640_PANA0036 | 640 | 47.7 | nlq f1 | 8.097 | 3.05x / 2.45x | 0.62 | 0.48 | 15.993 | 1.54x / 1.24x | 0.42 | 0.48 | yes |
| ISO800_PANA0021 | 800 | 39.4 | nlq f1 | 7.934 | 3.17x / 2.55x | 0.62 | 0.49 | 16.730 | 1.50x / 1.21x | 0.41 | 0.49 | yes |
| ISO1250_P1060413 | 1250 | 34.7 | nlq f1 | 7.880 | 3.25x / 2.59x | 0.62 | 0.49 | 17.208 | 1.49x / 1.19x | 0.42 | 0.51 | yes |
| ISO1600_PANA9976 | 1600 | 28.1 | nlq f1 | 8.223 | 3.35x / 2.60x | 0.65 | 0.51 | 18.920 | 1.45x / 1.13x | 0.42 | 0.51 | yes |
| ISO2000_PANA9996 | 2000 | 24.2 | nlq f1 | 8.547 | 3.21x / 2.47x | 0.64 | 0.50 | 18.289 | 1.50x / 1.16x | 0.42 | 0.49 | yes |
| ISO3200_PANA9944 | 3200 | 18.5 | nlq f1 | 9.242 | 3.20x / 2.46x | 0.65 | 0.51 | 20.949 | 1.41x / 1.09x | 0.45 | 0.51 | yes |
| P1037920 | 4000 | 17.2 | nlq f1 | 8.135 | 3.33x / 2.73x | 0.62 | 0.51 | 20.200 | 1.34x / 1.10x | 0.42 | 0.55 | yes |
| PANA0003 | 4000 | 16.5 | nlq f1 | 8.480 | 3.07x / 2.53x | 0.63 | 0.50 | 19.231 | 1.35x / 1.12x | 0.42 | 0.50 | yes |
| ISO10000_PANA9951 | 10000 | 12.5 | nlq f1 | 7.525 | 3.90x / 3.29x | 0.64 | 0.51 | 22.573 | 1.30x / 1.10x | 0.42 | 0.51 | yes |
| ISO51200_PANA0010 | 51200 | 5.9 | nlq f1 | 9.384 | 3.56x / 2.84x | 0.66 | 0.51 | 24.316 | 1.37x / 1.10x | 0.44 | 0.51 | yes |

- Totals: 359.4 MB of RW2 → vl 99.3 MB (3.62x), lossless 250.0 MB (1.44x).
- Engine changes against threshold 40: only ISO640_PANA0036 (SNR18 47.7) moves from half3 (4.20 MB, +3EV ss2 41.9 / ba p3 7.56, FAIL) to nlq (8.10 MB). All other sizes are identical to the pre-fix runs (half3 `matrix_blend` = 0 on P1060444, PANA9831 and P1060384).
- CLI wall clock on P1060444 vl, including interpreter start: encode 1.18 s, decode to DNG 0.78 s (targets ≤ 2.5 s / ≤ 1.5 s). Batch `encode samples/*.RW2 -j 2`: 13 files in 7.0 s (359.4 MB → 99.3 MB, 3.62x).
- Ctrl-C on a `-j 2` batch of 10 files 4 s in: exit 130 after 0.2 s, 4 encoded, 6 "not run", summary printed (before: 69 s, all 10 written). Killing one worker mid-batch (`kill -9`): all 5 files still encoded, exit 0 (before: traceback, no output at all).

## Measured: all presets on the original 5 samples (`bench_out/e2e_results.md`, before the review fixes)

The `.rsq` sizes below are unchanged by the review fixes (all 5 files keep their engine at threshold 60 and the gamut guard does not trigger on them). Decode-to-DNG timings are now about 0.5 s and DNG sizes 5–7 % smaller (new LJ92 encoder).

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
- **half3 false clips:** 599 pixels on P1060444 vl at the time of this run (unmasked pixels decoded to exactly `wl`); fixed in the review round: 0 now (clip to `wl−1` when SATM is present).
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

## Threshold calibration (Q1, review round 2026-10)

Calibrator's run over all 13 samples (scripts and full verify JSONs: `review/calib/` in the
scratch directory). Every value is the worst of 4 × 2048² tiles, AHD via DNG, FLOOR seed 5.
"noise ratio" is the rec/orig noise-std ratio of the centre and darkest tiles at +3EV. half3
numbers are *before* the gamut guard (so the ISO640 row shows the clamping failure).

| file | ISO | SNR18 (before ISO cap) | auto @40 | half3 d0.2 MB | half3 ss2 0/+2/+3 | half3 ba p3 +3EV | half3 ba max +3EV | half3 noise ratio | nlq f1 MB | nlq noise ratio | nlq ss2 0/+2/+3 | nlq ba p3/max +3EV | FLOOR ss2 0/+2/+3 | half3/nlq size |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1060444 | 100 | 102.1 (102.1) | half3 | 4.915 | 87.7/85.0/82.8 | 0.97 | 4.1 | 0.965 | 11.858 | 1.016 | 92.7/90.6/89.1 | 0.48/2.2 | 93.4/92.1/90.7 | 0.41 |
| PANA9831 | 100 | 103.0 (70.0) | half3 | 8.551 | 84.3/79.7/**76.8** | **1.23** | 4.8 | 0.987 | 16.609 | 1.009 | 92.2/90.4/89.1 | 0.54/3.0 | 93.1/90.9/89.6 | 0.51 |
| P1060384 | 320 | 70.5 | half3 | 2.341 | 89.4/82.2/**77.2** | **1.08** | 2.7 | 0.706 | 8.020 | 1.060 | 92.4/87.4/84.2 | 0.68/2.2 | 93.8/89.3/87.5 | 0.29 |
| ISO640_PANA0036 | 640 | 47.7 | **half3** | 4.198 | 85.4/**32.2/41.9** | **7.56** | **22.4** | 0.844 | 8.097 | 1.033 | 89.2/81.3/76.1 | 0.98/3.0 | 92.5/87.7/84.6 | 0.52 |
| ISO800_PANA0021 | 800 | 39.4 | nlq | 4.711 | 85.3/74.9/68.3 | 1.38 | 3.7 | 0.850 | 7.934 | 1.049 | 88.2/80.2/75.1 | 1.03/3.2 | 92.4/88.2/85.3 | 0.59 |
| ISO1250_P1060413 | 1250 | 34.7 | nlq | 4.795 | 82.7/66.6/57.0 | 1.59 | 3.7 | 0.855 | 7.880 | 1.042 | 86.8/75.6/68.7 | 1.11/3.2 | 91.2/84.0/79.8 | 0.61 |
| ISO1600_PANA9976 | 1600 | 28.1 | nlq | 6.261 | 82.6/71.7/65.9 | 1.49 | 3.8 | 0.930 | 8.223 | 1.044 | 85.5/76.5/71.9 | 1.15/3.2 | 91.9/86.9/84.2 | 0.76 |
| ISO2000_PANA9996 | 2000 | 24.2 | nlq | 6.578 | 76.8/57.9/47.5 | 1.80 | 5.8 | 0.905 | 8.547 | 1.054 | 81.4/66.6/58.0 | 1.36/3.8 | 89.5/81.8/77.7 | 0.77 |
| ISO3200_PANA9944 | 3200 | 18.5 (18.0) | nlq | 8.476 | 75.9/60.5/51.2 | 3.27 | 19.8 | 0.916 | 9.242 | 1.066 | 77.6/62.0/52.7 | 1.56/5.0 | 89.8/84.1/80.9 | 0.92 |
| P1037920 | 4000 | 17.2 | nlq | 7.763 | 65.2/40.3/29.7 | 5.09 | 27.2 | 0.903 | 8.135 | 1.043 | 70.8/49.7/38.6 | 1.75/5.5 | 86.2/76.9/72.2 | 0.95 |
| PANA0003 | 4000 | 16.5 | nlq | 7.073 | 64.4/38.0/25.1 | 2.22 | 10.3 | 0.924 | 8.480 | 1.062 | 70.2/47.5/35.6 | 1.76/5.0 | 85.9/76.5/71.8 | 0.83 |
| ISO10000_PANA9951 | 10000 | 12.5 | nlq | 8.753 | 75.8/63.1/59.2 | 1.64 | 5.6 | 0.969 | 7.524 | 1.059 | 71.4/57.3/55.3 | 1.64/5.1 | 88.7/83.7/80.8 | **1.16** |
| ISO51200_PANA0010 | 51200 | 5.9 | nlq | 11.243 | 62.2/48.1/42.3 | 2.33 | 8.5 | 0.966 | 9.384 | 1.026 | 41.6/21.1/13.1 | 3.02/9.0 | 84.4/78.7/75.9 | **1.20** |

Extra runs: half3 d0.1 at equal quality to nlq is *larger* than nlq at ISO800 (8.13 vs 7.93 MB)
and ISO1250 (8.28 vs 7.88 MB). ISO640 half3 with `use_matrix=False`: 3.06 MB, +3EV ss2 61.2,
ba p3 1.53 (no blow-up, still soft).

Decision: **default threshold SNR18 = 60** (≈ ISO 450 on the DC-S9).

- At SNR18 ≤ 40 (ISO ≥ 800) half3 is no longer visually lossless (grain flattened: noise ratio 0.85–0.93, ba p3 1.38–1.80, visible softening/blotchy grain in +3EV crops) and has no size advantage at equal quality; at ISO ≥ ~10000 it is even larger than nlq.
- At SNR18 ≥ 70 half3 is worth it (0.29–0.51 of the nlq size, no visible difference at 4x/+3EV).
- The only sample between 40 and 70 (ISO640) failed because of the XYB gamut clamp, not noise; 60 keeps a margin. The clamp itself is fixed separately (gamut guard): the same file forced to half3 now gives 3.76 MB, +3EV ss2 66.7, ba p3 1.42 (was 4.20 MB / 41.9 / 7.56), and the raw bias on its 13k saturated-blue sites drops from R +67 / G +100 / B −31 DN to 0.0 / −0.2 / −0.6 DN.

## Review fixes (2026-10)

| finding | fix | files |
|---|---|---|
| half3-xyb-gamut-clamp (high) | Gamut guard: per-site blend factor needed to keep `OPSIN·M·cam + bias ≥ 0`; `M ← (1−α)M + αI` with the smallest α (1e-6 of sites may stay negative); `matrix_blend`/`gamut_neg_sites` in HEAD; decoder unchanged (uses Minv) | `engines/half3.py`, `tests/test_half3.py` |
| calibration Q1 | default SNR18 threshold 40 → 60 | `select.py`, `cli.py`, `docs/DESIGN.md` 2.4/Q1/R1/R7, `tests/test_pipeline.py` |
| half3-matrix-dropped-bggr-gbrg | WB and matrix rows looked up by colour letter (LibRaw's green index 3) | `engines/half3.py`, `tests/test_half3.py` |
| half3-gat4-false-saturation | unmasked pixels clipped to `wl−1` when SATM is present | `engines/half3.py`, `engines/gat4.py`, `tests/test_half3.py` |
| lj92-rawspeed-incompatible | new LJ92 tile encoder: 2 components, predictor 1, frame `th × tw/2` (Adobe layout); `effective_tile` avoids LibRaw's `2·tw == width` special case; DNGs 5–7 % smaller | `dng.py`, `tests/test_dng.py`, `tests/test_meta.py` |
| panasonic-distortion-lost | DistortionInfo (0x0119) kept in XMP `rawsqueeze:PanasonicDistortionInfo` + warning; no WarpRectilinear opcode yet | `meta.py`, `dng.py`, `tests/test_dng.py` |
| exif-transfer-silent-failure | single `-q` (warnings kept) + post-check of `ExifIFD:ExifVersion`; warning when nothing arrived | `meta.py`, `tests/test_dng.py` |
| ifd0-tags-dropped | copy IFD0 ModifyDate/Artist/Copyright, set OriginalRawFileName (camera Software stays replaced by rawsqueeze's, Q10) | `meta.py`, `tests/test_dng.py` |
| ctrl-c-batch-continues | worker initializer ignores SIGINT; parent cancels + terminates workers, partial summary, exit 130 | `cli.py`, `tests/test_cli.py` |
| broken-pool-kills-batch | BrokenProcessPool caught per future; unfinished jobs re-run one by one in a fresh pool; per-file error only for a repeat crash; 3 GB/job budget with `--verify` | `cli.py`, `tests/test_cli.py` |
| verify-false-pass-missing-metrics | requested-but-unmeasured criteria are failures ("not measured (tool not found)"); unrequested EVs/metrics are skipped; `--metrics` names validated, empty `--ev` / `--tiles < 1` rejected; warning in `encode --verify` | `verify.py`, `cli.py`, `pipeline.py`, `bench.py`, `tests/test_verify.py`, `tests/test_cli.py` |
| raw-load-errors-traceback | `RawReadError` (ValueError) with "file not found" / "not a camera raw file" / "file truncated or corrupt"; bench keeps going per file | `rawio.py`, `bench.py`, `__init__.py`, `tests/test_cli.py` |
| dng-sibling-collision | decoded `x.dng` beside raw `x.*` ignored when encoding a directory; duplicates are per-file errors; case-folded keys on macOS/Windows | `cli.py`, `tests/test_cli.py` |
| verify-wrong-original-not-error | original checked first (mosaic sha256); `OriginalMismatchError`, exit 1, unless `--force` | `pipeline.py`, `cli.py`, `tests/test_cli.py` |
| api-unknown-keys-silent | `resolve()` rejects unknown keys with a suggestion; experimental `gat4_K` allowlisted | `presets.py`, `tests/test_presets.py` |
| readme-misleading | correct test commands; requirements, usage, presets, exit codes | `README.md` |

## Known issues / TODO

- **half3 fails the strict 6.4 criteria on 2 of 3 low-ISO samples.** PANA9831 (+3EV ss2 76.8, ba p3 1.23) and P1060384 (77.2, 1.08) are below the vl/half3 thresholds (79 / 1.0), although 4x/+3EV crops show no visible difference. Consequence: `encode --verify` falls back to nlq on them and the files grow 2–3.4x (8.55 → 16.6 MB, 2.34 → 8.0 MB). The half3 criteria and the quick-verify fallback need re-evaluation.
- **nlq vl sits on the edge of its 6.4 criteria.** RMSE/σ 0.325–0.328 at ISO100 (limit 0.32), noise ratio 1.054–1.066 on several mid/high-ISO files (limit 1.05), |bias8| 0.647 on PANA0003 (limit 0.5). These are at the theoretical floor (sqrt(1 + 1/12) = 1.041) and look too tight; `encode --verify` reports `verify-failed` (exit 2) for such files although nothing is wrong visually.
- **Lens distortion:** the Panasonic in-camera distortion correction is not converted to a DNG `OpcodeList3`/WarpRectilinear opcode. Adobe/Apple renderers therefore show the uncorrected lens geometry (edge shifts of ~20–45 px reported on the samples); LibRaw/darktable ignore it either way. The parameters are kept in XMP and the .rsq META. Converting the Panasonic polynomial model is TODO.
- **Gamut guard tolerance:** up to 1e-6 of the half-resolution sites (~6 per 24 MP) may keep a clamped colour; noise-driven negative mixes at high ISO can need large blends (α ≈ 0.4–0.6), which does not matter while those files go to nlq.
- **gat4 is not quality-validated.** The full-file AHD + butteraugli check is still needed before it can go into `auto` (Q3).
- **R8:** rawpy does not expose LibRaw's 2-D cblack pattern, so the lossy refusal for such cameras cannot be implemented. Non-Bayer CFAs are refused for lossy (`EncodeError`) and encoded positionally for lossless.
- **Noise estimator:** on most mid/high-ISO files `s2` is clamped to its floor 0.25 (read noise not identified); the effect on SNR18 and rate is negligible.
- **Partial checks:**
  - The quick verify measures +3EV only, so the "0EV ss2 ≥ 84" criterion for vl/half3 is reported as "not requested" there. A full `rawsqueeze verify` checks it.
  - The NoiseProfile does not model half3/gat4 error.
  - The exif-only META fallback and non-RW2 inputs were only exercised with DNG fixtures. No real CR3/NEF/ARW files were tested.
  - darktable itself is not installed here; rawspeed compatibility of the new LJ92 layout is checked structurally (predictor, frame vs tile) plus three independent decoders (LibRaw, imagecodecs, Apple ImageIO).
- **Interface:** `--dry-run` does not decode the raw, so it reports the requested engine, not the one `auto` would select. Two parallel full `verify` runs on 24 MP files can exceed memory on small machines (one calibrator process exited silently; not reproduced).

## Tests

- `uv run pytest -q`: 424 passed in 91 s, including 41 `slow` tests that need `samples/` (after the review fixes; was 400).
- `uv run pytest -q -m "not slow"`: 383 passed in 11 s (includes a real-process Ctrl-C test, ~4 s).
- `uv run pytest -q -m slow`: 41 passed. This includes `tests/test_presets_regression.py`, which checks sizes within ±10 % of the DESIGN references for 2 files × 4 presets, lossless sha256, and the 6.4 acceptance criteria.
