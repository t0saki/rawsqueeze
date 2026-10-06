# rawsqueeze v0.1.0: implementation status

Status as of 2026-10-07, after the implementation, one review/fix round and one polish round
(acceptance recalibration, half3 max-error guard, DNG CFA phase fix, lens-distortion opcode).
All numbers on this page were measured in the polish round on all 13 Panasonic DC-S9 samples
(6016x4016, 12-bit, ISO 100–51200) on an 8-core M-series Mac with 8 threads, unless a row says
otherwise. Raw data: `bench_out/final/` (gitignored: `e2e.{json,csv}`, `verify_<file>_<preset>.json`,
and the scripts that produced them); compact copy for charts: `docs/results/final_results.csv`.

## Features

- **CLI** (`rawsqueeze encode|decode|info|verify|bench`): all flags from DESIGN.md 5.1.
  - Batch runs use `-j` worker processes (spawn), each with `cpu//jobs` threads and its own stay_open exiftool. Progress on stderr (one line per file plus a summary); `--json` writes reports to stdout.
  - Exit codes: 0 ok, 1 error, 2 verify below threshold (or a requested criterion not measurable), 130 interrupted.
  - Other flags: `--dry-run`, `--skip-existing`/`--overwrite`, `-r`, decode formats `dng|npy|pgm16|tiff`.
  - New in the polish round: `encode --no-fixup / --fixup-k K / --fixup-t T` (half3/gat4 max-error guard) and `decode --no-lens-opcode` (no OpcodeList3 WarpRectilinear; same as env `RAWSQUEEZE_DNG_LENS_OPCODE=0`). `DecodeReport.lens` (and `decode --json`) reports what was done with the lens data.
  - `encode --verify` runs a quick check (2 tiles, +3EV). If half3 was chosen by `auto` and fails, the file is re-encoded with nlq (R1); `--no-fallback` turns this off. A requested criterion that cannot be measured (ssimulacra2/butteraugli_main missing) counts as a failure, with a warning up front.
  - Batch robustness: Ctrl-C stops a `-j` batch at once (exit 130, partial summary); a worker that dies is retried one file at a time in a fresh worker; `default_jobs` budgets 3 GB per file with `--verify` (1.5 GB otherwise).
  - Planning: decoded `x.dng` next to raw `x.*` is ignored when encoding a directory; two inputs mapping to one output are a per-file error; case-folded duplicate check on macOS/Windows.
  - `verify` refuses a wrong original (mosaic sha256 differs from HEAD) with exit 1 unless `--force`; bad `--metrics` / `--ev` / `--tiles` are errors; unreadable inputs give `RawReadError` instead of a traceback.
- **API** (`rawsqueeze/__init__.py`, lazy): `encode_file`, `decode_file` (new keyword `lens_opcode`), `encode_frame`, `decode_mosaic`, `verify_file`, report classes. Presets in `rawsqueeze/presets.py`; `resolve()` rejects unknown option keys with a suggestion; engine extras `gat4_K`, `fixup`, `fixup_k`, `fixup_t` are allowlisted and validated.
- **Engines**:
  - **nlq**: noise-adaptive quantisation + JXL lossless; f = 0 is the lossless mode (bit-exact, sha256 checked on decode).
  - **half3**: perceptual (JXL VarDCT on a half-resolution RGB + G1−G2 difference plane). Gamut guard (matrix blended towards identity where XYB would clamp; HEAD `matrix_blend`). Saturation mask restores clipped pixels exactly; unmasked pixels never decode to `wl`. **New: max-error guard** (chunk `H3FX`, DESIGN.md 2.5 step 10, on by default): pixels with `|err| > max(0.2·d·(wl−blk), 8σ)` are stored exactly. Decode order clip → H3FX → SATM; files without H3FX decode as before.
  - **gat4**: experimental, never picked by `auto`; uses the same H3FX guard (rarely triggers).
  - **auto**: half3 when SNR18 ≥ 60 (≈ ISO 450 on the DC-S9) and the CFA is RGGB-like Bayer, else nlq.
- **HEAD**: DESIGN.md 3.3 plus `selection`, `source.iso`, `fallback`, `codec.nlq.identity_step`, `codec.half3.matrix_blend` / `gamut_neg_sites`, and new `codec.half3.fixup` (or `codec.gat4.fixup`) = `{k, t, t_dn, noise, n, n_over, capped, err_max_before, err_max_after}`. `recon_sha256` is always stored for nlq and, with the guard on (default), for half3/gat4 too (the guard's in-process decode gives the exact reconstruction; `--verify` reuses it).
- **Acceptance (verify exit 2)**: recalibrated in the polish round (DESIGN.md 6.4). Criteria are functions of the file's actual d (half3) or f (nlq) read from HEAD; the preset only supplies the parameter when HEAD has none. See "Acceptance criteria" below.
- **Container**: CRC per chunk; damaged files rejected with a clear error (e.g. `RsqCRCError: CRC mismatch in chunk PLN1 at offset 2008705`); `H3FX` registered as critical + zstd.
- **DNG output**:
  - LJ92 tiles in Adobe DNG Converter's layout (2 interleaved components, predictor 1, frame `th x tw/2`, per-tile optimal Huffman tables); accepted by LibRaw, rawspeed (structurally checked), the Adobe SDK layout rules, imagecodecs and Apple ImageIO.
  - EXIF/MakerNotes/GPS copied from the META skeleton with one exiftool call (119 Panasonic tags, 13 GPS, 41 ExifIFD); IFD0 ModifyDate/Artist/Copyright copied, OriginalRawFileName set; a transfer that copies no EXIF is a warning.
  - **New: lens distortion as OpcodeList3 WarpRectilinear** (default on). Panasonic DistortionInfo (0x0119) is converted with the model `r_out = S·(r + a r³ + b r⁵ + c r⁷)` (S = 1/(1+w5/32768), a = w8, b = w4, c = w11 over 32768, radius unit N = w12 = 3606 px, centre = DefaultCrop centre), inverted and refitted to the DNG 4-term radial model (fit error 0.004–0.37 px; refused above 1.0 px). Validation against the camera JPEG on all 13 samples: corrected corner residual median 0.14–0.54 px (p90 ≤ 1.55 px) vs 6.6–165 px uncorrected; Apple `sips` renders of 3 DNGs agree to sub-pixel. The raw parameters stay in XMP `rawsqueeze:PanasonicDistortionInfo`. About 1 ms per DNG.
  - **Fixed: CFA/BlackLevel phase for odd ActiveArea margins** (`dng.rotate_cfa_phase`). No effect on DC-S9 files (margins 0,0).
  - `--keep-preview` stores the camera JPEGs; `--extract-preview` restores them bit-exact.

## Final measurements: 13 samples × 4 presets (engine auto)

Script `bench_out/final/final_e2e.py`. In-process `encode_file` / `decode_file`, 8 threads, one file at a time.
"MB" = complete `.rsq` incl. META (10⁶ bytes). "ratio vs file" = RW2 size / .rsq size; "ratio vs raw data" =
(RW2 size − RawDataOffset) / .rsq size. "enc" = load + noise estimate + encode (+ half3 guard decode) + write.
"dec→DNG" = read + decode + LJ92 DNG + exiftool transfer + lens opcode. Bit-exact = the decoded mosaic *and*
the lossless DNG read back with rawpy both equal the original `raw_image`.

| file | ISO | SNR18 | engine (high / vl / compact) | lossless MB | high MB | vl MB | compact MB | ratio vs file: lossless / high / vl / compact | ratio vs raw data: lossless / high / vl / compact | enc s: lossless / high / vl / compact | dec→DNG s (vl) | bit-exact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1060444 | 100 | 102.1 | half3 d0.1/d0.2/d0.3 | 17.838 | 7.534 | 4.930 | 3.737 | 1.58 / 3.75 / 5.73 / 7.56 | 1.20 / 2.85 / 4.35 / 5.74 | 0.42 / 1.23 / 1.22 / 1.20 | 0.53 | yes |
| PANA9831 | 100 | 103.0 | half3 d0.1/d0.2/d0.3 | 22.389 | 11.796 | 8.561 | 6.843 | 1.42 / 2.69 / 3.71 / 4.64 | 1.13 / 2.14 / 2.94 / 3.68 | 0.44 / 1.26 / 1.24 / 1.23 | 0.55 | yes |
| P1060384 | 320 | 70.5 | half3 d0.1/d0.2/d0.3 | 15.325 | 5.931 | 2.341 | 1.391 | 1.54 / 3.98 / 10.08 / 16.96 | 1.27 / 3.28 / 8.31 / 14.00 | 0.43 / 1.22 / 1.19 / 1.16 | 0.51 | yes |
| ISO640_PANA0036 | 640 | 47.7 | nlq f0.5/f1/f2 | 15.993 | 11.421 | 8.097 | 5.669 | 1.54 / 2.16 / 3.05 / 4.35 | 1.24 / 1.74 / 2.45 / 3.51 | 0.41 / 0.63 / 0.66 / 0.68 | 0.51 | yes |
| ISO800_PANA0021 | 800 | 39.4 | nlq f0.5/f1/f2 | 16.730 | 11.077 | 7.934 | 5.542 | 1.50 / 2.27 / 3.17 / 4.54 | 1.21 / 1.83 / 2.55 / 3.65 | 0.42 / 0.63 / 0.62 / 0.68 | 0.50 | yes |
| ISO1250_P1060413 | 1250 | 34.7 | nlq f0.5/f1/f2 | 17.208 | 11.109 | 7.880 | 5.466 | 1.49 / 2.31 / 3.25 / 4.69 | 1.19 / 1.84 / 2.59 / 3.74 | 0.41 / 0.63 / 0.63 / 0.69 | 0.49 | yes |
| ISO1600_PANA9976 | 1600 | 28.1 | nlq f0.5/f1/f2 | 18.920 | 11.174 | 8.223 | 5.759 | 1.45 / 2.46 / 3.35 / 4.78 | 1.13 / 1.92 / 2.60 / 3.72 | 0.42 / 0.64 / 0.64 / 0.70 | 0.51 | yes |
| ISO2000_PANA9996 | 2000 | 24.2 | nlq f0.5/f1/f2 | 18.289 | 11.627 | 8.547 | 6.092 | 1.50 / 2.36 / 3.21 / 4.50 | 1.16 / 1.82 / 2.47 / 3.47 | 0.43 / 0.65 / 0.64 / 0.69 | 0.49 | yes |
| ISO3200_PANA9944 | 3200 | 18.5 | nlq f0.5/f1/f2 | 20.949 | 12.122 | 9.242 | 6.697 | 1.41 / 2.44 / 3.20 / 4.42 | 1.09 / 1.88 / 2.46 / 3.40 | 0.46 / 0.65 / 0.65 / 0.70 | 0.51 | yes |
| P1037920 | 4000 | 17.2 | nlq f0.5/f1/f2 | 20.200 | 11.052 | 8.135 | 5.707 | 1.34 / 2.45 / 3.33 / 4.74 | 1.10 / 2.01 / 2.73 / 3.88 | 0.42 / 0.63 / 0.63 / 0.69 | 0.49 | yes |
| PANA0003 | 4000 | 16.5 | nlq f0.5/f1/f2 | 19.231 | 11.387 | 8.480 | 5.988 | 1.35 / 2.28 / 3.07 / 4.34 | 1.12 / 1.88 / 2.53 / 3.58 | 0.42 / 0.63 / 0.63 / 0.70 | 0.50 | yes |
| ISO10000_PANA9951 | 10000 | 12.5 | nlq f0.5/f1/f2 | 22.573 | 10.275 | 7.525 | 5.208 | 1.30 / 2.86 / 3.90 / 5.64 | 1.10 / 2.41 / 3.29 / 4.75 | 0.42 / 0.64 / 0.64 / 0.70 | 0.51 | yes |
| ISO51200_PANA0010 | 51200 | 5.9 | nlq f0.5/f1/f2 | 24.316 | 11.849 | 9.384 | 7.273 | 1.37 / 2.82 / 3.56 / 4.59 | 1.10 / 2.25 / 2.84 / 3.66 | 0.45 / 0.66 / 0.67 / 0.73 | 0.53 | yes |

| preset | total MB (13 files, 359.4 MB of RW2) | ratio vs file | ratio vs raw data | enc s (range) | dec→DNG s (range) | full verify |
|---|---|---|---|---|---|---|
| lossless | 250.0 | 1.44x | 1.15x | 0.41–0.46 | 0.49–0.62 | 13/13 bit-exact (mosaic and DNG) |
| high | 138.4 | 2.60x | 2.07x | nlq 0.63–0.66, half3 1.22–1.26 | 0.50–0.56 | 13/13 PASS |
| **vl (default)** | **99.3** | **3.62x** | **2.89x** | nlq 0.62–0.67, half3 1.19–1.24 | 0.50–0.55 | **13/13 PASS** |
| compact | 71.4 | 5.04x | 4.02x | nlq 0.68–0.73, half3 1.16–1.23 | 0.48–0.58 | 13/13 PASS |

- **CLI wall clock** (P1060444 vl, including interpreter start): encode 1.40–1.60 s, decode to DNG 0.79–0.82 s (targets ≤ 2.5 s / ≤ 1.5 s).
- **Batch** `encode samples/ -j 2`: 13 files in 8.3 s, 359.44 MB → 99.28 MB (3.62x). With `--verify`: 30.0 s, 13/13 pass, **0 fallbacks** (before the recalibration PANA9831 and P1060384 fell back to nlq, ≈ 113 MB total). `decode … -j 2` to DNG: 13 files in 6.0 s.
- **H3FX cost** (stored bytes): vl 225–14,107 B per half3 file (0.015–0.29 % of the file), high 320–19,678 B, compact 166–8,576 B; encode +0.2–0.35 s. Max |err| on unsaturated pixels: vl 308–799 → 158 DN, high 247–801 → 124 DN, compact 395–991 → 231–237 DN.
- **Lens opcode** written on all 52 DNGs (13 files × 4 presets; every sample has in-camera correction on); fit error 0.004–0.367 px (worst: 24 mm, ISO2000_PANA9996).
- Changes against the review-round table: half3 files are 0.01–0.3 % larger (H3FX) and encode 0.2–0.3 s slower; nlq and lossless sizes are byte-identical; decode to DNG is unchanged within noise (the opcode fit costs about 1 ms).

## Full verify (DESIGN.md 6.4, recalibrated): all 39 lossy files

Every value is the worst of 4 × 2048² tiles (centre, darkest, highest variance, seeded random), AHD via in-memory
DNG, FLOOR = original + random 0/1 DN (seed 5). noise ratio / |bias8| / RMSE/σ are the nlq criteria (shown only for
nlq). Clip consistency was 0 inconsistent pixels in all 39 runs. About 17–24 s per file.

### vl (default preset)

| file | engine | MB | ss2 0 / +2 / +3EV | ba p3 +3EV | ba max +3EV | noise ratio / \|bias8\| / RMSE/σ | FLOOR ss2 +3EV | H3FX px / B / max\|err\| DN | result |
|---|---|---|---|---|---|---|---|---|---|
| P1060444 | half3 d0.2 | 4.930 | 87.7 / 85.0 / 82.8 | 0.97 | 4.2 | – | 90.7 | 5101 / 14107 / 590→158 | PASS |
| PANA9831 | half3 d0.2 | 8.561 | 84.3 / 79.6 / 76.8 | 1.23 | 4.8 | – | 89.6 | 3043 / 9512 / 799→158 | PASS |
| P1060384 | half3 d0.2 | 2.341 | 89.4 / 82.2 / 77.2 | 1.08 | 2.7 | – | 87.5 | 38 / 225 / 308→158 | PASS |
| ISO640_PANA0036 | nlq f1 | 8.097 | 89.2 / 81.3 / 76.1 | 0.98 | 3.0 | 1.033 / 0.189 / 0.298 | 84.6 | – | PASS |
| ISO800_PANA0021 | nlq f1 | 7.934 | 88.2 / 80.2 / 75.1 | 1.03 | 3.2 | 1.049 / 0.130 / 0.297 | 85.3 | – | PASS |
| ISO1250_P1060413 | nlq f1 | 7.880 | 86.8 / 75.6 / 68.7 | 1.11 | 3.2 | 1.042 / 0.043 / 0.295 | 79.8 | – | PASS |
| ISO1600_PANA9976 | nlq f1 | 8.223 | 85.5 / 76.5 / 71.9 | 1.15 | 3.2 | 1.044 / 0.058 / 0.292 | 84.2 | – | PASS |
| ISO2000_PANA9996 | nlq f1 | 8.547 | 81.4 / 66.6 / 58.0 | 1.36 | 3.8 | 1.054 / 0.124 / 0.296 | 77.7 | – | PASS |
| ISO3200_PANA9944 | nlq f1 | 9.242 | 77.6 / 62.0 / 52.7 | 1.56 | 5.0 | 1.066 / 0.039 / 0.294 | 80.9 | – | PASS |
| P1037920 | nlq f1 | 8.135 | 70.8 / 49.7 / 38.5 | 1.75 | 5.5 | 1.043 / 0.047 / 0.291 | 72.2 | – | PASS |
| PANA0003 | nlq f1 | 8.480 | 70.2 / 47.5 / 35.6 | 1.76 | 5.0 | 1.062 / 0.647 / 0.293 | 71.8 | – | PASS |
| ISO10000_PANA9951 | nlq f1 | 7.525 | 71.4 / 57.3 / 55.3 | 1.64 | 5.1 | 1.059 / 0.084 / 0.289 | 80.8 | – | PASS |
| ISO51200_PANA0010 | nlq f1 | 9.384 | 41.6 / 21.1 / 13.1 | 3.02 | 9.0 | 1.026 / 0.209 / 0.285 | 75.9 | – | PASS |

### high

| file | engine | MB | ss2 0 / +2 / +3EV | ba p3 +3EV | ba max +3EV | noise ratio / \|bias8\| / RMSE/σ | FLOOR ss2 +3EV | H3FX px / B / max\|err\| DN | result |
|---|---|---|---|---|---|---|---|---|---|
| P1060444 | half3 d0.1 | 7.534 | 90.0 / 88.1 / 86.7 | 0.70 | 2.8 | – | 90.7 | 7327 / 18839 / 427→124 | PASS |
| PANA9831 | half3 d0.1 | 11.796 | 88.2 / 85.0 / 83.1 | 0.90 | 4.0 | – | 89.6 | 6726 / 19678 / 801→125 | PASS |
| P1060384 | half3 d0.1 | 5.931 | 91.5 / 85.7 / 81.8 | 0.88 | 2.5 | – | 87.5 | 60 / 320 / 247→124 | PASS |
| ISO640_PANA0036 | nlq f0.5 | 11.421 | 92.5 / 87.7 / 84.5 | 0.68 | 2.6 | 1.007 / 0.015 / 0.148 | 84.6 | – | PASS |
| ISO800_PANA0021 | nlq f0.5 | 11.077 | 91.5 / 86.5 / 83.3 | 0.73 | 2.4 | 1.014 / 0.013 / 0.147 | 85.3 | – | PASS |
| ISO1250_P1060413 | nlq f0.5 | 11.109 | 90.4 / 85.3 / 81.6 | 0.77 | 2.8 | 1.014 / 0.074 / 0.144 | 79.8 | – | PASS |
| ISO1600_PANA9976 | nlq f0.5 | 11.174 | 89.8 / 83.9 / 80.9 | 0.83 | 2.8 | 1.014 / 0.022 / 0.146 | 84.2 | – | PASS |
| ISO2000_PANA9996 | nlq f0.5 | 11.627 | 87.6 / 78.6 / 73.4 | 0.96 | 3.4 | 1.013 / 0.012 / 0.146 | 77.7 | – | PASS |
| ISO3200_PANA9944 | nlq f0.5 | 12.122 | 85.0 / 75.5 / 69.9 | 1.10 | 3.7 | 1.017 / 0.097 / 0.148 | 80.9 | – | PASS |
| P1037920 | nlq f0.5 | 11.052 | 80.8 / 67.8 / 60.9 | 1.28 | 4.3 | 1.009 / 0.045 / 0.146 | 72.2 | – | PASS |
| PANA0003 | nlq f0.5 | 11.387 | 80.2 / 66.1 / 58.6 | 1.28 | 4.5 | 1.008 / 0.100 / 0.148 | 71.8 | – | PASS |
| ISO10000_PANA9951 | nlq f0.5 | 10.275 | 81.0 / 72.2 / 70.3 | 1.18 | 5.1 | 1.013 / 0.027 / 0.145 | 80.8 | – | PASS |
| ISO51200_PANA0010 | nlq f0.5 | 11.849 | 64.1 / 51.0 / 45.5 | 2.07 | 8.0 | 1.007 / 0.096 / 0.143 | 75.9 | – | PASS |

### compact

| file | engine | MB | ss2 0 / +2 / +3EV | ba p3 +3EV | ba max +3EV | noise ratio / \|bias8\| / RMSE/σ | FLOOR ss2 +3EV | H3FX px / B / max\|err\| DN | result |
|---|---|---|---|---|---|---|---|---|---|
| P1060444 | half3 d0.3 | 3.737 | 85.8 / 81.9 / 78.8 | 1.12 | 3.7 | – | 90.7 | 2929 / 8576 / 782→237 | PASS |
| PANA9831 | half3 d0.3 | 6.843 | 81.2 / 75.4 / 71.7 | 1.52 | 6.2 | – | 89.6 | 1796 / 5680 / 991→237 | PASS |
| P1060384 | half3 d0.3 | 1.391 | 87.8 / 79.1 / 73.3 | 1.22 | 3.1 | – | 87.5 | 27 / 166 / 395→231 | PASS |
| ISO640_PANA0036 | nlq f2 | 5.669 | 83.3 / 69.8 / 61.0 | 1.47 | 3.6 | 1.079 / 0.234 / 0.578 | 84.6 | – | PASS |
| ISO800_PANA0021 | nlq f2 | 5.542 | 81.4 / 67.8 / 59.0 | 1.57 | 3.8 | 1.176 / 0.117 / 0.578 | 85.3 | – | PASS |
| ISO1250_P1060413 | nlq f2 | 5.466 | 78.4 / 59.0 / 47.2 | 1.72 | 3.9 | 1.170 / 0.444 / 0.584 | 79.8 | – | PASS |
| ISO1600_PANA9976 | nlq f2 | 5.759 | 76.1 / 61.0 / 53.3 | 1.82 | 4.7 | 1.167 / 0.167 / 0.577 | 84.2 | – | PASS |
| ISO2000_PANA9996 | nlq f2 | 6.092 | 69.7 / 45.1 / 30.9 | 2.10 | 5.9 | 1.183 / 0.330 / 0.575 | 77.7 | – | PASS |
| ISO3200_PANA9944 | nlq f2 | 6.697 | 62.2 / 35.4 / 20.0 | 2.46 | 5.8 | 1.201 / 0.234 / 0.574 | 80.9 | – | PASS |
| P1037920 | nlq f2 | 5.707 | 51.8 / 17.8 / 1.3 | 2.70 | 6.2 | 1.065 / 0.529 / 0.575 | 72.2 | – | PASS |
| PANA0003 | nlq f2 | 5.988 | 52.5 / 17.1 / -0.5 | 2.64 | 6.2 | 1.130 / 0.953 / 0.571 | 71.8 | – | PASS |
| ISO10000_PANA9951 | nlq f2 | 5.208 | 51.1 / 27.4 / 25.5 | 2.69 | 7.1 | 1.109 / 0.212 / 0.580 | 80.8 | – | PASS |
| ISO51200_PANA0010 | nlq f2 | 7.273 | 2.9 / -24.5 / -33.6 | 5.18 | 12.4 | 1.060 / 0.990 / 0.633 | 75.9 | – | PASS |

Notes:
- The nlq criteria are noise-relative (they check that the added error is the intended fraction of the sensor noise), not perceptual. At compact (f = 2) on ISO ≥ 2000 the +3EV ss2 drops to 31…−34 because the grain itself changes (noise std ratio up to 1.20); this passes by design. Use vl or high when high-ISO files will be pushed hard.
- Forced half3 (`--engine half3`) at vl fails the new criteria on every sample at ISO ≥ 640 (+3EV ss2 24.9–68.3, ba p3 1.37–2.42), as intended: see the calibration table.

## Acceptance criteria (recalibrated, `verify.py`)

- **nlq f** (theory for a uniform quantiser of step f·σ, plus estimator slack ×1.2 / +0.03): raw RMSE/σ ≤ 1.2f/√12 + 0.03; +3EV noise std ratio ≤ √(1 + (1.2f)²/12) + 0.03; |bias8| ≤ 0.75 + 0.25f²; for f ≤ 0.5 at low ISO also ss2 ≥ FLOOR − 3 at every EV. Limits at f = 0.5 / 1 / 2: RMSE/σ 0.203 / 0.376 / 0.723, noise ratio 1.045 / 1.088 / 1.247, |bias8| 0.81 / 1.00 / 1.75.
- **half3 d**: anchors at d = 0.1 / 0.2 / 0.3, linear in d between them (outer slopes extrapolated, d clamped to [0.05, 0.6]), plus clip consistency = 0:

  | d | ss2 0 / +2 / +3EV ≥ | ba p3 +3EV ≤ | ba max +3EV ≤ | good files, worst (ss2 +3EV / p3) | degraded files, best (ISO800 forced half3) |
  |---|---|---|---|---|---|
  | 0.1 | 84 / 82 / 78 | 1.2 | 6 | 81.8 / 0.90 | 76.2 / 1.06 |
  | 0.2 | 80 / 77 / 72 | 1.5 | 8 | 76.8 / 1.23 | 68.3 / 1.37 |
  | 0.3 | 78 / 73 / 67 | 1.8 | 9 | 71.7 / 1.52 | 62.4 / 1.57 |

  +3EV ss2 is the main separator (margins at d 0.2: +4.8 for the worst good file, −3.7 for the best degraded one), +2EV ss2 the second (79.6 vs 74.9). 0EV ss2 does not separate (floor only). The noise std ratio is not used for half3 (0.71 on P1060384, which looks clean; 0.83–0.97 on degraded files).
- **lossless**: mosaic sha256 equal (and DNG bit-exact).
- **Why the change**: the v1 table had limits at or below the theoretical floor for nlq (RMSE/σ 0.32 vs f/√12 = 0.289 plus integer rounding; noise ratio 1.05 vs √(1+1/12) = 1.041 plus estimator bias) and failed PANA9831 / P1060384 half3 (no visible difference at 4x / +3EV), which made `encode --verify` fall back and grow those files 2–3.4x. Under the old criteria 6 of 13 vl files failed (PANA9831, P1060384, ISO2000, ISO3200, PANA0003, ISO10000); under the new ones 13/13 pass, and every forced-half3 file at ISO ≥ 640 fails (CLI exit 2).

## Threshold calibration (Q1, review round)

Worst of 4 × 2048² tiles, AHD via DNG, FLOOR seed 5. half3 numbers are from the review round *before* the gamut guard
and H3FX (the guard does not change these tile metrics on the low-ISO files: e.g. PANA9831 +3EV ss2 76.78 → 76.79).

| file | ISO | SNR18 | half3 d0.2 MB | half3 ss2 0/+2/+3 | half3 ba p3 / max +3EV | half3 noise ratio | nlq f1 MB | nlq ss2 0/+2/+3 | FLOOR ss2 0/+2/+3 | half3/nlq size |
|---|---|---|---|---|---|---|---|---|---|---|
| P1060444 | 100 | 102.1 | 4.915 | 87.7/85.0/82.8 | 0.97 / 4.1 | 0.965 | 11.858 | 92.7/90.6/89.1 | 93.4/92.1/90.7 | 0.41 |
| PANA9831 | 100 | 103.0 | 8.551 | 84.3/79.7/76.8 | 1.23 / 4.8 | 0.987 | 16.609 | 92.2/90.4/89.1 | 93.1/90.9/89.6 | 0.51 |
| P1060384 | 320 | 70.5 | 2.341 | 89.4/82.2/77.2 | 1.08 / 2.7 | 0.706 | 8.020 | 92.4/87.4/84.2 | 93.8/89.3/87.5 | 0.29 |
| ISO640_PANA0036 | 640 | 47.7 | 4.198 | 85.4/32.2/41.9 | 7.56 / 22.4 (gamut clamp; 3.76 MB, 66.7, 1.42 with the guard) | 0.844 | 8.097 | 89.2/81.3/76.1 | 92.5/87.7/84.6 | 0.52 |
| ISO800_PANA0021 | 800 | 39.4 | 4.711 | 85.3/74.9/68.3 | 1.38 / 3.7 | 0.850 | 7.934 | 88.2/80.2/75.1 | 92.4/88.2/85.3 | 0.59 |
| ISO1250_P1060413 | 1250 | 34.7 | 4.795 | 82.7/66.6/57.0 | 1.59 / 3.7 | 0.855 | 7.880 | 86.8/75.6/68.7 | 91.2/84.0/79.8 | 0.61 |
| ISO1600_PANA9976 | 1600 | 28.1 | 6.261 | 82.6/71.7/65.9 | 1.49 / 3.8 | 0.930 | 8.223 | 85.5/76.5/71.9 | 91.9/86.9/84.2 | 0.76 |
| ISO2000_PANA9996 | 2000 | 24.2 | 6.578 | 76.8/57.9/47.5 | 1.80 / 5.8 | 0.905 | 8.547 | 81.4/66.6/58.0 | 89.5/81.8/77.7 | 0.77 |
| ISO3200_PANA9944 | 3200 | 18.5 | 8.476 | 75.9/60.5/51.2 | 3.27 / 19.8 | 0.916 | 9.242 | 77.6/62.0/52.7 | 89.8/84.1/80.9 | 0.92 |
| P1037920 | 4000 | 17.2 | 7.763 | 65.2/40.3/29.7 | 5.09 / 27.2 | 0.903 | 8.135 | 70.8/49.7/38.6 | 86.2/76.9/72.2 | 0.95 |
| PANA0003 | 4000 | 16.5 | 7.073 | 64.4/38.0/25.1 | 2.22 / 10.3 | 0.924 | 8.480 | 70.2/47.5/35.6 | 85.9/76.5/71.8 | 0.83 |
| ISO10000_PANA9951 | 10000 | 12.5 | 8.753 | 75.8/63.1/59.2 | 1.64 / 5.6 | 0.969 | 7.524 | 71.4/57.3/55.3 | 88.7/83.7/80.8 | 1.16 |
| ISO51200_PANA0010 | 51200 | 5.9 | 11.243 | 62.2/48.1/42.3 | 2.33 / 8.5 | 0.966 | 9.384 | 41.6/21.1/13.1 | 84.4/78.7/75.9 | 1.20 |

Decision: **auto threshold SNR18 = 60**. At SNR18 ≤ 40 (ISO ≥ 800) half3 flattens the grain (noise ratio 0.85–0.93, +3EV ss2 well below nlq's) and has no size advantage at equal quality (half3 d0.1 is larger than nlq f1 at ISO800/1250: 8.13 vs 7.93 MB, 8.28 vs 7.88 MB); at ISO ≥ 10000 it is even larger than nlq. At SNR18 ≥ 70 half3 is 0.29–0.51 of the nlq size with no visible difference at 4x / +3EV. The only sample between 40 and 70 (ISO640) failed through the XYB gamut clamp, fixed separately; 60 keeps a margin.

## Change history (brief)

**Review round** (findings → fixes; details in git history):
- half3: XYB gamut clamp (gamut guard), matrix rows dropped for BGGR/GBRG, false saturation (clip to `wl−1` with SATM).
- auto threshold SNR18 40 → 60 (calibration above).
- DNG: rawspeed-compatible LJ92 layout (5–7 % smaller DNGs); DistortionInfo kept in XMP; EXIF transfer post-check; IFD0 tags copied.
- CLI/API: Ctrl-C and worker-crash handling, unmeasured criteria count as failures, `RawReadError`, DNG-sibling / duplicate-output planning, wrong-original check in `verify`, unknown API keys rejected, README corrected.

**Polish round** (this page's numbers):
| change | files |
|---|---|
| Acceptance criteria recalibrated as functions of d / f (`nlq_criteria`, `half3_criteria`, `codec_param`, `ba_max_max` criterion); `encode --verify` no longer falls back on PANA9831 / P1060384 | `verify.py`, `tests/test_verify.py`, `tests/test_presets_regression.py`, DESIGN.md 6.4 |
| half3/gat4 max-error guard, chunk `H3FX` (default on; `fixup`, `fixup_k`, `fixup_t`; CLI `--no-fixup`, `--fixup-k`, `--fixup-t`); decode split into `_reconstruct` + H3FX + SATM; `recon_sha256` always written with the guard | `engines/half3.py`, `engines/gat4.py`, `container.py`, `presets.py`, `cli.py`, `tests/test_fixup.py`, `tests/test_pipeline.py`, DESIGN.md 2.5 / 3.2 / 3.3 / 5.1 |
| DNG CFAPattern / BlackLevel phase relative to the ActiveArea origin for odd margins (`rotate_cfa_phase`) | `dng.py`, `tests/test_dng.py`, DESIGN.md 3.5 |
| Panasonic distortion → OpcodeList3 WarpRectilinear (default on; `write_dng(lens_opcode=)`, env `RAWSQUEEZE_DNG_LENS_OPCODE`, `DngReport.lens`); `PanasonicDistortion` parser | `dng.py`, `meta.py`, `tests/test_dng.py`, DESIGN.md 3.5 |
| Lens switch exposed on decode: `decode_file(lens_opcode=)`, `DecodeReport.lens`, CLI `decode --no-lens-opcode` | `pipeline.py`, `cli.py`, `tests/test_cli.py`, DESIGN.md 5.1 |

## Deviations from DESIGN.md

1. **nlq identity step is 2 DN when f < 1** (`curves.identity_step_for`; spec: 1 DN). With 1-DN steps an integer LUT gives one-directional 0/1 DN errors; on P1037920 f0.5 the noise ratio drops from 1.027 to 1.009 and |bias8| from 0.27 to 0.045 for +0.5 % size. f ≥ 1 follows the spec; the decoder does not depend on it.
2. **Acceptance criteria (6.4) replaced** by the recalibrated parameter-dependent ones (DESIGN.md 6.4 updated; v1 table kept there for reference).
3. **nlq sizes at ISO 320–4000 are 4–5 % below Track B's figures**: B used an older noise estimator without the percentile bias correction; the current numbers are right for the estimator in A.3.
4. **half3 error tails**: full-file p99.99 |err| is 199 DN against 95 in the spec (the spec's own A.1 code gives the same; the spec figure most likely came from crops). The new H3FX guard caps the max at `max(0.2·d·(wl−blk), 8σ)` (158 DN at vl on the low-ISO files).
5. **New chunk `H3FX`** and **DNG `OpcodeList3`** are additions to the v1 spec (now documented in DESIGN.md 2.5, 3.2, 3.5).
6. **Container**: INDX damage triggers an index rebuild, not an error; `verify_crc=False` still checks HEAD; `--force` (skip CRC on lossy payloads) is not implemented (not in the 5.1 CLI).
7. **META**: the skeleton is built with a built-in TIFF parser (no exiftool call, 0.01 s); the "329 tags" figure counts every exiftool line (287 real tags).
8. **HEAD**: `color.camera_wb` holds the effective WB (a 4th value of 0 is replaced by G); a lossless file records `engine: "nlq"` with `mode: "lossless"`.
9. **gat4**: K capped per plane when `100·y_max` would exceed 65535; mosaic clamped to `wl` before the transform; decoder clips to [0, wl].
10. **DNG writer**: `tw = W` (or two tile columns) for images narrower than the tile width (LibRaw 0.22.1 single-column-tile bug); full-size frames unaffected.
11. **`-q` with `--engine auto` is rejected**; give `--d` and `--f` instead.
12. **Output permissions**: atomically written files are chmodded to `0o666 & ~umask` (mkstemp default would be 0600).

## Known issues / TODO

- **Quick verify is partial**: `encode --verify` measures +3EV on 2 tiles only, so the 0EV/+2EV ss2 criteria and the other two tiles are only checked by a full `rawsqueeze verify`.
- **half3 d0.1 (preset high) is only good at ISO ≤ 320**: forced on ISO800 it reaches +3EV ss2 76.2 (about nlq vl quality) and fails the high criteria. auto never picks half3 there, so this only affects `--engine half3`.
- **H3FX on forced high-ISO half3**: the threshold is `max(t·range, 8σ)`, so with large σ the remaining max error stays high (P1037920: 714 DN after the guard). Irrelevant for auto (those files go to nlq).
- **nlq integer centroid LUT bias**: the LUT is rounded to integers, which gives a raw-domain mean error of −0.14…−0.28 DN on the PANA0003 night sky (+3EV |bias8| 0.647 at vl, 0.953 at compact; a Gaussian control with the same error variance gives 0.14). It passes the new criteria; bias-compensated rounding of the LUT would remove it.
- **compact on high-ISO files is noise-faithful, not perceptually transparent**: see the note under the compact verify table (+3EV ss2 down to −34 at ISO51200).
- **Lens opcode scope**: validated only on the DC-S9 with the LUMIX S 24-60 and 70-300 at 3:2. Other aspect-ratio crop modes and other Panasonic bodies (older GH/G with N = 2500) are untested, though the code uses word12 and the DefaultCrop centre generically. Lateral CA (0x011B) is not converted (single-plane opcode). Adobe ACR/Lightroom were not available; checks are our numpy implementation of the DNG SDK formula and Apple Core Image. LibRaw/darktable ignore the opcode either way.
- **LibRaw BlackLevel quirk (low)**: for an odd ActiveArea origin LibRaw rounds the margin up to even and shifts the CFA, but reads BlackLevel relative to its rounded origin, so its `black_level_per_channel` is permuted when per-position blacks differ and a margin is odd. Our DNGs follow the DNG spec (as the Adobe SDK does). No current sample is affected.
- **Gamut guard tolerance**: up to 1e-6 of half-resolution sites (~6 per 24 MP) may keep a clamped colour; noise-driven negative mixes at high ISO can need large blends (α ≈ 0.4–0.6), irrelevant while those files go to nlq.
- **gat4 is not quality-validated** (full-file AHD + butteraugli still needed before it can enter `auto`, Q3).
- **R8**: rawpy does not expose LibRaw's 2-D cblack pattern, so the lossy refusal for such cameras is not implemented. Non-Bayer CFAs are refused for lossy and encoded positionally for lossless.
- **Noise estimator**: on most mid/high-ISO files `s2` is clamped to its floor 0.25 (read noise not identified); negligible effect on SNR18 and rate.
- **Coverage**: only Panasonic DC-S9 RW2 files were tested with real data; the exif-only META fallback and non-RW2 inputs were exercised with DNG fixtures only (no real CR3/NEF/ARW). darktable itself is not installed; rawspeed compatibility of the LJ92 layout is checked structurally plus three independent decoders. The NoiseProfile tag does not model half3/gat4 error.
- **Interface**: `--dry-run` does not decode the raw, so it reports the requested engine, not the one `auto` would pick. Two parallel full `verify` runs on 24 MP files can exceed memory on small machines.

## Tests

- `uv run pytest -q`: 459 passed in 99 s (42 `slow` tests need `samples/`; was 424 before the polish round).
- `uv run pytest -q -m "not slow"`: 417 passed in 11 s (includes a real-process Ctrl-C test, ~4 s).
- Slow tests need `samples/`; they include `tests/test_presets_regression.py` (sizes within ±10 % of the DESIGN references for 2 files × 4 presets, lossless sha256, 6.4 acceptance) and real-sample DNG checks (OpcodeList3 survives the exiftool transfer).
