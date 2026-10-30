import av
import os
import csv
import json
import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4Cover

SRC_ROOT = Path("source directory here")
DST_ROOT = Path("output directory here")
BITRATE = 196_000
MIN_VALID_SIZE = 1024

AAC_VALID_RATES = [
    8000, 11025, 12000, 16000, 22050, 24000, 32000,
    44100, 48000, 64000, 88200, 96000
]

# Tracks output files already validated as structurally complete, so
# re-runs can skip re-opening/re-checking them. Keyed by relative path
# under DST_ROOT, storing (mtime, size) so a file that's changed since
# it was last validated gets re-checked rather than blindly trusted.
STATE_FILE = DST_ROOT / ".conversion_validation_state.json"

# Persistent, append-only CSV log of conversion failures. Accumulates across
# every run rather than creating a new timestamped file each time — matches
# the corrupt_files.csv pattern used in the tag-fixing script.
LOG_PATH = Path("conversion_failures.csv")
LOG_HEADERS = ["timestamp", "file_name", "source_path", "error_type", "message"]


def ensure_log_header(csv_path: Path) -> None:
    """Creates the CSV file with a header row if it doesn't exist yet."""
    if not csv_path.exists():
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LOG_HEADERS)


def load_state(state_file: Path) -> dict[str, list]:
    if not state_file.exists():
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_file: Path, state: dict[str, list]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def pick_aac_rate(source_rate: int) -> int:
    if source_rate in AAC_VALID_RATES:
        return source_rate
    return min(AAC_VALID_RATES, key=lambda r: abs(r - source_rate))


def pick_cover_picture(flac_pictures):
    """
    Given a list of mutagen FLAC Picture objects, pick the best one to use
    as the embedded cover. Prefers type 3 (front cover, the standard tag
    for this) if present, otherwise falls back to the first picture found.
    """
    if not flac_pictures:
        return None
    for pic in flac_pictures:
        if pic.type == 3:  # "Cover (front)"
            return pic
    return flac_pictures[0]


def copy_cover_art(src_path: Path, dst_path: Path) -> str:
    """
    Extracts the embedded cover picture (if any) from the source FLAC and
    embeds it into the destination M4A's cover atom. Returns a short status
    string for logging; never raises — a missing/failed cover shouldn't be
    treated as a failed conversion, since the audio itself is still fine.
    """
    try:
        flac_audio = FLAC(src_path)
        picture = pick_cover_picture(flac_audio.pictures)

        if picture is None:
            return "no cover art in source"

        if picture.mime in ("image/jpeg", "image/jpg"):
            cover_format = MP4Cover.FORMAT_JPEG
        elif picture.mime == "image/png":
            cover_format = MP4Cover.FORMAT_PNG
        else:
            return f"skipped (unsupported cover mime type: {picture.mime})"

        mp4_audio = MP4(dst_path)
        mp4_audio["covr"] = [MP4Cover(picture.data, imageformat=cover_format)]
        mp4_audio.save()

        return "cover art copied"

    except Exception as e:
        return f"cover art failed: {e}"


def convert_flac_to_aac(src_path: Path, dst_path: Path) -> tuple[str, bool, str]:
    # printed from the worker process the moment it picks up this file, so
    # you can see what's actively being worked on, not just what's finished
    print(f"Converting: {src_path}", flush=True)

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        in_container = av.open(str(src_path))
        out_container = av.open(str(dst_path), mode="w")

        in_stream = in_container.streams.audio[0]
        target_rate = pick_aac_rate(in_stream.rate)

        # FFmpeg's native "aac" encoder doesn't expose a clean quality-based
        # VBR mode through PyAV's simple API (that's specific to libfdk_aac,
        # which most FFmpeg builds omit for licensing reasons). Setting
        # bit_rate targets an average of ~196kbps while the encoder still
        # varies bit allocation frame-to-frame based on audio complexity —
        # the practical benefit of VBR, just not labeled as such internally.
        out_stream = out_container.add_stream("aac", rate=target_rate)
        out_stream.bit_rate = BITRATE

        layout = "stereo" if in_stream.layout.name != "mono" else "mono"

        # FFmpeg's native AAC encoder expects planar float samples
        resampler = av.AudioResampler(
            format="fltp",
            layout=layout,
            rate=target_rate,
        )

        if in_container.metadata:
            out_container.metadata.update(in_container.metadata)

        for frame in in_container.decode(in_stream):
            frame.pts = None
            resampled_frames = resampler.resample(frame)
            if resampled_frames is None:
                continue
            if not isinstance(resampled_frames, list):
                resampled_frames = [resampled_frames]
            for rframe in resampled_frames:
                for packet in out_stream.encode(rframe):
                    out_container.mux(packet)

        for rframe in resampler.resample(None):
            for packet in out_stream.encode(rframe):
                out_container.mux(packet)

        for packet in out_stream.encode(None):
            out_container.mux(packet)

        out_container.close()
        in_container.close()

        cover_status = copy_cover_art(src_path, dst_path)

        return (str(src_path), True, f"OK ({cover_status})")

    except av.error.InvalidDataError as e:
        # leave the partial output file in place for inspection — it will
        # simply be overwritten (mode="w") if this file is retried later
        return (str(src_path), False, f"CORRUPT_SOURCE: {e}")

    except Exception as e:
        return (str(src_path), False, str(e))


def validate_output_file(path: Path) -> tuple[str, bool]:
    """
    Runs in a worker process. Confirms the file is a structurally complete,
    readable MP4/AAC container — not just "exists and is over 1KB".

    Demuxes every packet (reading the container structure) rather than fully
    decoding audio, which is enough to catch truncated files (missing moov
    atom, cut-off streams, etc.) without the cost of a full audio decode.
    """
    try:
        if path.stat().st_size < MIN_VALID_SIZE:
            return (str(path), False)

        container = av.open(str(path))
        if not container.streams.audio:
            container.close()
            return (str(path), False)

        packet_count = 0
        for packet in container.demux(container.streams.audio[0]):
            packet_count += 1

        container.close()

        return (str(path), packet_count > 0)

    except Exception:
        return (str(path), False)


def build_existing_output_set(dst_root: Path) -> set[Path]:
    """
    Scans dst_root for existing .m4a files. Files already recorded in the
    state file as valid (and unchanged since then) are trusted without
    re-opening them. Everything else gets validated fresh; invalid files
    are left on disk (not deleted) but excluded from the trusted set, so
    they'll be picked up for reconversion and overwritten.
    """
    existing = set()
    if not dst_root.exists():
        return existing

    candidates = list(dst_root.rglob("*.m4a"))
    if not candidates:
        return existing

    state = load_state(STATE_FILE)
    to_validate = []
    trusted_count = 0

    for path in candidates:
        rel_str = str(path.relative_to(dst_root))
        stat = path.stat()
        current = [stat.st_mtime, stat.st_size]

        if rel_str in state and state[rel_str] == current:
            # unchanged since last validated as good — trust it, skip re-opening
            existing.add(path.relative_to(dst_root))
            trusted_count += 1
        else:
            to_validate.append(path)

    if trusted_count:
        print(f"Trusting {trusted_count} file(s) already validated in a previous run")

    if to_validate:
        print(f"Validating {len(to_validate)} new/changed file(s)...")
        num_workers = os.cpu_count() or 1
        invalid_count = 0

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(validate_output_file, p): p for p in to_validate}

            for future in as_completed(futures):
                path_str, is_valid = future.result()
                path = Path(path_str)
                rel_str = str(path.relative_to(dst_root))

                if is_valid:
                    existing.add(path.relative_to(dst_root))
                    stat = path.stat()
                    state[rel_str] = [stat.st_mtime, stat.st_size]
                else:
                    # left on disk for inspection — just excluded from the
                    # trusted set, so it'll be re-converted (and overwritten)
                    print(f"Invalid/partial file (left on disk): {path}")
                    state.pop(rel_str, None)
                    invalid_count += 1

        if invalid_count:
            print(f"Found {invalid_count} invalid/partial existing file(s) — left in place, will be overwritten on reconversion")

        save_state(STATE_FILE, state)

    return existing


def main():
    flac_files = list(SRC_ROOT.rglob("*.flac"))
    print(f"Found {len(flac_files)} FLAC files in source")

    print("Checking existing output files (trusting previously-validated ones)...")
    existing_outputs = build_existing_output_set(DST_ROOT)
    print(f"Found {len(existing_outputs)} valid existing AAC files")

    jobs = []
    skipped = 0
    for src_path in flac_files:
        rel_path = src_path.relative_to(SRC_ROOT)
        rel_out_path = rel_path.with_suffix(".m4a")

        if rel_out_path in existing_outputs:
            skipped += 1
            continue

        dst_path = DST_ROOT / rel_out_path
        jobs.append((src_path, dst_path))

    print(f"Skipping {skipped} already-converted files, {len(jobs)} to convert")
    print(f"Failures will be appended to: {LOG_PATH.resolve()}")

    if not jobs:
        print("Nothing to do.")
        return

    num_workers = os.cpu_count() or 1
    print(f"Using {num_workers} worker processes")

    failures = []
    state = load_state(STATE_FILE)
    ensure_log_header(LOG_PATH)

    with open(LOG_PATH, "a", encoding="utf-8", newline="") as log_file:
        csv_writer = csv.writer(log_file)

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(convert_flac_to_aac, src, dst): (src, dst)
                for src, dst in jobs
            }

            completed = 0
            for future in as_completed(futures):
                src_path, success, message = future.result()
                completed += 1
                if not success:
                    failures.append((src_path, message))
                    error_type = message.split(":", 1)[0] if ":" in message else "ERROR"
                    csv_writer.writerow([
                        datetime.datetime.now().isoformat(timespec="seconds"),
                        Path(src_path).name,
                        src_path,
                        error_type,
                        message,
                    ])
                    log_file.flush()  # write immediately so partial logs survive a crash
                    print(f"[{completed}/{len(jobs)}] FAILED: {src_path} — {message}")
                else:
                    print(f"[{completed}/{len(jobs)}] OK: {src_path}")
                    # record the new output in state so it's trusted on the next run
                    dst_path = futures[future][1]
                    if dst_path.exists():
                        stat = dst_path.stat()
                        rel_str = str(dst_path.relative_to(DST_ROOT))
                        state[rel_str] = [stat.st_mtime, stat.st_size]

    save_state(STATE_FILE, state)

    print(f"\nDone. {len(jobs) - len(failures)} succeeded, {len(failures)} failed, {skipped} skipped (already existed).")
    if failures:
        print(f"\n{len(failures)} failures appended to: {LOG_PATH.resolve()}")


if __name__ == "__main__":
    main()