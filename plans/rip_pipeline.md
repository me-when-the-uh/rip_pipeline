# rip_pipeline.py — Game Boy (and generic) soundtrack rip → combine → normalize pipeline

A single command that drives the whole workflow:

    rip (per-channel groups)  ->  combine groups (baseline gain)  ->  analyze peaks  ->  normalize to average peak

## Usage

    python rip_pipeline.py PLAYLIST.m3u [-o OUTPUT_DIR] [--gameboy] [--dry-run] [-v]

* `PLAYLIST.m3u` — an m3u of `.vgm`/`.vgz` tracks (paths resolved relative to the playlist).
* `-o/--output` — root output dir (default: `<playlist_stem>_rip` next to the playlist).
* `--gameboy` — force the Game Boy preset even if the probe says otherwise.
* `--dry-run` — print the exact `vgm2wav-mute` / `ffmpeg` commands and stop before analysis.
* `-v/--verbose` — show the underlying tool output.

Requires `vgm2wav-mute.exe` (built from `libvgm-research`, SBOY-enabled) and `ffmpeg` on PATH
(or pass `--vgm2wav-mute` / `--ffmpeg`).

## Output layout

    OUTPUT_DIR/
      0-2/   <track>.wav     # group 0-2 (square+wave) ripped at volume 0.9
      3/     <track>.wav     # group 3 (noise) ripped at volume 0.72
      combined/ <track>.wav  # groups summed (each scaled by its baseline gain)
      final/    <track>.wav  # combined, normalized to the average peak

## Game Boy behaviour (auto-detected via probe)

* Two groups: `0-2` (channels 0,1,2) @ 0.9 and `3` (channel 3) @ 0.72.
* Ripped with `--core 0x13=SBOY` (SameBoy core) and the complement channels muted.
* Group `0-2` mutes channel 3; group `3` mutes channels 0,1,2.

## Generic (non-Game Boy) behaviour

Interactive menu: shows detected device(s), channel count and names. The user adds groups by:

* `s 2`        — single channel 2
* `s 0 1 2`    — several single channels
* `span 0-3`   — channel range 0..3
* `combo 0 1`  — a combined group (all summed)
* `combo 0-2`  — combined range

Each group gets a volume (default 1.0). `u` toggles muting of unmanaged devices.
`p` proceeds; `q` quits. Queued vs still-unmanaged channels are shown after each edit.

## Combine

ffmpeg `filter_complex` applies each group's baseline gain, then `amix=inputs=N` sums them.
Because `amix` divides by N, a trailing `volume=N` restores unity so the sum is a true addition
(not an average). Output is 16-bit PCM at the source rate/channels.

## Analyze & normalize

* Peak (dBFS) measured per combined track (ffmpeg `astats`; falls back to a manual WAV reader
  that also handles `WAVEFORMATEXTENSIBLE`, which the stdlib `wave` module cannot open).
* Average peak = mean of all combined peaks → this is the target.
* Per track: `gain = target - peak`.
  * `|gain| <= 0.6 dB`  → left unchanged (within ±0.6 dB of average).
  * `|gain| > 3.5 dB`   → capped at ±3.5 dB and flagged as an **outlier** (only boosted/attenuated
    by 3.5 dB, never more).
  * otherwise           → exact `gain` applied.
* RMS is ignored.
* `final/<track>.wav` is written with `volume=gain` (linear).

## Report

Per-track table (peak / gain / outlier flag), a separate **Outliers** section listing any capped
tracks with their original peak and applied gain, the average peak (target), and an optional
verification that every final peak is within ±0.6 dB of the target.

## Resuming from a step (--step)

The pipeline can be started at any stage so you don't have to re-rip when all you
want is to re-level the audio:

* `--step rip` (default) — full pipeline from the `.m3u` playlist.
* `--step combine` — skip ripping; combine the already-ripped group folders
  (`0-2/`, `3/`, …) under the output dir, then normalize. The group layout is
  read from `rip_pipeline.state.json` (written during `rip`); if absent, groups are
  derived from the subdirectories (`--gameboy` assigns the 0.9 / 0.72 baseline
  volumes by folder name).
* `--step normalize` — skip rip **and** combine; just analyze + normalize a folder
  of WAVs to a common peak. Point it at manually-ripped tracks with
  `--input-dir DIR` (defaults to `<output>/combined` if present, else `<output>`).
  This is what you want when you ripped the music by hand and only need the peaks
  levelled to the same dBFS across tracks (outlier-aware, ±3.5 dB cap).

`--dry-run` prints the exact commands for whichever step(s) would run.

## Implementation notes / fixes made along the way

* `vgm2wav-mute.cpp`: added `--core CHIP=CORE` support (e.g. `0x13=SBOY`) and fixed its parser
  to split on `=` first (previously `0x13=SBOY` was mis-parsed as instance 0x13 → "device not
  present"). Built into `libvgm-research/build-mute-sboy`.
* vgm2wav-mute emits `WAVEFORMATEXTENSIBLE` WAVs, so the Python side never uses the stdlib `wave`
  module for peaks; it uses ffmpeg `astats` (with a manual extensible-WAV fallback).
* Subprocess calls always capture output with `errors="replace"` and Python stdout/stderr are
  reconfigured to UTF-8, so non-ASCII VGM tags (e.g. Japanese) don't crash the run.
