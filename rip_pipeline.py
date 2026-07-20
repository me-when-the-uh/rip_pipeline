#!/usr/bin/env python3
"""
rip_pipeline.py - Game Boy (and generic) soundtrack ripping pipeline.

Full pipeline:
    .m3u  ->  probe first track  ->  rip channels into groups
         ->  combine groups (with per-group baseline gain)
         ->  analyze peaks  ->  normalize to the average peak (outlier-aware)

The pipeline can be started at any step with --step:
    rip        (default) run the whole thing from the playlist
    combine    skip ripping; combine already-ripped group dirs, then normalize
    normalize   skip rip + combine; just analyze + normalize a folder of WAVs
               (handy when you ripped the music manually and only want the
                peaks levelled to the same dBFS across tracks)

Usage:
    python rip_pipeline.py PLAYLIST.m3u
    python rip_pipeline.py PLAYLIST.m3u -o OUTPUT_DIR
    python rip_pipeline.py PLAYLIST.m3u --gameboy
    python rip_pipeline.py PLAYLIST.m3u --dry-run -v
    python rip_pipeline.py PLAYLIST.m3u --vgm2wav-mute path/to/vgm2wav-mute.exe --ffmpeg path/to/ffmpeg.exe

    # Resume from a later step (no re-rip needed):
    python rip_pipeline.py -o OUTPUT_DIR --step combine
    python rip_pipeline.py -o OUTPUT_DIR --step combine --gameboy
    # Level a folder of manually-ripped WAVs to a common peak:
    python rip_pipeline.py -o OUTPUT_DIR --step normalize --input-dir "my manual rips"

Arguments:
    m3u                  m3u of .vgm/.vgz tracks (paths resolved relative to the
                          playlist). Optional when --step is combine/normalize.
    -o, --output DIR    output root (default: <playlist_stem>_rip next to the
                          playlist, or ./rip_output when no playlist is given)
    --gameboy             force the Game Boy preset (groups 0-2 @0.9, 3 @0.72,
                          SBOY core). Also used by --step combine to assign the
                          Game Boy baseline volumes when deriving groups from folders.
    --step {rip,combine,normalize}
                          start the pipeline at this step (default: rip)
    --input-dir DIR      for --step normalize: directory of WAVs to level
                          (default: <output>/combined if present, else <output>)
    --dry-run             print the exact vgm2wav-mute/ffmpeg commands, then stop
    -v, --verbose        show the underlying tool output
    --vgm2wav-mute PATH override the auto-detected vgm2wav-mute location
    --ffmpeg PATH        override the auto-detected ffmpeg location
    --no-normalize       stop after combine (skip peak normalization)

This script is self-contained when placed next to vgm2wav-mute.exe and
libiconv.dll (the executable is searched for next to this script first).
ffmpeg must be on PATH (or passed via --ffmpeg).

Example session (Game Boy):
    $ python rip_pipeline.py "Operation C (Game Boy)/test3.m3u"
    # detects Game Boy, rips groups 0-2 (vol 0.9) and 3 (vol 0.72) with the
    # SBOY core into 0-2/ and 3/, combines them, then normalizes every track
    # to the average peak (outliers >3.5 dB capped at +/-3.5 dB).

Example (other system, interactive):
    $ python rip_pipeline.py "Some SNES OST.m3u"
    # drops into a menu: add groups with `s 0 1`, `span 0-3`, `combo 0,1`,
    # set a volume, then `p` to proceed.

Example (manual rip, level only):
    $ python rip_pipeline.py -o my_rip --step normalize --input-dir "my manual rips"
    # reads every WAV in "my manual rips", finds the average peak, and writes
    # levelled copies into my_rip/final/ (outlier-aware, +/-3.5 dB cap).

Design notes (see plans/rip_pipeline.md):
  * Ripping is done at FULL volume (no gain at rip time). Per-group baseline
    gain (e.g. Game Boy 0-2 @0.9, channel 3 @0.72) is applied later,
    during the combine step, so the raw channel rips stay reusable.
  * Game Boy is detected automatically (device type 0x13) and uses the
    SBOY core (forced via --core 0x13=SBOY). The preset is:
        group "0-2" -> channels 0,1,2 @ volume 0.9
        group "3"   -> channel 3      @ volume 0.72
  * Other systems drop into an interactive menu where the user builds groups
    out of single (5), span (0-7) or combo (0,1,3-12,17,21) channel
    specs, each with its own volume. Unmanaged channels are shown; pressing
    a key proceeds with the queue.
  * Normalization: peak must be within +/-0.6 dB of the average peak
    (no change inside that window). Tracks further than 3.5 dB off are
    outliers and are only adjusted by +/-3.5 dB (capped), never more.
    RMS is ignored. Outliers are listed separately in the report.
  * Peak analysis ignores the first and last 1.0 s of every track (PEAK_TRIM_SEC)
    so random edge pops/clicks don't skew the measurement.

Dependencies: vgm2wav-mute (built with SBOY) and ffmpeg/ffprobe.
"""

import argparse
import json
import math
import os
import shlex
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

# Make stdout/stderr UTF-8 so non-ASCII (e.g. Japanese VGM tags) can be
# printed safely on a cp1251 console instead of raising UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _dump(res, verbose):
    if verbose and res.stderr:
        sys.stderr.write(res.stderr)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
GAMEBOY_TYPE = 0x13  # DEVID_GB_DMG

# Game Boy preset: list of (group_dir_name, [channels], baseline_volume)
GB_PRESET = [
    ("0-2", [0, 1, 2], 0.9),
    ("3",   [3],        0.72),
]

PEAK_TOLERANCE_DB = 0.6    # inside +/- this of the average => leave alone
OUTLIER_THRESHOLD_DB = 3.5  # beyond this => cap the adjustment at +/- this
PEAK_TRIM_SEC = 1.0        # ignore first/last N seconds for peak analysis (edge pops)

# Persisted pipeline state (written during --step rip, read by later steps so
# they know the group layout without re-probing / re-ripping).
STATE_FILENAME = "rip_pipeline.state.json"

SCRIPT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Helpers: external tools
# --------------------------------------------------------------------------
def find_vgm2wav_mute(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"vgm2wav-mute not found at: {explicit}")
    # candidate locations: bundled next to this script first, then dev builds
    candidates = [
        SCRIPT_DIR / "vgm2wav-mute.exe",
        SCRIPT_DIR / "vgm2wav-mute/build/Release/vgm2wav-mute.exe",
        SCRIPT_DIR / "vgm2wav-mute/vgm2wav-mute.exe",
        SCRIPT_DIR / "libvgm-research/build-mute-sboy/bin/Release/vgm2wav-mute.exe",
        SCRIPT_DIR / "libvgm-research/build-mute/bin/Release/vgm2wav-mute.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("vgm2wav-mute")
    if found:
        return Path(found)
    raise SystemExit(
        "vgm2wav-mute not found. Pass --vgm2wav-mute PATH or build it "
        "(see plans/rip_pipeline.md)."
    )


def find_ffmpeg(explicit=None):
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        raise SystemExit(f"ffmpeg not found at: {explicit}")
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("ffmpeg not found on PATH; install it or pass --ffmpeg PATH.")
    return Path(found)


# --------------------------------------------------------------------------
# Pipeline state (persisted so later steps can resume without re-ripping)
# --------------------------------------------------------------------------
def save_state(out_dir, devices, groups):
    """Persist the device + group layout so --step combine/normalize can resume."""
    state = {
        "devices": devices,
        "groups": [
            {
                "name": g["name"],
                "dev_type": g["dev_type"],
                "dev_inst": g["dev_inst"],
                "dev_ch": g["dev_ch"],
                "keep": sorted(g["keep"]),
                "disable_dev": g.get("disable_dev", False),
                "mute_other_devs": g.get("mute_other_devs", True),
                "volume": g["volume"],
            }
            for g in groups
        ],
    }
    (out_dir / STATE_FILENAME).write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


def load_state(out_dir):
    """Load a previously saved state. Returns (devices, groups) or (None, None)."""
    p = out_dir / STATE_FILENAME
    if not p.exists():
        return None, None
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  (warning: could not read state file: {e})", file=sys.stderr)
        return None, None
    devices = state.get("devices", [])
    groups = []
    for g in state.get("groups", []):
        ng = dict(g)
        ng["keep"] = set(g.get("keep", []))
        groups.append(ng)
    return devices, groups


def derive_groups_from_dirs(out_dir, gameboy):
    """Build groups from existing subdirectories that contain WAVs.

    Used by --step combine when no saved state exists (e.g. a manual rip).
    Each qualifying subdirectory becomes one group. With --gameboy the Game Boy
    baseline volumes are assigned by folder name (0-2 -> 0.9, 3 -> 0.72);
    otherwise every group defaults to volume 1.0.
    """
    groups = []
    if not out_dir.is_dir():
        return groups
    for d in sorted(out_dir.iterdir()):
        if not d.is_dir():
            continue
        if not any(d.glob("*.wav")):
            continue
        name = d.name
        vol = 1.0
        if gameboy:
            if name == "0-2":
                vol = 0.9
            elif name == "3":
                vol = 0.72
        groups.append({
            "name": name,
            "dev_type": GAMEBOY_TYPE if gameboy else 0,
            "dev_inst": 0,
            "dev_ch": 4 if gameboy else 0,
            "keep": set(),
            "disable_dev": False,
            "mute_other_devs": False,
            "volume": vol,
        })
    return groups


def discover_tracks_from_groups(out_dir, groups):
    """Return sorted unique track stems found inside the group subdirectories."""
    stems = set()
    for g in groups:
        gdir = out_dir / g["name"]
        if not gdir.is_dir():
            continue
        for w in gdir.glob("*.wav"):
            stems.add(w.stem)
    return sorted(stems)


# --------------------------------------------------------------------------
# M3U parsing
# --------------------------------------------------------------------------
def parse_m3u(m3u_path):
    m3u_path = Path(m3u_path)
    base = m3u_path.parent
    tracks = []
    for raw in m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file://"):
            line = line[7:]
        p = Path(line)
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        if p.suffix.lower() in (".vgm", ".vgz") and p.exists():
            tracks.append(p)
        else:
            print(f"  (skipping missing/unsupported entry: {line})", file=sys.stderr)
    return tracks


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
def probe_track(vgm2wav_mute, track_path):
    """Run vgm2wav-mute --probe and parse the PROBE lines from stdout."""
    res = subprocess.run(
        [str(vgm2wav_mute), "--probe", str(track_path)],
        capture_output=True, text=True, errors="replace",
    )
    if res.returncode != 0:
        raise SystemExit(
            f"vgm2wav-mute --probe failed on {track_path}\n{res.stderr}"
        )
    devices = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("PROBE "):
            continue
        d = parse_probe_line(line[len("PROBE "):])
        if d:
            devices.append(d)
    return devices


def parse_probe_line(s):
    """Parse a single PROBE line into a device dict.

    Example:
      dev=0 type=0x13 name="GameBoy DMG" inst=0 ch=4 core=SBOY \
           names="Square 1","Square 2","Wave","Noise"
    """
    parts = shlex.split(s)
    kv = {}
    for tok in parts:
        if "=" in tok:
            k, v = tok.split("=", 1)
            kv[k] = v
    if "type" not in kv:
        return None
    names = []
    if kv.get("names"):
        names = [n.strip() for n in kv["names"].split(",") if n.strip()]
    return {
        "dev": int(kv.get("dev", "0")),
        "type": int(kv.get("type", "0x0"), 16),
        "inst": int(kv.get("inst", "0")),
        "ch": int(kv.get("ch", "0")),
        "name": kv.get("name", ""),
        "core": kv.get("core", ""),
        "names": names,
    }


def is_gameboy(devices):
    return any(d["type"] == GAMEBOY_TYPE for d in devices)


# --------------------------------------------------------------------------
# Building the rip queue
# --------------------------------------------------------------------------
def build_gameboy_queue(devices):
    gb = next(d for d in devices if d["type"] == GAMEBOY_TYPE)
    groups = []
    for name, chans, vol in GB_PRESET:
        groups.append({
            "name": name,
            "dev_type": gb["type"],
            "dev_inst": gb["inst"],
            "dev_ch": gb["ch"],
            "keep": set(chans),
            "disable_dev": False,
            "mute_other_devs": True,
            "volume": vol,
        })
    return groups, gb


def parse_channel_spec(spec, max_ch):
    """Parse '0-2,3,5-7' into a sorted list of ints in [0, max_ch)."""
    channels = set()
    spec = spec.strip()
    if not spec:
        return []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            for c in range(lo, hi + 1):
                if 0 <= c < max_ch:
                    channels.add(c)
        else:
            c = int(tok)
            if 0 <= c < max_ch:
                channels.add(c)
    return sorted(channels)


def interactive_menu(devices):
    """Let the user build groups interactively. Returns (groups, configured_dev)."""
    # pick the device to configure
    if len(devices) == 1:
        dev = devices[0]
    else:
        print("Multiple devices detected:")
        for i, d in enumerate(devices):
            print(f"  [{i}] {d['name']} (type 0x{d['type']:02X}, "
                  f"inst {d['inst']}, {d['ch']} channels)")
        idx = input("Configure which device? [0]: ").strip()
        dev = devices[int(idx) if idx else 0]

    groups = []
    managed = set()
    while True:
        print(f"\nDevice: {dev['name']} (type 0x{dev['type']:02X}, "
              f"inst {dev['inst']}, {dev['ch']} channels)")
        ch_desc = ", ".join(
            f"{c}:{dev['names'][c] if c < len(dev['names']) else '?'}"
            for c in range(dev['ch'])
        )
        print(f"  Channels: {ch_desc}")
        print(f"  Managed channels: {sorted(managed) if managed else '(none)'}")
        unmanaged = [c for c in range(dev['ch']) if c not in managed]
        print(f"  Unmanaged channels: {unmanaged if unmanaged else '(all managed)'}")
        if groups:
            print("  Current queue:")
            for g in groups:
                print(f"    - {g['name']}: channels {sorted(g['keep'])} @ {g['volume']}")
        print("Enter channel selection (e.g. 0-2, 3, 5-7), "
              "or [P]roceed / [Q]uit:")
        sel = input("> ").strip()
        if sel.lower() in ("p", "proceed"):
            break
        if sel.lower() in ("q", "quit"):
            sys.exit(0)
        chs = parse_channel_spec(sel, dev["ch"])
        if not chs:
            print("  (no valid channels parsed; try again)")
            continue
        vol_s = input("  Volume for this group (e.g. 0.9): ").strip()
        try:
            vol = float(vol_s)
        except ValueError:
            print("  invalid volume; try again")
            continue
        name = sel.replace(" ", "")
        groups.append({
            "name": name,
            "dev_type": dev["type"],
            "dev_inst": dev["inst"],
            "dev_ch": dev["ch"],
            "keep": set(chs),
            "disable_dev": False,
            "mute_other_devs": True,
            "volume": vol,
        })
        managed |= set(chs)

    if not groups:
        raise SystemExit("No groups defined; nothing to rip.")
    return groups, dev


def finalize_groups(groups, devices, configured_dev):
    """Add an implicit 'other' group for any devices other than the configured one."""
    if len(devices) <= 1:
        return groups
    other = {
        "name": "other",
        "dev_type": configured_dev["type"],
        "dev_inst": configured_dev["inst"],
        "dev_ch": configured_dev["ch"],
        "keep": set(range(configured_dev["ch"])),
        "disable_dev": True,        # silence the configured device entirely
        "mute_other_devs": False,  # keep the other devices at full volume
        "volume": 1.0,
    }
    return groups + [other]


# --------------------------------------------------------------------------
# Ripping
# --------------------------------------------------------------------------
def rip_group(vgm2wav_mute, track_path, group, devices, out_dir, verbose=False, dry_run=False):
    stem = track_path.stem
    grp_dir = out_dir / group["name"]
    out_wav = grp_dir / f"{stem}.wav"
    if out_wav.exists():
        if verbose:
            print(f"    [skip] {out_wav.name} (exists)")
        return out_wav

    mute_args = []
    if group.get("disable_dev"):
        mute_args += ["--mute",
                      f"0x{group['dev_type']:X}#{group['dev_inst']}.Disabled=True"]
    else:
        complement = [c for c in range(group["dev_ch"]) if c not in group["keep"]]
        if complement:
            chans = ",".join(f"Ch{c}" for c in complement)
            mute_args += ["--mute",
                          f"0x{group['dev_type']:X}#{group['dev_inst']}.{chans}"]
    if group.get("mute_other_devs"):
        for e in devices:
            if e["type"] == group["dev_type"] and e["inst"] == group["dev_inst"]:
                continue
            mute_args += ["--mute",
                          f"0x{e['type']:X}#{e['inst']}.Disabled=True"]

    core_args = []
    if group["dev_type"] == GAMEBOY_TYPE:
        core_args = ["--core", f"0x{group['dev_type']:X}=SBOY"]

    cmd = [str(vgm2wav_mute), *core_args, *mute_args, str(track_path), str(out_wav)]
    print(f"    [rip ] {group['name']}/{out_wav.name}")
    if dry_run:
        print("           " + " ".join(cmd))
        grp_dir.mkdir(parents=True, exist_ok=True)
        return out_wav
    grp_dir.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    _dump(res, verbose)
    if res.returncode != 0:
        print(f"    !! rip failed for {track_path.name} group {group['name']}:\n{res.stderr}",
              file=sys.stderr)
    return out_wav


# --------------------------------------------------------------------------
# Combining (per-group baseline gain applied here)
# --------------------------------------------------------------------------
def combine_track(track_stem, groups, out_dir, combined_dir, ffmpeg, verbose=False, dry_run=False):
    inputs, volumes = [], []
    for g in groups:
        wav = out_dir / g["name"] / f"{track_stem}.wav"
        if wav.exists():
            inputs.append(wav)
            volumes.append(g["volume"])
    if not inputs:
        if dry_run:
            # speculative: assume every group wav would be produced
            inputs = [out_dir / g["name"] / f"{track_stem}.wav" for g in groups]
            volumes = [g["volume"] for g in groups]
        else:
            print(f"    [skip] combine {track_stem} (no group wavs)")
            return None
    out_path = combined_dir / f"{track_stem}.wav"
    if out_path.exists() and not dry_run:
        if verbose:
            print(f"    [skip] combine {track_stem} (exists)")
        return out_path

    n = len(inputs)
    if n == 1:
        filt = f"[0]volume={volumes[0]}[out]"
    else:
        parts = [f"[{i}]volume={volumes[i]}[g{i}]" for i in range(n)]
        labels = "".join(f"[g{i}]" for i in range(n))
        amix = (f"{labels}amix=inputs={n}:duration=longest:"
                 f"dropout_transition=0,volume={n}[out]")
        filt = ";".join(parts) + ";" + amix

    grp_names = ", ".join(g["name"] for g in groups)
    print(f"    [comb] {track_stem}.wav  (groups: {grp_names})")

    if dry_run:
        cmd = [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            *sum((["-i", str(p)] for p in inputs), []),
            "-filter_complex", filt, "-map", "[out]",
            "-c:a", "pcm_s16le", str(out_path),
        ]
        print("           " + " ".join(cmd))
        return out_path

    cmd = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
        *sum((["-i", str(p)] for p in inputs), []),
        "-filter_complex", filt,
        "-map", "[out]",
        "-c:a", "pcm_s16le",
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    _dump(res, verbose)
    if res.returncode != 0:
        print(f"    !! combine failed for {track_stem}:\n{res.stderr}", file=sys.stderr)
        return None
    return out_path


# --------------------------------------------------------------------------
# Peak analysis + normalization
# --------------------------------------------------------------------------
def peak_db_ffmpeg(path, ffmpeg, trim_sec=PEAK_TRIM_SEC):
    """Peak (dBFS) via ffmpeg astats. The first and last `trim_sec` seconds are
    dropped so edge pops/clicks don't skew the measurement."""
    def _run(filt):
        res = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path), "-af", filt, "-f", "null", "-"],
            capture_output=True, text=True, errors="replace",
        )
        for line in res.stderr.splitlines():
            low = line.lower()
            if "peak level db" in low:
                try:
                    val = line.split("Peak level dB:")[1].strip()
                    return float(val)
                except (IndexError, ValueError):
                    pass
        return None
    # Drop first and last `trim_sec` seconds (reverse trick needs no duration).
    trimmed = (f"atrim={trim_sec},asetpts=PTS-STARTPTS,areverse,"
               f"atrim={trim_sec},asetpts=PTS-STARTPTS,astats")
    v = _run(trimmed)
    if v is not None:
        return v
    # Fallback: very short track where trimming would empty it -> analyze whole.
    return _run("astats")


def peak_db_python(path, trim_sec=PEAK_TRIM_SEC):
    """Fallback peak (dBFS) reader that handles both plain PCM and
    WAVEFORMATEXTENSIBLE (which the stdlib wave module can't open).
    The first and last `trim_sec` seconds are skipped."""
    with open(str(path), "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0.0
        nch = sw = rate = 0
        # locate the 'fmt ' chunk
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4]
            size = struct.unpack("<I", hdr[4:8])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                wtag = struct.unpack("<H", fmt[0:2])[0]
                nch = struct.unpack("<H", fmt[2:4])[0]
                rate = struct.unpack("<I", fmt[4:8])[0]
                bits = struct.unpack("<H", fmt[14:16])[0]
                sw = bits // 8
                if wtag == 0xFFFE:  # extensible -> subformat in first 2 bytes of GUID
                    sub = struct.unpack("<H", fmt[16:18])[0]
                    if sub != 0x0001:
                        return 0.0
                elif wtag != 0x0001:
                    return 0.0
                break
            f.seek(size + (size & 1), 1)
        if not (nch and sw and rate):
            return 0.0
        # locate the 'data' chunk
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid = hdr[:4]
            size = struct.unpack("<I", hdr[4:8])[0]
            if cid == b"data":
                break
            f.seek(size + (size & 1), 1)
        maxv = float(1 << (sw * 8 - 1))
        signed = (sw != 1)
        # skip first/last `trim_sec` seconds
        frames_total = size // (sw * nch)
        skip = int(trim_sec * rate)
        start = skip
        end = frames_total - skip
        if end <= start:
            start, end = 0, frames_total
        f.seek(start * sw * nch, 1)  # discard leading frames
        peak = 0
        remaining = (end - start) * sw * nch  # bytes to scan
        while remaining > 0:
            chunk = f.read(min(1 << 16, remaining))
            if not chunk:
                break
            cnt = len(chunk) // sw
            if sw == 3:
                vals = []
                for i in range(cnt):
                    b = chunk[i * 3:(i + 1) * 3]
                    v = b[0] | (b[1] << 8) | (b[2] << 16)
                    if v & 0x800000:
                        v -= 0x1000000
                    vals.append(v)
            else:
                fmt = "<" + ("h" if sw == 2 else "i") * cnt
                vals = list(struct.unpack(fmt, chunk))
                if not signed:
                    vals = [v - 128 for v in vals]
            for v in vals:
                a = abs(v)
                if a > peak:
                    peak = a
            remaining -= len(chunk)
        if peak <= 0:
            return float("-inf")
        return 20.0 * math.log10(peak / maxv)


def get_peak_db(path, ffmpeg):
    v = peak_db_ffmpeg(path, ffmpeg)
    if v is not None and math.isfinite(v):
        return v
    return peak_db_python(path)


def normalize_track(track_stem, src_path, final_dir, ffmpeg, avg_peak,
                    verbose=False, dry_run=False, no_normalize=False):
    """Normalize a single source WAV to `avg_peak` (outlier-aware) into final_dir."""
    if not src_path.exists():
        return None, None, False
    peak = get_peak_db(src_path, ffmpeg)
    diff = peak - avg_peak  # >0 => louder than average

    if no_normalize:
        gain = 0.0
        outlier = False
    elif abs(diff) <= PEAK_TOLERANCE_DB:
        gain = 0.0
        outlier = False
    elif diff < -OUTLIER_THRESHOLD_DB:
        gain = +OUTLIER_THRESHOLD_DB  # quiet outlier: boost, capped
        outlier = True
    elif diff > OUTLIER_THRESHOLD_DB:
        gain = -OUTLIER_THRESHOLD_DB  # loud outlier: attenuate, capped
        outlier = True
    else:
        gain = -diff  # bring exactly to the average peak
        outlier = False

    dst = final_dir / f"{track_stem}.wav"
    if abs(gain) < 0.01:
        if dry_run:
            print(f"    [copy] {track_stem}.wav (gain ~0)")
        else:
            shutil.copy(src_path, dst)
    else:
        cmd = [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src_path),
            "-af", f"volume={gain:.3f}dB",
            "-c:a", "pcm_s16le",
            str(dst),
        ]
        if dry_run:
            print(f"    [norm] {track_stem}.wav  peak {peak:+.2f} -> gain {gain:+.2f} dB"
                  f"{'  (OUTLIER)' if outlier else ''}")
        else:
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            _dump(res, verbose)
            if res.returncode != 0:
                print(f"    !! normalize failed for {track_stem}:\n{res.stderr}", file=sys.stderr)
    return peak, gain, outlier


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def fmt_db(x):
    if x == float("-inf"):
        return "   -inf"
    return f"{x:+.2f}"


def emit_report(stems, peaks, gains, outliers, avg_peak, final_dir, ffmpeg, verify=True):
    print("\n" + "=" * 72)
    print("RIP PIPELINE REPORT")
    print("=" * 72)
    print(f"{'Track':<48}{'Peak':>9}{'Gain':>9}  Outlier")
    print("-" * 72)
    for stem in stems:
        peak = peaks.get(stem)
        gain = gains.get(stem)
        flag = "YES" if outliers.get(stem) else ""
        print(f"{stem:<48}{fmt_db(peak):>9}{fmt_db(gain) if gain is not None else '   n/a':>9}  {flag}")

    print("-" * 72)
    print(f"Average peak (target): {fmt_db(avg_peak)} dB")

    out_list = [(stem, peaks[stem], gains[stem]) for stem in stems if outliers.get(stem)]
    if out_list:
        print("\nOUTLIERS (adjusted with the +/-3.5 dB cap):")
        for stem, peak, gain in out_list:
            print(f"  - {stem}.wav : peak {fmt_db(peak)} dB, applied gain {fmt_db(gain)} dB")
    else:
        print("\nNo outliers detected.")

    if verify:
        # optional verification: re-measure final peaks
        print("\nVerification (final peaks):")
        all_ok = True
        for stem in stems:
            final = final_dir / f"{stem}.wav"
            if not final.exists():
                continue
            fp = get_peak_db(final, ffmpeg)
            fdiff = fp - avg_peak
            ok = abs(fdiff) <= PEAK_TOLERANCE_DB or (
                outliers.get(stem) and abs(fdiff) <= OUTLIER_THRESHOLD_DB + 1e-6
            )
            if not ok:
                all_ok = False
            mark = "ok" if ok else "CHECK"
            print(f"  - {stem:<44}{fmt_db(fp):>9}  ({mark})")
        print("\nAll final peaks within tolerance." if all_ok else
              "\nWARNING: some final peaks are outside tolerance.")


def analyze_and_normalize(track_inputs, final_dir, ffmpeg, avg_peak=None,
                          verbose=False, dry_run=False, no_normalize=False):
    """Analyze peaks of the given (stem, src_path) inputs and normalize each to the
    average peak (outlier-aware). Writes results into final_dir.

    track_inputs : list of (stem, Path) tuples.
    avg_peak     : if provided, use it as the target; otherwise compute the mean
                    of the finite peaks in this batch.
    """
    peaks, gains, outliers = {}, {}, {}
    for stem, src in track_inputs:
        if not src.exists():
            continue
        peaks[stem] = get_peak_db(src, ffmpeg)

    valid = [p for p in peaks.values() if math.isfinite(p)]
    if not valid:
        raise SystemExit("No tracks to analyze.")
    target = avg_peak if (avg_peak is not None and math.isfinite(avg_peak)) else (sum(valid) / len(valid))

    for stem, src in track_inputs:
        if stem not in peaks:
            continue
        peak, gain, outlier = normalize_track(
            stem, src, final_dir, ffmpeg, target,
            verbose=verbose, dry_run=dry_run, no_normalize=no_normalize,
        )
        gains[stem] = gain
        outliers[stem] = outlier

    emit_report(list(peaks.keys()), peaks, gains, outliers, target, final_dir, ffmpeg)


# --------------------------------------------------------------------------
# Interactive step chooser (used when launched with no playlist / no --step)
# --------------------------------------------------------------------------
def prompt_for_step():
    """Prompt for what to do when no playlist and no explicit --step were given.

    Returns (mode, playlist) where mode is one of 'analyse', 'analyse_combined',
    'tweak', 'rip', and playlist is the chosen .m3u path (or None for the others).
    """
    print("\nNo playlist provided. Choose what to do:")
    print("  1) Analyse audio only              - report peak loudness, make no changes")
    print("  2) Analyse + tweak combined audio  - normalize WAVs next to this script into final/")
    print("  3) Analyse and tweak (full)        - combine channels + normalize into final/")
    print("  4) Rip from a playlist             - you'll be asked for the .m3u path")
    while True:
        choice = input("Choice [1/2/3/4]: ").strip()
        if choice == "1":
            return "analyse", None
        if choice == "2":
            return "analyse_combined", None
        if choice == "3":
            return "tweak", None
        if choice == "4":
            path = input("Playlist (.m3u) path: ").strip()
            if path:
                return "rip", path
            print("No path entered; please choose again.")
            continue
        print("Please enter 1, 2, 3, or 4.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Game Boy / generic soundtrack rip -> combine -> normalize pipeline. "
                    "Run with no arguments for an interactive step menu.")
    ap.add_argument("m3u", nargs="?",
                    help="input .m3u playlist of .vgm/.vgz files "
                         "(optional for --step combine/normalize)")
    ap.add_argument("-o", "--output", help="output directory "
                    "(default: <m3u_stem>_rip/, or ./rip_output when no playlist)")
    ap.add_argument("--vgm2wav-mute", help="path to vgm2wav-mute.exe")
    ap.add_argument("--ffmpeg", help="path to ffmpeg")
    ap.add_argument("--gameboy", action="store_true",
                    help="force the Game Boy preset (skip probing)")
    ap.add_argument("--step", choices=["rip", "combine", "normalize"], default="rip",
                    help="start the pipeline at this step "
                         "(rip=full, combine=skip rip, normalize=analyze+normalize only)")
    ap.add_argument("--input-dir", help="for --step normalize: directory of WAVs to "
                    "normalize (default: <output>/combined if present, else <output>)")
    ap.add_argument("--dry-run", action="store_true", help="show commands, do nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-normalize", action="store_true",
                    help="stop after combine (skip peak normalization)")
    args = ap.parse_args()

    # No playlist and no explicit step -> let the user pick what to do.
    if args.m3u is None and args.step == "rip":
        mode, playlist = prompt_for_step()
        if mode == "analyse":
            args.step = "normalize"
            args.analyse_only = True
        elif mode == "analyse_combined":
            # Already-combined WAVs live next to this script; normalize them
            # in place (top-level only, subdirectories ignored) into final/.
            args.step = "normalize"
            args.input_dir = str(SCRIPT_DIR)
            if args.output is None:
                args.output = str(SCRIPT_DIR)
        elif mode == "tweak":
            args.step = "combine"
        elif mode == "rip":
            args.m3u = playlist
            args.step = "rip"
        # In interactive mode operate relative to the current directory,
        # unless the user explicitly passed -o/--output (or a choice set it).
        if args.output is None:
            args.output = "."

    step = args.step

    # ffmpeg is needed for combine + normalize; vgm2wav-mute only for rip.
    ffmpeg = find_ffmpeg(args.ffmpeg)
    vgm2wav_mute = None
    if step == "rip":
        vgm2wav_mute = find_vgm2wav_mute(args.vgm2wav_mute)

    # Resolve output directory.
    if args.output:
        out_dir = Path(args.output)
    elif args.m3u:
        m3u_path = Path(args.m3u)
        out_dir = m3u_path.parent / f"{m3u_path.stem}_rip"
    else:
        out_dir = Path("rip_output")
    out_dir.mkdir(parents=True, exist_ok=True)
    combined_dir = out_dir / "combined"
    final_dir = out_dir / "final"
    combined_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # STEP: rip (full pipeline)
    # ------------------------------------------------------------------
    if step == "rip":
        if not args.m3u:
            raise SystemExit("--step rip requires an m3u playlist argument.")
        m3u_path = Path(args.m3u)
        tracks = parse_m3u(m3u_path)
        if not tracks:
            raise SystemExit(f"No .vgm/.vgz tracks found in {m3u_path}")

        # --- detect system / build queue ---
        if args.gameboy:
            print("Forcing Game Boy preset (--gameboy).")
            # synthesize a GB device for the preset
            gb_dev = {"type": GAMEBOY_TYPE, "inst": 0, "ch": 4, "name": "GameBoy DMG",
                       "core": "SBOY", "names": ["Square 1", "Square 2", "Wave", "Noise"]}
            devices = [gb_dev]
            groups, configured_dev = build_gameboy_queue(devices)
        else:
            print(f"Probing first track: {tracks[0].name}")
            devices = probe_track(vgm2wav_mute, tracks[0])
            if not devices:
                raise SystemExit("Probe returned no devices; cannot continue.")
            if is_gameboy(devices):
                print("Detected system: Game Boy -> applying preset "
                      "(0-2 @0.9, 3 @0.72, SBOY core).")
                groups, configured_dev = build_gameboy_queue(devices)
            else:
                names = ", ".join(d["name"] for d in devices)
                print(f"Detected system: {names} -> interactive group builder.")
                groups, configured_dev = interactive_menu(devices)

        groups = finalize_groups(groups, devices, configured_dev)
        save_state(out_dir, devices, groups)

        print(f"\nRip queue ({len(groups)} group(s)):")
        for g in groups:
            print(f"  - {g['name']}: dev 0x{g['dev_type']:X}#{g['dev_inst']} "
                  f"channels {sorted(g['keep'])} @ {g['volume']}"
                  f"{'  [disable dev, keep others]' if g.get('disable_dev') else ''}")

        if args.dry_run:
            print("\n[DRY RUN] would rip the following:")
        else:
            print()

        # --- rip ---
        for t in tracks:
            print(f"Ripping: {t.name}")
            for g in groups:
                rip_group(vgm2wav_mute, t, g, devices, out_dir,
                          verbose=args.verbose, dry_run=args.dry_run)

        # --- combine ---
        print("\nCombining groups (applying per-group baseline gain):")
        for t in tracks:
            combine_track(t.stem, groups, out_dir, combined_dir, ffmpeg,
                          verbose=args.verbose, dry_run=args.dry_run)

        if args.dry_run:
            print("\n[DRY RUN] stopping before analysis/normalization.")
            return

        # --- analyze + normalize ---
        print("\nAnalyzing combined peaks and normalizing:")
        track_inputs = [(t.stem, combined_dir / f"{t.stem}.wav") for t in tracks]
        analyze_and_normalize(track_inputs, final_dir, ffmpeg,
                              verbose=args.verbose, dry_run=args.dry_run,
                              no_normalize=args.no_normalize)

    # ------------------------------------------------------------------
    # STEP: combine (skip rip; combine existing group dirs, then normalize)
    # ------------------------------------------------------------------
    elif step == "combine":
        devices, groups = load_state(out_dir)
        if not groups:
            print("No saved pipeline state; deriving groups from subdirectories "
                  "of the output directory.")
            groups = derive_groups_from_dirs(out_dir, args.gameboy)
        if not groups:
            raise SystemExit(
                "No groups found (no state file and no group subdirectories in "
                f"{out_dir}). Run --step rip first, or place ripped group "
                "folders (e.g. 0-2/, 3/) under the output directory.")
        if devices is None:
            devices = []

        stems = discover_tracks_from_groups(out_dir, groups)
        if not stems:
            raise SystemExit("No WAV files found in the group subdirectories.")
        print(f"\nCombine queue ({len(groups)} group(s)) for {len(stems)} track(s):")
        for g in groups:
            print(f"  - {g['name']}: channels {sorted(g['keep'])} @ {g['volume']}")

        if args.dry_run:
            print("\n[DRY RUN] would combine the following:")
        else:
            print()

        print("Combining groups (applying per-group baseline gain):")
        for stem in stems:
            combine_track(stem, groups, out_dir, combined_dir, ffmpeg,
                          verbose=args.verbose, dry_run=args.dry_run)

        if args.dry_run:
            print("\n[DRY RUN] stopping before analysis/normalization.")
            return

        print("\nAnalyzing combined peaks and normalizing:")
        track_inputs = [(stem, combined_dir / f"{stem}.wav") for stem in stems]
        analyze_and_normalize(track_inputs, final_dir, ffmpeg,
                              verbose=args.verbose, dry_run=args.dry_run,
                              no_normalize=args.no_normalize)

    # ------------------------------------------------------------------
    # STEP: normalize (analyze + normalize a folder of WAVs only)
    # ------------------------------------------------------------------
    elif step == "normalize":
        if args.input_dir:
            input_dir = Path(args.input_dir)
        elif combined_dir.exists() and any(combined_dir.glob("*.wav")):
            input_dir = combined_dir
        else:
            input_dir = out_dir
        if not input_dir.is_dir():
            raise SystemExit(f"Input directory not found: {input_dir}")
        wavs = sorted(input_dir.glob("*.wav"))
        if not wavs:
            raise SystemExit(f"No WAV files found in {input_dir}.")
        print(f"\nNormalize-only step: analyzing {len(wavs)} WAV file(s) in {input_dir}")
        track_inputs = [(w.stem, w) for w in wavs]

        if getattr(args, "analyse_only", False):
            # Analysis only: report peaks, write nothing.
            peaks = {w.stem: get_peak_db(w, ffmpeg) for w in wavs}
            valid = [p for p in peaks.values() if math.isfinite(p)]
            target = (sum(valid) / len(valid)) if valid else 0.0
            print(f"\nAnalysis only (no changes written) for {len(wavs)} WAV file(s) in {input_dir}")
            emit_report(list(peaks.keys()), peaks,
                        {s: 0.0 for s in peaks}, {s: False for s in peaks},
                        target, input_dir, ffmpeg, verify=False)
            return

        if args.dry_run:
            print("[DRY RUN] would normalize the following:")
        analyze_and_normalize(track_inputs, final_dir, ffmpeg,
                              verbose=args.verbose, dry_run=args.dry_run,
                              no_normalize=args.no_normalize)


if __name__ == "__main__":
    main()
