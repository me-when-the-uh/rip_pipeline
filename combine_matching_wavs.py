#!/usr/bin/env python3
"""
combine_matching_wavs.py

Scan subdirectories for WAV files, group by exact basename + approximate file size
(~1% difference), then MIX (parallel sum) the matching files into a single WAV.

This is intended for combining per-channel (or per-group) WAV dumps of the *same*
logical track/song. Each input file represents simultaneous audio that should be
added together (e.g. different chip channels or mute-pass layers). Up to 30+ inputs
supported.

NOT concatenation (end-to-end). It performs a sample-by-sample sum across all inputs.

Default behavior (no flags):
  - Scan current directory recursively for *.wav (case-insensitive)
  - Group by exact basename across subdirectories
  - Require ~same file size (proxy for duration) and identical format
  - MIX the group members and write <stem>.wav into the current directory

Prefers external tools for highest quality mixing when available:
  - ffmpeg (recommended): uses amix + volume compensation for accurate sum
  - sox (fallback)
Pure-Python mixer (stdlib wave + struct) is used otherwise. All paths are lossless PCM
(no resampling, no re-encoding). 16-bit output with clipping on overflow.

Usage examples:
  python combine_matching_wavs.py
  python combine_matching_wavs.py --dry-run -v
  python combine_matching_wavs.py "VGMPlay_052-0/old/Metal_Gear_2_-_Solid_Snake_(MSX2)" -t 0.02 -v
  python combine_matching_wavs.py . --tolerance 0.05 -o ./combined
  python combine_matching_wavs.py -ac
  python combine_matching_wavs.py . --audio-check -o ./combined

Audio check (loudness / clipping analysis):
  The script can also analyze the *final* tracks in the work directory for
  consistent, comfortable loudness. At startup it prompts "Audio check? [y/n/1/0]"
  (or pass -ac/--audio-check to skip the prompt). It then checks each top-level
  WAV's peak and RMS level, tracks the collection averages, and prints concise
  recommendations (concise; only about peak headroom / clipping):
    - CLIPPED (samples pinned at the rail) -> audio already damaged; fix at source.
    - Any other track peaking above -10 dBFS -> loud but not clipping; summarized
      in ONE line (with the hottest peak) rather than nagged per track. If you
      want uniform headroom, apply a collection-wide limiter at -10 dBFS.
    Per-track RMS and cross-track averages are shown for reference only; they do
    not generate volume-change advice (not meaningful across mixed content such as
    SFX vs music).
  Thresholds are defined as module-level constants (AUDIO_CHECK_*) for easy tuning.
  The check runs after combining and works even when there is nothing to combine.
"""

import argparse
import math
import shutil
import struct
import subprocess
import sys
import wave
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Audio-check tuning (adjust these easily)
# ---------------------------------------------------------------------------
# Desired peak headroom: aim to keep peaks at/under this dBFS so the track is
# not clipped and not overly loud. Shown as the safety target in the table;
# tracks above it are summarized in one line rather than nagged per track.
AUDIO_CHECK_TARGET_PEAK_DBFS = -10.0

# Seconds trimmed from the START and END of each track before measuring peak/RMS,
# so random edge pops/clicks don't skew the loudness analysis.
ANALYSIS_TRIM_SEC = 1.0


def find_wav_files(root: Path) -> list[Path]:
    """Recursively find all .wav files (case-insensitive suffix)."""
    wavs = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".wav":
            wavs.append(p)
    return wavs


def find_top_level_wavs(root: Path) -> list[Path]:
    """List only direct-child .wav files of `root` (no recursion into subdirs).

    Used by the audio check so it analyzes the *final* tracks in the work
    directory rather than the per-channel source dumps that live in
    subdirectories (those share basenames and would be misleading to check).
    """
    wavs = []
    if not root.is_dir():
        return wavs
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() == ".wav":
            wavs.append(p)
    return wavs


def group_by_basename(paths: list[Path]) -> dict[str, list[Path]]:
    """Group file paths by their exact basename."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in paths:
        groups[p.name].append(p)
    return groups


def sizes_within_tolerance(sizes: list[int], tolerance: float = 0.01) -> bool:
    """Return True if max/min size difference is within tolerance (relative to min)."""
    if not sizes:
        return False
    min_s = min(sizes)
    if min_s == 0:
        return False
    max_s = max(sizes)
    return (max_s - min_s) / min_s <= tolerance


def durations_within_tolerance(durations: list[float], tolerance: float = 0.02) -> bool:
    """Return True if max/min duration (seconds) difference is within tolerance."""
    if not durations:
        return False
    min_d = min(durations)
    if min_d <= 0:
        return False
    max_d = max(durations)
    return (max_d - min_d) / min_d <= tolerance


def get_wav_format_info(path: Path) -> tuple | None:
    """Return (nchannels, sampwidth, framerate, comptype) or None on failure."""
    try:
        with wave.open(str(path), "rb") as w:
            p = w.getparams()
            return (p.nchannels, p.sampwidth, p.framerate, p.comptype)
    except Exception:
        return None


def get_wav_info(path: Path):
    """Return dict with format + duration info, or None."""
    try:
        with wave.open(str(path), "rb") as w:
            p = w.getparams()
            rate = p.framerate or 0
            dur = (p.nframes / rate) if rate > 0 else 0.0
            return {
                "nchannels": p.nchannels,
                "sampwidth": p.sampwidth,
                "framerate": rate,
                "comptype": p.comptype,
                "nframes": p.nframes,
                "duration": dur,
                "size": path.stat().st_size if path.exists() else 0,
            }
    except Exception:
        return None


def formats_compatible(paths: list[Path]) -> bool:
    """All files must have identical format for safe concat without resampling."""
    formats = []
    for p in paths:
        fmt = get_wav_format_info(p)
        if fmt is None:
            return False
        formats.append(fmt)
    # All must be identical
    return len(set(formats)) == 1


def _track_levels_ffmpeg(path: Path, ffmpeg_path: str, trim_sec: float = ANALYSIS_TRIM_SEC) -> tuple[float, float] | None:
    """Return (peak_dbfs, rms_dbfs) via ffmpeg astats, or None on failure.
    The first and last `trim_sec` seconds are dropped so edge pops/clicks
    don't skew the measurement."""
    def _run(filt: str) -> tuple[float, float] | None:
        try:
            cmd = [ffmpeg_path, "-hide_banner", "-i", str(path), "-af", filt, "-f", "null", "-"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            out = res.stderr
        except Exception:
            return None
        peak = rms = None
        for line in out.splitlines():
            low = line.lower()
            if "rms level db" in low:
                try:
                    rms = float(line.split("RMS level dB:")[1].strip())
                except Exception:
                    pass
            elif "peak level db" in low:
                try:
                    peak = float(line.split("Peak level dB:")[1].strip())
                except Exception:
                    pass
        if peak is None or rms is None:
            return None
        return (peak, rms)

    # Drop first and last `trim_sec` seconds (reverse trick needs no duration).
    trimmed = (f"atrim={trim_sec},asetpts=PTS-STARTPTS,areverse,"
               f"atrim={trim_sec},asetpts=PTS-STARTPTS,astats")
    res = _run(trimmed)
    if res is not None:
        return res
    # Fallback: very short track where trimming would empty it -> analyze whole.
    return _run("astats")


def _track_levels_python(path: Path, trim_sec: float = ANALYSIS_TRIM_SEC) -> tuple[float, float] | None:
    """Return (peak_dbfs, rms_dbfs) using pure stdlib wave.

    Supports 8/16/24/32-bit PCM. Used as a fallback when ffmpeg is unavailable.
    The first and last `trim_sec` seconds are skipped.
    """
    try:
        with wave.open(str(path), "rb") as w:
            nch = w.getnchannels()
            sw = w.getsampwidth()
            rate = w.getframerate()
            nframes = w.getnframes()
            if nframes == 0:
                return (0.0, 0.0)
            if sw == 1:
                fmt = "B"          # unsigned 8-bit
                maxv = 128.0
                signed = False
            elif sw == 2:
                fmt = "h"          # signed 16-bit
                maxv = 32768.0
                signed = True
            elif sw == 3:
                maxv = 8388608.0   # signed 24-bit (handled below)
                signed = True
            elif sw == 4:
                fmt = "i"          # signed 32-bit
                maxv = 2147483648.0
                signed = True
            else:
                return None

            # skip first/last `trim_sec` seconds
            skip = int(trim_sec * rate)
            start = skip
            end = nframes - skip
            if end <= start:
                start, end = 0, nframes
            w.readframes(start)  # discard leading frames
            peak = 0.0
            sum_sq = 0.0
            total = 0
            remaining = end - start
            chunk = 4096 * nch
            while remaining > 0:
                nframes_now = min(chunk, remaining)
                data = w.readframes(nframes_now)
                if not data:
                    break
                if sw == 3:
                    count = len(data) // 3
                    samples = []
                    for i in range(count):
                        b = data[i * 3:(i + 1) * 3]
                        val = b[0] | (b[1] << 8) | (b[2] << 16)
                        if val & 0x800000:
                            val -= 0x1000000
                        samples.append(val)
                else:
                    n = len(data) // sw
                    vals = struct.unpack("<" + fmt * n, data)
                    samples = [v - 128 for v in vals] if not signed else list(vals)
                for v in samples:
                    a = abs(v)
                    if a > peak:
                        peak = a
                    sum_sq += v * v
                    total += 1
                remaining -= nframes_now
            if total == 0:
                return (0.0, 0.0)
            rms = (sum_sq / total) ** 0.5
            peak_db = 20.0 * math.log10(peak / maxv) if peak > 0 else float("-inf")
            rms_db = 20.0 * math.log10(rms / maxv) if rms > 0 else float("-inf")
            return (peak_db, rms_db)
    except Exception as e:
        print(f"  ERROR reading levels {path}: {e}", file=sys.stderr)
        return None


def get_track_levels(path: Path) -> tuple[float, float] | None:
    """Return (peak_dbfs, rms_dbfs) for a WAV.

    Prefers ffmpeg's astats filter (fast, accurate) and falls back to a
    pure-Python reader when ffmpeg is unavailable. Returns None on failure.
    """
    ff = shutil.which("ffmpeg")
    if ff:
        res = _track_levels_ffmpeg(path, ff)
        if res is not None:
            return res
    return _track_levels_python(path)


def detect_clipping(path: Path, min_run: int = 3) -> bool:
    """Return True if the file has samples pinned at the rail for >= min_run
    consecutive samples (a sign of actual clipping / flat-topping), as opposed
    to a single full-scale transient.

    A 0 dBFS *sample peak* is normal for music and is NOT clipping by
    itself; clipping is when the waveform is flattened against the rail.
    """
    try:
        with wave.open(str(path), "rb") as w:
            sw = w.getsampwidth()
            nch = w.getnchannels()
            if sw == 1:
                maxv, fmt, signed = 128, "B", False
            elif sw == 2:
                maxv, fmt, signed = 32768, "h", True
            elif sw == 3:
                maxv, signed = 8388608, True
            elif sw == 4:
                maxv, fmt, signed = 2147483648, "i", True
            else:
                return False
            rail = maxv - 1
            run = 0
            chunk = 4096 * nch
            while True:
                data = w.readframes(chunk)
                if not data:
                    break
                if sw == 3:
                    cnt = len(data) // 3
                    for i in range(cnt):
                        b = data[i * 3:(i + 1) * 3]
                        v = b[0] | (b[1] << 8) | (b[2] << 16)
                        if v & 0x800000:
                            v -= 0x1000000
                        if abs(v) >= rail:
                            run += 1
                            if run >= min_run:
                                return True
                        else:
                            run = 0
                else:
                    n = len(data) // sw
                    vals = struct.unpack("<" + fmt * n, data)
                    if not signed:
                        vals = [v - 128 for v in vals]
                    for v in vals:
                        if abs(v) >= rail:
                            run += 1
                            if run >= min_run:
                                return True
                        else:
                            run = 0
            return False
    except Exception:
        return False


def _mix_with_ffmpeg(input_paths: list[Path], output_path: Path, ffmpeg_path: str) -> bool:
    """Use ffmpeg for high-quality, accurate parallel mix (sum)."""
    output_path = output_path.resolve()
    n = len(input_paths)
    try:
        # Get reference format from first file
        info = get_wav_info(input_paths[0])
        if info is None:
            raise RuntimeError("cannot read first input")

        cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "warning"]
        for p in input_paths:
            cmd += ["-i", str(p)]

        if n == 1:
            # trivial "mix": copy
            cmd += ["-c:a", "copy", str(output_path)]
        else:
            # amix sums after scaling each by 1/n; volume=n undoes it -> raw sum
            # duration=longest to handle minor length diffs from separate dumps
            filt = f"amix=inputs={n}:duration=longest:dropout_transition=0,volume={n}[aout]"
            cmd += [
                "-filter_complex", filt,
                "-map", "[aout]",
                "-c:a", "pcm_s16le",
                "-ar", str(info["framerate"]),
                "-ac", str(info["nchannels"]),
                str(output_path),
            ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  ffmpeg error: {res.stderr.strip()[:300]}", file=sys.stderr)
            if output_path.exists():
                output_path.unlink()
            return False
        return True
    except Exception as e:
        print(f"  ERROR (ffmpeg mix) {output_path}: {e}", file=sys.stderr)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False


def _mix_with_sox(input_paths: list[Path], output_path: Path, sox_path: str) -> bool:
    """Use sox for mix. Tries to produce a straight sum."""
    output_path = output_path.resolve()
    n = len(input_paths)
    try:
        # sox -m mixes; to approximate unnormalized sum we can use -v per input
        # but for simplicity use -m and then scale if needed. Many chiptune uses accept 1/n.
        # To get closer to sum, we use vol compensation post but sox mix applies 1/n.
        # Best-effort: use -m -v 1 for all (but sox normalizes mix). Use pipe or accept.
        # For accuracy we prefer ffmpeg path. Here: straight mix.
        cmd = [sox_path, "-m"]
        for p in input_paths:
            cmd += [str(p)]
        # To counteract default gain reduction in some sox versions, we can post-process but keep simple.
        cmd += ["-b", "16", str(output_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"  sox error: {res.stderr.strip()[:300]}", file=sys.stderr)
            if output_path.exists():
                output_path.unlink()
            return False
        return True
    except Exception as e:
        print(f"  ERROR (sox mix) {output_path}: {e}", file=sys.stderr)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False


def _mix_with_python(input_paths: list[Path], output_path: Path) -> bool:
    """
    Pure stdlib mix: sum samples across all inputs (parallel).
    Streams in chunks. Supports 16-bit PCM (standard for VGMPlay dumps).
    Output is always 16-bit PCM, same rate/channels as inputs. Clips on overflow.
    Longest input determines output length (others zero-padded).
    """
    output_path = output_path.resolve()
    if not input_paths:
        return False

    readers = []
    try:
        # Open all readers
        for p in input_paths:
            w = wave.open(str(p), "rb")
            readers.append(w)

        ref = readers[0].getparams()
        nch = ref.nchannels
        sw = ref.sampwidth
        rate = ref.framerate
        comptype = ref.comptype

        if sw != 2 or comptype != "NONE":
            # Only 16-bit supported in pure path for simplicity and correctness
            raise RuntimeError("pure-Python mixer only supports 16-bit PCM WAVs")

        # Verify all have same basic format (lengths may differ)
        for w in readers:
            p = w.getparams()
            if (p.nchannels, p.sampwidth, p.framerate, p.comptype) != (nch, sw, rate, comptype):
                raise RuntimeError("input format mismatch during mix")

        max_frames = max(w.getnframes() for w in readers)

        with wave.open(str(output_path), "wb") as wav_out:
            wav_out.setparams((nch, sw, rate, max_frames, "NONE", ""))

            chunk_frames = 4096
            for offset in range(0, max_frames, chunk_frames):
                this_chunk = min(chunk_frames, max_frames - offset)
                mixed = [0] * (this_chunk * nch)

                for w in readers:
                    data = w.readframes(this_chunk)
                    got_samps = len(data) // 2
                    if got_samps < this_chunk * nch:
                        # zero pad
                        data += b"\x00" * (this_chunk * nch * 2 - len(data))
                        got_samps = this_chunk * nch
                    vals = struct.unpack("<" + "h" * got_samps, data)
                    for i, v in enumerate(vals):
                        mixed[i] += v

                # clip to 16-bit signed
                clipped = [max(-32768, min(32767, v)) for v in mixed]
                out_bytes = struct.pack("<" + "h" * len(clipped), *clipped)
                wav_out.writeframes(out_bytes)

        return True

    except Exception as e:
        print(f"  ERROR writing {output_path}: {e}", file=sys.stderr)
        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass
        return False
    finally:
        for w in readers:
            try:
                w.close()
            except Exception:
                pass


def mix_wavs(input_paths: list[Path], output_path: Path) -> bool:
    """
    Mix multiple WAVs sample-by-sample (parallel sum).
    Prefers ffmpeg > sox > pure Python.
    Assumes caller verified basic compatibility.
    Returns True on success.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return _mix_with_ffmpeg(input_paths, output_path, ffmpeg)
    sox = shutil.which("sox")
    if sox:
        return _mix_with_sox(input_paths, output_path, sox)
    return _mix_with_python(input_paths, output_path)


def process(
    root: Path,
    output_dir: Path,
    tolerance: float = 0.01,
    dry_run: bool = False,
    verbose: bool = False,
    min_group: int = 2,
) -> None:
    root = root.resolve()
    output_dir = output_dir.resolve()

    print(f"Scanning for WAVs under: {root}")
    all_wavs = find_wav_files(root)
    print(f"Found {len(all_wavs)} WAV file(s).")
    print("Mode: MIX (parallel / channel sum)  -- not sequential concatenation.")

    # Inform about mixer backend (best available = highest fidelity)
    ff = shutil.which("ffmpeg")
    sx = shutil.which("sox")
    if ff:
        print("Using mixer: ffmpeg (best quality)")
    elif sx:
        print("Using mixer: sox")
    else:
        print("Using mixer: pure Python (wave)")

    if not all_wavs:
        print("Nothing to do.")
        return

    groups = group_by_basename(all_wavs)
    multi_name_groups = {name: paths for name, paths in groups.items() if len(paths) >= min_group}

    if not multi_name_groups:
        print("No groups with multiple files sharing the same basename (need files in separate subdirs with identical names).")
        return

    print(f"Found {len(multi_name_groups)} basename group(s) with >= {min_group} files.")

    matches: list[tuple[str, list[Path]]] = []

    for name, paths in sorted(multi_name_groups.items()):
        infos = [get_wav_info(p) for p in paths]
        if any(i is None for i in infos):
            if verbose:
                print(f"  SKIP (unreadable WAV): {name}")
            continue

        sizes = [i["size"] for i in infos]
        if not sizes_within_tolerance(sizes, tolerance):
            if verbose:
                print(f"  SKIP (size diff > {tolerance*100:.1f}%): {name}")
                for p, s in zip(paths, sizes):
                    print(f"    - {p} ({s} bytes)")
            continue

        durs = [i["duration"] for i in infos]
        # Slightly looser duration tolerance (size is proxy but duration more direct)
        if not durations_within_tolerance(durs, max(tolerance * 2, 0.02)):
            if verbose:
                print(f"  SKIP (duration diff too large): {name}")
                for p, d in zip(paths, durs):
                    print(f"    - {p} ({d:.2f}s)")
            continue

        if not formats_compatible(paths):
            if verbose:
                print(f"  SKIP (incompatible WAV formats): {name}")
            continue

        # Avoid re-including a previously produced mix that lives at the output location.
        # (Common when outputs are written into the scanned root.)
        prospective_out = output_dir / name
        source_paths = [p for p in paths if p.resolve() != prospective_out.resolve()]

        if len(source_paths) < min_group:
            if verbose:
                print(f"  SKIP (would mix fewer than {min_group} after excluding prior output): {name}")
            continue

        # Sort for deterministic order (by full path string)
        sorted_paths = sorted(source_paths)
        matches.append((name, sorted_paths))

    if not matches:
        print("No matching groups passed size + format checks.")
        return

    print(f"\n{len(matches)} matching group(s) will be mixed:")
    for name, paths in matches:
        print(f"  {name}  ({len(paths)} files)")
        if verbose:
            for p in paths:
                print(f"    {p}")

    if dry_run:
        print("\n[DRY RUN] No files written.")
        for name, paths in matches:
            stem = Path(name).stem
            out_name = f"{stem}.wav"
            out_path = output_dir / out_name
            total_size = sum(p.stat().st_size for p in paths)
            print(f"  Would create: {out_path}  (mix of {len(paths)} sources -> approx {total_size} bytes)")
        return

    # Real run
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0

    for name, paths in matches:
        stem = Path(name).stem
        out_name = f"{stem}.wav"
        out_path = output_dir / out_name

        if out_path.exists():
            print(f"  SKIP existing output: {out_path}")
            skipped += 1
            continue

        print(f"  Mixing ({len(paths)} sources) -> {out_path}")
        ok = mix_wavs(paths, out_path)
        if ok:
            written += 1
            if verbose:
                print(f"    Wrote {out_path} from {len(paths)} files")
        else:
            skipped += 1

    print(f"\nDone. Mixed {written} file(s). Skipped {skipped}.")


def audio_check(workdir: Path, verbose: bool = False) -> None:
    """Analyze peak and RMS levels of the *final* tracks in `workdir`.

    Scans only top-level WAVs (not subdirectory sources) and prints a table plus
    concise recommendations so a collection plays back at consistent, comfortable
    loudness without clipping or ear-splitting transients.

    Recommendations (concise; only about peak headroom / clipping):
      - CLIPPED (samples pinned at the rail, see Clip column) -> the audio is
        already damaged; reduce gain / apply a limiter at the SOURCE and
        re-export. Do not just normalize.
      - Any other track peaking above AUDIO_CHECK_TARGET_PEAK_DBFS -> loud but
        not clipping; summarized in ONE line (with the hottest peak) rather than
        nagged per track. For uniform headroom, apply a collection-wide limiter
        at the target. Do NOT flat-cut individual tracks (would crush RMS).
    Per-track RMS and cross-track averages are shown for reference only; they do
    not generate volume-change advice (not meaningful across mixed content such as
    SFX vs music).
    Note: "Peak" is the sample (true) peak in dBFS. Editors' volume meters
    usually show RMS/averaged loudness, so a -8 dB meter with a 0 dBFS peak
    (crest ~8 dB) is normal for music.
    """
    workdir = workdir.resolve()
    print(f"\n=== Audio check (top-level WAVs in {workdir}) ===")
    wavs = find_top_level_wavs(workdir)
    if not wavs:
        print("No top-level WAV files found to analyze.")
        return

    results: list[tuple[Path, float, float, bool]] = []
    for p in wavs:
        levels = get_track_levels(p)
        if levels is None:
            print(f"  SKIP (cannot analyze): {p.name}")
            continue
        peak_db, rms_db = levels
        clipped = detect_clipping(p)
        results.append((p, peak_db, rms_db, clipped))
        if verbose:
            print(f"  {p.name}: peak {peak_db:+.1f} dB, RMS {rms_db:+.1f} dB"
                  f"{' [CLIPPED]' if clipped else ''}")

    if not results:
        print("No analyzable WAV files found.")
        return

    peaks = [r[1] for r in results]
    rmss = [r[2] for r in results]
    avg_peak = sum(peaks) / len(peaks)
    avg_rms = sum(rmss) / len(rmss)

    print(f"\n{'Track':<38} {'Peak':>8} {'RMS':>8} {'Crest':>8} {'Clip':>6}")
    print("-" * 68)
    for p, peak_db, rms_db, clipped in results:
        crest = peak_db - rms_db
        name = p.name if len(p.name) <= 36 else p.name[:33] + "..."
        flag = "YES" if clipped else ""
        print(f"{name:<38} {peak_db:>+7.1f} {rms_db:>+7.1f} {crest:>+7.1f} {flag:>6}")
    print("-" * 68)
    print(f"{'AVERAGE':<38} {avg_peak:>+7.1f} {avg_rms:>+7.1f}")

    print(
        "\nNote: 'Peak' is the SAMPLE (true) peak in dBFS; 0.0 = a sample hit\n"
        "the rail. Editors' meters show RMS / peak-hold (smoothed), which is why\n"
        "they read ~-8..-10 dB while a track here shows 0.0 dBFS. A 0 dBFS\n"
        "peak from a single transient sample is NOT clipping (Clip = NO) and is\n"
        "inaudible. 'Clip' = YES means samples are pinned at the rail (real damage)."
    )

    print("\nRecommendations:")
    recs: list[str] = []
    loud: list[tuple[str, float]] = []  # (name, peak_db) above target, not clipped
    for p, peak_db, rms_db, clipped in results:
        if clipped:
            # Samples are pinned at the rail -> the audio is already damaged.
            recs.append(
                f"  - {p.name}: CLIPPED at 0 dBFS (samples pinned at the "
                f"rail) -> the audio is already damaged. Reduce gain / apply a "
                f"limiter at the SOURCE and re-export; do not just normalize."
            )
        elif peak_db > AUDIO_CHECK_TARGET_PEAK_DBFS:
            # Loud but not clipping: noted for the summary, not nagged per track.
            loud.append((p.name, peak_db))

    if recs:
        seen: set[str] = set()
        for r in recs:
            if r not in seen:
                seen.add(r)
                print(r)
    if loud:
        hottest = max(peak_db for _, peak_db in loud)
        print(
            f"  - {len(loud)} track(s) peak above {AUDIO_CHECK_TARGET_PEAK_DBFS:+.1f} "
            f"dBFS (loud but not clipping; hottest at {hottest:+.1f} dBFS). If you "
            f"want more headroom, apply a collection-wide limiter at "
            f"{AUDIO_CHECK_TARGET_PEAK_DBFS:+.1f} dBFS (see table for which)."
        )
    if not recs and not loud:
        print("  All tracks within tolerance. No changes needed.")


def prompt_audio_check() -> bool:
    """Ask at startup whether to run the audio check. Accepts y/n/1/0/yes/no."""
    while True:
        try:
            ans = input("Audio check? [y/n/1/0]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans in ("y", "yes", "1"):
            return True
        if ans in ("n", "no", "0", ""):
            return False
        print("  Please answer y / n / 1 / 0.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mix matching WAV files (same basename + ~size) from subdirectories into one WAV via parallel sum (for per-channel dumps).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python combine_matching_wavs.py
  python combine_matching_wavs.py . --dry-run -v
  python combine_matching_wavs.py "VGMPlay_052-0/old/Metal_Gear_2_-_Solid_Snake_(MSX2)" -t 0.02 -v
  python combine_matching_wavs.py /data/rips --tolerance 0.05 -v -o ./out

The script finds groups of files that share an *exact* filename (e.g. "01 Song.wav")
located in different subdirectories. It then mixes them together sample-by-sample.
This is the correct way to recombine individual channel WAVs (or layer passes)
produced by multiple VGMPlay runs with different muting into a final full mix.
""",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Root directory to scan (default: current directory)",
    )
    parser.add_argument(
        "-t",
        "--tolerance",
        type=float,
        default=0.01,
        help="Size tolerance as fraction (default: 0.01 = 1%%)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="Directory to write the mixed WAV(s) (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print more details (skipped groups, file lists)",
    )
    parser.add_argument(
        "--min-group",
        type=int,
        default=2,
        help="Minimum files with same name to consider a group (default: 2)",
    )
    parser.add_argument(
        "-ac",
        "--audio-check",
        action="store_true",
        help="Run the audio-level check on the final (top-level) WAVs after combining",
    )

    args = parser.parse_args()

    # Determine whether to run the audio check: explicit flag, else prompt at
    # startup. The check runs after the combine pipeline and works even when
    # there is nothing to combine (it analyzes existing top-level WAVs).
    do_check = args.audio_check
    if not do_check:
        do_check = prompt_audio_check()

    root = Path(args.root)
    output_dir = Path(args.output_dir)

    if not root.exists() or not root.is_dir():
        print(f"Error: root is not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    process(
        root=root,
        output_dir=output_dir,
        tolerance=args.tolerance,
        dry_run=args.dry_run,
        verbose=args.verbose,
        min_group=args.min_group,
    )

    if do_check:
        audio_check(output_dir, verbose=args.verbose)


if __name__ == "__main__":
    main()
