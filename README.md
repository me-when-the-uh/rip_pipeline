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
