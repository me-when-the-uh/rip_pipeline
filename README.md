# rip_pipeline

A coherent Game Boy (and general libvgm) soundtrack ripping pipeline:

**rip individual channels → combine matching WAVs → analyze peak loudness →
normalize to a common dBFS level**, with the ability to resume from any step.

It wraps [`vgm2wav-mute`](../vgm2wav-mute) (a per-instance channel-muting
`vgm2wav` fork) and `ffmpeg` to produce level-matched rips.

## Repository layout

```
rip_pipeline/
├── rip_pipeline.py          # the pipeline (rip / combine / normalize steps)
├── combine_matching_wavs.py# standalone analysis/mix helper
├── vgm2wav-mute/           # git submodule -> the C++ ripper (see below)
├── vgm2wav-mute.exe        # prebuilt ripper (so the pipeline runs out of the box)
├── libiconv.dll            # runtime dependency of the prebuilt exe
├── build_vgm2wav_mute.ps1  # rebuild the submodule and refresh vgm2wav-mute.exe
├── plans/rip_pipeline.md   # design notes
└── .gitignore
```

`vgm2wav-mute` is a **git submodule**. To rebuild from source (instead of using
the bundled `.exe`):

```powershell
git submodule update --init --recursive
.\build_vgm2wav_mute.ps1
```

## Requirements

- Python 3.8+
- `ffmpeg` on PATH (or pass `--ffmpeg PATH`)
- `vgm2wav-mute` — bundled as `vgm2wav-mute.exe`, or built from the submodule

## Quick start

```text
# Full pipeline from an .m3u playlist (rips, combines, normalizes):
python rip_pipeline.py path/to/playlist.m3u

# Resume from a specific step:
python rip_pipeline.py playlist.m3u --step combine     # skip re-ripping
python rip_pipeline.py playlist.m3u --step normalize   # only level peaks
python rip_pipeline.py playlist.m3u --dry-run          # print commands, do nothing

# Normalize a folder of already-ripped WAVs to a common peak:
python rip_pipeline.py --step normalize --input-dir my_tracks
```

### Interactive menu (no arguments)

Running the script with no playlist and no `--step` opens a short menu so you can
pick where to start:

```text
No playlist provided. Choose what to do:
  1) Analyse audio only              - report peak loudness, make no changes
  2) Analyse + tweak combined audio  - normalize WAVs next to this script into final/
  3) Analyse and tweak (full)        - combine channels + normalize into final/
  4) Rip from a playlist             - you'll be asked for the .m3u path
Choice [1/2/3/4]:
```

- **Analyse only** runs the normalize step in report-only mode: it measures each
  WAV's peak (first/last 1 s trimmed) and prints the average peak and any
  outliers, but writes nothing.
- **Analyse + tweak combined audio** normalizes WAVs that are already combined
  and sitting next to the script (top-level only; subdirectories such as `0-2/`,
  `3/`, `combined/`, `final/` are ignored). Results are written into `final/`
  next to the script. Use this when you already have combined tracks and just
  want level matching without re-combining.
- **Analyse and tweak (full)** runs the full pipeline from the combine step
  (combine channels, then normalize) and writes results into `final/`. It
  operates on the current directory, so place your ripped group folders (e.g.
  `0-2/`, `3/`) there first, or pass `-o/--output`.
- **Rip from a playlist** falls back to the normal rip flow and prompts for the
  `.m3u` path.

### Game Boy specifics

Channels 0-2 (square + wave) are ripped at volume `0.9` into `0-2/`, and the
noise channel (3) is ripped separately at `0.72` into `3/`. The pipeline forces
the SBOY core for Game Boy: `--core 0x13=SBOY`.

### Peak analysis notes

- The first and last 1 second of each track are excluded from peak analysis
  (to avoid edge pops).
- Tracks are normalized to the average peak, within a ±0.6 dB window.
- Outliers (quiet tracks >3.5 dB off) are boosted by at most 3.5 dB and listed
  separately in the report.

See [`plans/rip_pipeline.md`](plans/rip_pipeline.md) for the full design.
