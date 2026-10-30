import os
import json
import csv
import datetime
from pathlib import Path
from mutagen import File as MutagenFile

# Root of your music library, structured as ROOT/Artist/Album/song.ext
# Uses the directory the script is run from (not the script's own location).
ROOT = Path.cwd()

# Which extensions to process
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac"}

# If True, just report what WOULD change without writing anything
DRY_RUN = True

# Tracks files already confirmed correct/updated across runs, so re-runs can
# skip them entirely without re-opening and re-reading tags. Stored in ROOT.
STATE_FILE = ROOT / ".artist_tag_fix_state.json"

# Simple append-only CSV log of files that have errored out (typically
# corruption). This is NOT consulted by the script — it's just a running
# record for you to review, e.g. in Excel/Numbers/Google Sheets. Corrupt
# files are still re-attempted every run.
CORRUPT_LOG_PATH = ROOT / "corrupt_files.csv"
CORRUPT_LOG_HEADERS = ["timestamp", "file_name", "file_path", "error"]

LOG_PATH = Path(f"artist_tag_fixes_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")


def load_state(state_file: Path) -> dict[str, float]:
    """
    Returns a dict of {relative_path_str: last_modified_time} for files
    already confirmed correct/updated in a previous (non-dry) run.
    """
    if not state_file.exists():
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # corrupt or unreadable state file — treat as empty rather than crash
        return {}


def save_state(state_file: Path, state: dict[str, float]) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def ensure_corrupt_log_header(csv_path: Path) -> None:
    """Creates the CSV file with a header row if it doesn't exist yet."""
    if not csv_path.exists():
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CORRUPT_LOG_HEADERS)


def get_artist_from_path(file_path: Path, root: Path) -> str | None:
    """
    Given ROOT/Artist/Album/song.ext, return 'Artist'.
    Also handles multi-disc albums, e.g. ROOT/Artist/Album/CD1/song.ext —
    any extra folder depth beyond Artist/Album is ignored since only the
    first folder (the artist) is used.

    Returns None if the file isn't at least Artist/Album/song deep
    (i.e. there's no clear artist folder to infer from).
    """
    rel_path = file_path.relative_to(root)
    parts = rel_path.parts

    if len(parts) < 3:
        # not enough folder depth (need at least Artist/Album/song.ext)
        return None

    return parts[0]


def update_tags(file_path: Path, artist_name: str, dry_run: bool) -> tuple[bool, str]:
    """
    Returns (changed, message)
    """
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None:
            return (False, "Could not parse file (unsupported/corrupt)")

        if audio.tags is None:
            audio.add_tags()

        current_artist = audio.tags.get("artist", [None])[0]
        current_albumartist = audio.tags.get("albumartist", [None])[0]

        needs_artist_update = current_artist != artist_name
        needs_albumartist_update = current_albumartist != artist_name

        if not needs_artist_update and not needs_albumartist_update:
            return (False, "Already correct")

        msg_parts = []
        if needs_artist_update:
            msg_parts.append(f"artist: '{current_artist}' -> '{artist_name}'")
        if needs_albumartist_update:
            msg_parts.append(f"albumartist: '{current_albumartist}' -> '{artist_name}'")

        if not dry_run:
            audio["artist"] = artist_name
            audio["albumartist"] = artist_name
            audio.save()

        return (True, "; ".join(msg_parts))

    except Exception as e:
        return (False, f"ERROR: {e}")


def main():
    audio_files = [
        p for p in ROOT.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    print(f"Found {len(audio_files)} audio files under {ROOT}")

    state = load_state(STATE_FILE)
    print(f"Loaded state file with {len(state)} previously-processed entries")

    ensure_corrupt_log_header(CORRUPT_LOG_PATH)

    if DRY_RUN:
        print("DRY RUN — no files will be modified, and the state file will NOT be updated.\n")
    else:
        print("LIVE RUN — tags will be written to disk, and the state file will be updated.\n")

    changed_count = 0
    skipped_no_structure = 0
    skipped_already_processed = 0
    unchanged_count = 0
    error_count = 0

    with open(LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(f"Run started: {datetime.datetime.now()}\n")
        log_file.write(f"Root: {ROOT}\n")
        log_file.write(f"Dry run: {DRY_RUN}\n")
        log_file.write("=" * 80 + "\n\n")

        for i, file_path in enumerate(audio_files, 1):
            rel_path_str = str(file_path.relative_to(ROOT))
            mtime = file_path.stat().st_mtime

            # Skip files already processed in a prior LIVE run, unless the
            # file has changed since then (e.g. re-tagged by another tool),
            # in which case the mtime won't match and it gets reprocessed.
            if rel_path_str in state and state[rel_path_str] == mtime:
                skipped_already_processed += 1
                continue

            artist_name = get_artist_from_path(file_path, ROOT)

            if artist_name is None:
                skipped_no_structure += 1
                line = f"[{i}/{len(audio_files)}] SKIPPED (no Artist/Album structure): {file_path}"
                print(line)
                log_file.write(line + "\n")
                continue

            changed, message = update_tags(file_path, artist_name, DRY_RUN)

            if message.startswith("ERROR"):
                error_count += 1
                line = f"[{i}/{len(audio_files)}] ERROR: {file_path} — {message}"
                print(line)
                log_file.write(line + "\n")
                # append a row to the standalone corrupt-files CSV for easy review
                with open(CORRUPT_LOG_PATH, "a", newline="", encoding="utf-8") as corrupt_log:
                    writer = csv.writer(corrupt_log)
                    writer.writerow([
                        f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
                        file_path.name,
                        str(file_path),
                        message,
                    ])
                continue
            elif changed:
                changed_count += 1
                line = f"[{i}/{len(audio_files)}] {'WOULD UPDATE' if DRY_RUN else 'UPDATED'}: {file_path} — {message}"
                print(line)
                log_file.write(line + "\n")
            else:
                unchanged_count += 1
                log_file.write(f"[{i}/{len(audio_files)}] OK (already correct): {file_path}\n")

            if not DRY_RUN:
                # re-read mtime, since audio.save() updates the file's modified time
                state[rel_path_str] = file_path.stat().st_mtime

        log_file.write("\n" + "=" * 80 + "\n")
        log_file.write(f"Run finished: {datetime.datetime.now()}\n")
        log_file.write(f"Changed: {changed_count}\n")
        log_file.write(f"Already correct: {unchanged_count}\n")
        log_file.write(f"Skipped (no structure): {skipped_no_structure}\n")
        log_file.write(f"Skipped (already processed, from state file): {skipped_already_processed}\n")
        log_file.write(f"Errors: {error_count}\n")

    if not DRY_RUN:
        save_state(STATE_FILE, state)

    print(f"\nDone.")
    print(f"  {'Would change' if DRY_RUN else 'Changed'}: {changed_count}")
    print(f"  Already correct: {unchanged_count}")
    print(f"  Skipped (no Artist/Album structure): {skipped_no_structure}")
    print(f"  Skipped (already processed previously): {skipped_already_processed}")
    print(f"  Errors: {error_count}")
    print(f"\nFull log: {LOG_PATH.resolve()}")
    if error_count > 0:
        print(f"Errors appended to: {CORRUPT_LOG_PATH.resolve()}")

    if DRY_RUN and changed_count > 0:
        print("\nThis was a dry run. Set DRY_RUN = False at the top of the script and re-run to apply these changes.")


if __name__ == "__main__":
    main()
