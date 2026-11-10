## Overview

Five scripts cover the library pipeline: get music into Lidarr, identify and sort it, normalise artist tags, strip the FLAC tree down to audio, and build an AAC copy. All run on the Debian Bookworm server; the FLAC and AAC libraries live in separate folders.

| Script | Language | What it does | Changes files? | Safe preview |
| --- | --- | --- | --- | --- |
| `lidarr_add_artists.py` | Python 3.7+, stdlib only | Adds artists from a CSV to Lidarr and starts album searches | No (Lidarr only) | `--dry-run` |
| `tagging.py` | Python 3.9+ | Identifies tracks via MusicBrainz/AcoustID, retags, embeds cover art, moves into `Artist/Album/` | Yes: retags and moves, deletes originals | `--dry-run`, `--keep-originals` |
| `fix_artist_tags.py` | Python 3.10+ | Sets `artist` and `albumartist` tags to the top-level folder name | Yes: rewrites tags in place | `DRY_RUN = True` (default) |
| `deletenonflac.sh` | Bash | Deletes every non-`.flac` file and any folder left with no FLAC | Yes: permanent delete | Runs as a dry run unless `--delete` is passed |
| `flac_to_aac.py` | Python 3.9+ | Transcodes `/root/flac` to \~196 kbps AAC in `/root/compressed`, copies tags and cover | Writes new files only | None; skips already-valid outputs |

Typical order for new music:

1. `lidarr_add_artists.py` to queue downloads.
2. `tagging.py` on the new-music folder, then the duplicate-removal script (not in this project yet), then move into the main library.
3. `fix_artist_tags.py` if folder names and artist tags disagree.
4. `deletenonflac.sh` on the FLAC tree to clear logs, cue sheets, images and lossy strays.
5. `flac_to_aac.py` to bring the AAC copy up to date.

Shared Python packages: `mutagen` (all tag work), `av` / PyAV (conversion), `musicbrainzngs`, `pyacoustid` and `requests` (tagging). `fpcalc` from `libchromaprint-tools` is needed for AcoustID fingerprinting.

## lidarr\_add\_artists.py

Adds every artist in a CSV to Lidarr and, by default, starts a search for their missing albums, pausing 5 minutes between adds so the indexers and download client aren't flooded.

**Requirements:** Python 3.7+, no third-party packages. Lidarr reachable over HTTP and an API key (Lidarr > Settings > General). At least one root folder, quality profile and metadata profile must already exist in Lidarr.

**CSV format** (header row optional; it's skipped if column 1 reads `artist`, `name`, `artist name` or `artistname`):

```csv
artist,mbid
Mogwai,
Boards of Canada,
Explosions in the Sky,a3cb23fc-acd3-4ce0-8f36-1e5aa6a18432
```

Column 2 is an optional MusicBrainz artist ID. When present it's used for an exact lookup and the name is ignored. The file is read as UTF-8, falling back to cp1252 then latin-1 (Excel re-saves), with a note printed if it wasn't UTF-8.

**Usage:**

```
export LIDARR_URL=http://localhost:8686
export LIDARR_API_KEY=your_api_key
python3 lidarr_add_artists.py artists.csv --dry-run
python3 lidarr_add_artists.py artists.csv
```

| Option | Default | Effect |
| --- | --- | --- |
| `csv_file` | required | Path to the CSV |
| `--url` | `$LIDARR_URL` or `http://localhost:8686` | Lidarr base URL |
| `--api-key` | `$LIDARR_API_KEY` | API key; exits if missing |
| `--root-folder` | first root folder in Lidarr | Where artists are stored |
| `--quality-profile` | first profile | Quality profile, by name (case-insensitive) |
| `--metadata-profile` | first profile | Metadata profile, by name |
| `--monitor` | `all` | Albums to monitor: `all`, `future`, `missing`, `existing`, `latest`, `first`, `none` |
| `--no-search` | off | Add artists without searching for albums |
| `--loose` | off | Accept the top search result when no name matches exactly |
| `--delay` | `300` | Seconds to wait after each successful add (not after skips/fails or the last artist) |
| `--dry-run` | off | Look up every artist and report, add nothing |

**How it matches:** with an MBID it looks up `lidarr:<mbid>`. Otherwise it searches by name and takes the first result whose name matches exactly (case-insensitive). With no exact match it fails the row and lists the top three results, unless `--loose` is set, in which case it takes the first result.

**Output:** each row prints `[ OK ]`, `[SKIP]` (already in Lidarr, by MusicBrainz ID), `[DRY ]` or `[FAIL]`, then a summary. Failures are written to `lidarr_failed.csv` (artist, mbid, reason) in the current directory; fix the names or add MBIDs and re-run that file. Already-added artists are skipped on re-runs, so re-running the full list is safe.

New artists are added with `monitorNewItems: all`, so future releases are monitored regardless of `--monitor`.

## tagging.py

Identifies each track against MusicBrainz, rewrites its tags, embeds cover art, and files it as `<dest>/<Album Artist>/<Album>/<NN - Title>.<ext>`. Originals are deleted after a successful copy-and-tag unless `--keep-originals` is set. (The file's own docstring calls it `music_organizer.py`.)

**Formats:** mp3, flac, m4a/mp4, ogg, oga, opus.

**Setup (Debian Bookworm):**

```
sudo apt install libchromaprint-tools python3-venv
python3 -m venv ~/.venvs/musicorg && . ~/.venvs/musicorg/bin/activate
pip install mutagen pyacoustid musicbrainzngs requests
export ACOUSTID_API_KEY=xxxxxxxx   # free key: acoustid.org/new-application
```

**Usage:**

```
./tagging.py /music/unsorted /music/sorted --contact you@example.com --dry-run
./tagging.py /music/unsorted /music/sorted --contact you@example.com
```

| Option | Default | Effect |
| --- | --- | --- |
| `source` | required | Folder scanned recursively |
| `dest` | required | Root of the sorted library (files already under it are ignored) |
| `--contact` | required | Email or URL for the MusicBrainz User-Agent |
| `--acoustid-key` | `$ACOUSTID_API_KEY` | Enables the fingerprint fallback; without it step 3 is skipped |
| `--dry-run` | off | Print the planned moves, change nothing |
| `--keep-originals` | off | Copy instead of move |
| `--no-cover` | off | Don't embed cover art |
| `--min-score` | `0.8` | Minimum AcoustID match score (0–1) |
| `--artist-folder` | `albumartist` | Top-level folder from album artist or track artist |
| `--year-in-album` | off | Name album folders `Album (Year)` |

**How a file is identified.** Files are handled one folder at a time so an album stays together:

1. **Album search** (`album-search`): the folder's most common album and album-artist tags are searched on MusicBrainz (score ≥ 85, top 5). Each candidate release is matched track by track on title similarity ≥ 0.85, with a small bonus for matching track numbers. The release that matches the most files wins; official releases break ties.
2. **Recording search** (`track-search`): each still-unmatched file is searched as a single recording (score ≥ 90) using its title, artist and album, and checked against up to three of that recording's releases.
3. **AcoustID** (`acoustid`): remaining files are fingerprinted with `fpcalc`. Their candidate releases are pooled and voted on; the top four are scored on vote count, title similarity to the folder's album tag, official status and track-count fit. The best release is used for every file it matches.

When tags are missing, the filename is used: a leading track number is stripped and `Artist - Title` is split. Anything still unidentified is left in place and listed in `<dest>/unmatched.txt`.

**Tags written:** title, artist, album, album artist, track and disc number with totals, date, label, and MusicBrainz album, release-group, track and recording IDs. MP3s are saved as ID3v2.3. MP4 files get the recording and album IDs only, and no label.

**Cover art:** the Cover Art Archive (release 1200px, then release group 1200px, then release 500px), falling back to a `cover`, `folder`, `front` or `album` .jpg/.jpeg/.png next to the file.

**Naming:** multi-disc tracks are numbered `D-NN`. Unsafe characters are replaced with `_`; folder names are capped at 120 characters and filenames at 150. A name clash gets `  (2) `, `  (3) ` and so on.

**Safety:** each file is copied to its destination and tagged there. If tagging fails the copy is removed and the original is untouched. The original is deleted only after tagging succeeds.

**Speed:** MusicBrainz's public API allows about one request per second, and AcoustID lookups are throttled to about three per second, so large folders are slow. Release lookups and covers are cached for the run.

## fix\_artist\_tags.py

Sets both the `artist` and `albumartist` tags of every audio file to the name of its top-level artist folder, so tags match a `Artist/Album/track` layout. It runs as a dry run until you edit the script.

**Requirements:** Python 3.10+ (uses `str | None` type syntax) and `mutagen`.

**Usage:** `cd` into the library root, then run it. The root is the current directory, not the script's location.

```
cd /path/to/library
python3 /path/to/fix_artist_tags.py        # dry run
# edit DRY_RUN = False at the top, then:
python3 /path/to/fix_artist_tags.py        # writes tags
```

**Settings** (constants at the top of the file):

| Setting | Default | Effect |
| --- | --- | --- |
| `ROOT` | current directory | Library root, laid out as `ROOT/Artist/Album/song.ext` |
| `AUDIO_EXTENSIONS` | `.mp3 .flac .m4a .aac` | Files processed |
| `DRY_RUN` | `True` | Report changes without writing; the state file isn't updated |
| `STATE_FILE` | `ROOT/.artist_tag_fix_state.json` | Files already checked, keyed by path and modified time |
| `CORRUPT_LOG_PATH` | `ROOT/corrupt_files.csv` | Running log of files that errored |

**What it does per file:**

1. Skips it if the state file has the same path with the same modified time (checked in an earlier live run and unchanged since).
2. Takes the first folder under `ROOT` as the artist. Files fewer than two folders deep are skipped. Deeper layouts such as `Artist/Album/CD1/` work, since only the first folder is used.
3. Compares `artist` and `albumartist` with the folder name and rewrites both if either differs. A file with no tags gets a new tag block.

**Output:**

- Console lines for each update, skip and error, then totals.
- A full per-run log, `artist_tag_fixes_YYYYMMDD_HHMMSS.log`, in the current directory, including files that were already correct.
- Errors (usually corrupt or unreadable files) are appended to `corrupt_files.csv` with timestamp, file name, path and error. This file is a record only; errored files are retried every run and never added to the state file.

**Watch out:** the folder name replaces the track artist on every file. Featured-artist credits (`Artist feat. X`) and per-track artists on compilations are overwritten. Run the dry run first and check the log.

## deletenonflac.sh

Permanently deletes every file and symlink under a directory whose name doesn't end in `.flac` (case-insensitive), then removes any folders left empty. Without `--delete` it only lists what would go.

**Requirements:** Bash and GNU `find` (uses `-printf`, `-delete`, `-iname`).

**Usage:**

```
./deletenonflac.sh /root/flac            # dry run: list files and folders
./deletenonflac.sh /root/flac --delete   # delete them
```

**What gets deleted:** everything that isn't `.flac`, including cover images (`cover.jpg`, `folder.png`), `.cue` sheets, `.log` rip logs, `.m3u` playlists, lyrics files, hidden files such as `.artist_tag_fix_state.json` and `corrupt_files.csv`, and any lossy duplicates. Embedded cover art inside the FLACs is unaffected.

**Dry run output:** the count and list of files, then the folders that contain no FLAC anywhere beneath them (the ones that will be empty afterwards).

**Delete run:** prints each file as it's removed, then each empty folder, removed bottom-up. The target directory itself is never removed. Nothing goes to a trash folder; there's no undo apart from a ZFS snapshot.

The script stops on the first error (`set -euo pipefail`). It exits with an error if no directory is given or the path isn't a directory.

## flac\_to\_aac.py

Converts every FLAC under `/root/flac` to an AAC `.m4a` at about 196 kbps under `/root/compressed`, mirroring the folder structure, using every CPU core. Re-runs only convert files that are missing or whose output is broken.

**Requirements:** Python 3.9+, `av` (PyAV, which bundles FFmpeg's native `aac` encoder) and `mutagen`.

**Usage:** no arguments; edit the constants, then run:

```
python3 flac_to_aac.py
```

| Setting | Default | Effect |
| --- | --- | --- |
| `SRC_ROOT` | `/root/flac` | FLAC library scanned recursively |
| `DST_ROOT` | `/root/compressed` | Output root; `a/b/track.flac` becomes `a/b/track.m4a` |
| `BITRATE` | `196_000` | Target average bitrate in bits/s |
| `MIN_VALID_SIZE` | `1024` | Outputs smaller than this (bytes) count as broken |
| `STATE_FILE` | `DST_ROOT/.conversion_validation_state.json` | Outputs already validated, keyed by modified time and size |
| `LOG_PATH` | `./conversion_failures.csv` | Running log of failures, in the current directory |

**How a run works:**

1. Scans `DST_ROOT` for existing `.m4a` files. Those in the state file with an unchanged modified time and size are trusted without opening them.
2. The rest are validated in parallel: the file must be at least 1 KB, contain an audio stream, and demux at least one packet. This catches truncated files and missing `moov` atoms without a full decode. Broken files are left on disk and reconverted (overwritten).
3. Every FLAC without a valid output is converted in parallel. Audio is resampled to planar float, mono kept as mono and everything else downmixed to stereo. The sample rate is kept if AAC supports it, otherwise the nearest supported rate is used.
4. Container metadata (tags) is copied from the FLAC. The front-cover picture (type 3, else the first picture) is embedded if it's JPEG or PNG. A missing or failed cover doesn't fail the conversion.
5. Each successful output is added to the state file so the next run trusts it.

**Output:** `Converting: <path>` as each worker starts a file, then `[n/total] OK` or `FAILED` lines and a summary. Failures are appended to `conversion_failures.csv` with timestamp, file name, source path, error type and message. Corrupt source FLACs show as `CORRUPT_SOURCE`.

**Notes:**

- It only adds and overwrites; it never deletes AAC files whose FLAC has been removed or renamed. Orphans have to be cleaned up separately.
- FFmpeg's native AAC encoder has no true VBR mode through PyAV, so the bitrate is an average target (`libfdk_aac` is not used).

## Known issues and caveats

The biggest gap: tag changes made to FLACs after conversion never reach the AAC copy, because `flac_to_aac.py` skips any FLAC that already has a valid `.m4a`, regardless of the FLAC's own modified time.

| Script | Issue | Impact |
| --- | --- | --- |
| `flac_to_aac.py` | Skip check looks only at the output, not whether the source FLAC changed | Retagging FLACs (e.g. with `fix_artist_tags.py`) leaves stale tags in the AAC library; delete the affected `.m4a` files to force reconversion |
| `flac_to_aac.py` | Never removes outputs whose FLAC was deleted, renamed or moved | Orphaned and duplicate `.m4a` files build up in `/root/compressed` |
| `deletenonflac.sh` | Header comment says folders are left in place, but `--delete` removes every empty folder | Documentation in the file is wrong; behaviour matches this doc |
| `deletenonflac.sh` | Deletes everything not ending in `.flac`, including hidden files | Running it on the library root also removes `.artist_tag_fix_state.json` and `corrupt_files.csv`, so the next tag fix starts from scratch |
| `fix_artist_tags.py` | Overwrites track artist with the folder name | Loses featured-artist credits and per-track artists on compilations or Various Artists folders |
| `fix_artist_tags.py` | `.aac` (raw ADTS) is in the extension list but mutagen can't write tags to it | Those files most likely error every run and pile up in `corrupt_files.csv` |
| `fix_artist_tags.py` | Root is the current directory | Running it from the wrong folder treats the wrong first-level folder as the artist |
| `tagging.py` | Docstring and usage examples call it `music_organizer.py` | Cosmetic; examples in the file use the old name |
| `tagging.py` | MP4 files get fewer tags (no label, release-group or release-track ID) than MP3/FLAC/Ogg | M4A files are less precisely identified by Navidrome and Lidarr |
| `lidarr_add_artists.py` | In a dry run, an artist listed twice in the CSV shows `[DRY ]` both times | A live run correctly skips the second one |
