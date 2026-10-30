#!/usr/bin/env python3
"""
music_organizer.py - retag (MusicBrainz), embed cover art, rename and sort
music files into  <dest>/<Album Artist>/<Album>/<NN - Title>.<ext>

Supported formats: mp3, flac, m4a/mp4, ogg, oga, opus

How a file gets identified (per directory, so albums stay consistent):
  1. Search MusicBrainz for the album using the existing tags.
  2. Files still unmatched: search MusicBrainz for the individual recording.
  3. Files still unmatched: AcoustID fingerprint -> MusicBrainz recording ->
     candidate releases. Files in the same folder vote, and the release most
     of them agree on wins. (Skipped if no AcoustID key is given.)
  Anything still unidentified is left untouched and listed in unmatched.txt.

Cover art comes from the Cover Art Archive (falls back to cover/folder/front
images sitting next to the file).

Setup (Debian Bookworm):
  sudo apt install libchromaprint-tools python3-venv     # provides fpcalc
  python3 -m venv ~/.venvs/musicorg && . ~/.venvs/musicorg/bin/activate
  pip install mutagen pyacoustid musicbrainzngs requests
  # free AcoustID application key: https://acoustid.org/new-application
  export ACOUSTID_API_KEY=xxxxxxxx

Usage:
  ./music_organizer.py /music/unsorted /music/sorted --contact you@example.com --dry-run
  ./music_organizer.py /music/unsorted /music/sorted --contact you@example.com

Safety: each file is copied to its new location and tagged there; the original
is only deleted after tagging succeeds (use --keep-originals to never delete).
"""
import argparse
import base64
import difflib
import functools
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import acoustid
import musicbrainzngs as mb
import mutagen
import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB,
                         TRCK, TXXX, UFID, ID3NoHeaderError)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus"}
USER_AGENT = {"User-Agent": "music-organizer/0.1"}
LOCAL_COVER_NAMES = ["cover", "folder", "front", "album"]


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
@dataclass
class Track:
    rec_id: str
    track_id: str
    title: str
    artist: str
    disc: int
    number: int
    disc_total: int
    track_total: int


@dataclass
class Release:
    mbid: str
    rgid: str
    title: str
    artist: str
    date: str
    label: str
    status: str
    tracks: list


@dataclass
class Info:
    path: Path
    title: str
    artist: str
    album: str
    albumartist: str
    number: int


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def safe(name, limit=120):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "Unknown"


def to_int(v, default=0):
    try:
        return int(str(v).split("/")[0])
    except (ValueError, TypeError):
        return default


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr, flush=True)


def read_info(path):
    title = artist = album = albumartist = ""
    number = 0
    try:
        f = mutagen.File(path, easy=True)
        if f and f.tags:
            first = lambda k: (f.tags.get(k) or [""])[0]
            title, artist, album = first("title"), first("artist"), first("album")
            albumartist, number = first("albumartist"), to_int(first("tracknumber"))
    except Exception:
        pass
    if not title:
        stem = re.sub(r"^\d+[\s.\-_]+", "", path.stem)
        if " - " in stem and not artist:
            artist, stem = stem.split(" - ", 1)
        title = stem
    return Info(path, title.strip(), artist.strip(), album.strip(),
                albumartist.strip(), number)


# --------------------------------------------------------------------------
# MusicBrainz / AcoustID / Cover Art Archive
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=512)
def get_release(mbid):
    try:
        r = mb.get_release_by_id(
            mbid,
            includes=["recordings", "artists", "artist-credits",
                      "release-groups", "labels", "media"])["release"]
    except mb.MusicBrainzError as e:
        warn(f"could not fetch release {mbid}: {e}")
        return None
    mediums = r.get("medium-list", [])
    tracks = []
    for m in mediums:
        tl = m.get("track-list", [])
        for t in tl:
            rec = t.get("recording", {})
            tracks.append(Track(
                rec_id=rec.get("id", ""),
                track_id=t.get("id", ""),
                title=t.get("title") or rec.get("title") or "",
                artist=(t.get("artist-credit-phrase")
                        or rec.get("artist-credit-phrase")
                        or r.get("artist-credit-phrase") or ""),
                disc=to_int(m.get("position"), 1),
                number=to_int(t.get("position")),
                disc_total=len(mediums),
                track_total=len(tl),
            ))
    label = ""
    try:
        label = r["label-info-list"][0]["label"]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    return Release(
        mbid=r["id"],
        rgid=r.get("release-group", {}).get("id", ""),
        title=r.get("title", ""),
        artist=r.get("artist-credit-phrase", ""),
        date=r.get("date", ""),
        label=label,
        status=r.get("status", ""),
        tracks=tracks,
    )


def acoustid_candidates(path, key, min_score):
    """Return list of (recording_id, [release_ids]) best score first."""
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
        time.sleep(0.35)  # AcoustID allows ~3 requests/second
        resp = acoustid.lookup(key, fp, duration, meta="recordings releaseids")
    except Exception as e:
        warn(f"{path.name}: acoustid failed ({e})")
        return []
    out = []
    for res in sorted(resp.get("results", []), key=lambda r: -r.get("score", 0)):
        if res.get("score", 0) < min_score:
            continue
        for rec in res.get("recordings", []):
            rels = [r["id"] for r in rec.get("releases", [])]
            out.append((rec["id"], rels))
    # if the top recording came back with no releases, ask MusicBrainz
    if out and not out[0][1]:
        try:
            r = mb.get_recording_by_id(out[0][0], includes=["releases"])["recording"]
            out[0] = (out[0][0], [x["id"] for x in r.get("release-list", [])])
        except mb.MusicBrainzError:
            pass
    return out


@functools.lru_cache(maxsize=256)
def get_cover(mbid, rgid):
    urls = [f"https://coverartarchive.org/release/{mbid}/front-1200"]
    if rgid:
        urls.append(f"https://coverartarchive.org/release-group/{rgid}/front-1200")
    urls.append(f"https://coverartarchive.org/release/{mbid}/front-500")
    for url in urls:
        try:
            r = requests.get(url, timeout=30, headers=USER_AGENT)
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                return r.content
        except requests.RequestException:
            continue
    return None


def local_cover(folder):
    for name in LOCAL_COVER_NAMES:
        for ext in (".jpg", ".jpeg", ".png"):
            for cand in (folder / f"{name}{ext}", folder / f"{name.title()}{ext}"):
                if cand.is_file():
                    return cand.read_bytes()
    return None


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def match_tracks(rel, items, recs):
    """Map item.path -> Track within one release (by recording id, then title)."""
    out, used = {}, set()
    by_rec = {t.rec_id: t for t in rel.tracks}
    for it in items:
        found = None
        for rid in recs.get(it.path, []):
            t = by_rec.get(rid)
            if t and t.track_id not in used:
                found = t
                break
        if not found and it.title:
            best = 0.0
            for t in rel.tracks:
                if t.track_id in used:
                    continue
                s = sim(t.title, it.title)
                if it.number and t.number == it.number:
                    s += 0.05
                if s >= 0.85 and s > best:
                    best, found = s, t
        if found:
            out[it.path] = found
            used.add(found.track_id)
    return out


def majority(values):
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else ""


def search_album(album, artist, items, min_ms=85):
    """Search MusicBrainz for an album; return (release, {path: Track}) or None."""
    kwargs = {"release": album, "limit": 5}
    if artist:
        kwargs["artist"] = artist
    try:
        res = mb.search_releases(**kwargs)
    except mb.MusicBrainzError as e:
        warn(f"album search failed: {e}")
        return None
    best = None
    for r in res.get("release-list", []):
        if to_int(r.get("ext:score")) < min_ms:
            continue
        rel = get_release(r["id"])
        if not rel:
            continue
        m = match_tracks(rel, items, {})
        key = (len(m), rel.status == "Official")
        if m and (best is None or key > best[0]):
            best = (key, rel, m)
    return (best[1], best[2]) if best else None


def search_recording(it):
    """Last resort: search for the single recording."""
    if not it.title:
        return None
    kwargs = {"recording": it.title, "limit": 5}
    if it.artist:
        kwargs["artist"] = it.artist
    if it.album:
        kwargs["release"] = it.album
    try:
        res = mb.search_recordings(**kwargs)
    except mb.MusicBrainzError as e:
        warn(f"recording search failed: {e}")
        return None
    for rec in res.get("recording-list", []):
        if to_int(rec.get("ext:score")) < 90:
            continue
        rels = sorted(rec.get("release-list", []),
                      key=lambda r: -sim(r.get("title", ""), it.album))
        for r in rels[:3]:
            rel = get_release(r["id"])
            if not rel:
                continue
            m = match_tracks(rel, [it], {it.path: [rec["id"]]})
            if it.path in m:
                return rel, m[it.path]
    return None


def identify_directory(files, args):
    """Return ({path: (Release, Track, method)}, [unmatched paths])."""
    items = [read_info(p) for p in files]
    dir_album = majority([i.album for i in items])
    dir_artist = majority([i.albumartist or i.artist for i in items])
    assigned = {}

    # 1) MusicBrainz album search from existing tags
    if dir_album:
        found = search_album(dir_album, dir_artist, items)
        if found:
            rel, m = found
            for path, tr in m.items():
                assigned[path] = (rel, tr, "album-search")

    # 2) MusicBrainz per-recording search for whatever is left
    for it in [i for i in items if i.path not in assigned]:
        found = search_recording(it)
        if found:
            assigned[it.path] = (found[0], found[1], "track-search")

    # 3) AcoustID fingerprinting as the fallback, with per-directory release
    #    voting among the files that are still unidentified
    left = [i for i in items if i.path not in assigned]
    if left and args.acoustid_key:
        recs, votes = {}, Counter()
        for it in left:
            cands = acoustid_candidates(it.path, args.acoustid_key, args.min_score)
            recs[it.path] = [rid for rid, _ in cands]
            votes.update({rid for _, rels in cands for rid in rels})
        if votes:
            best = None
            for rid, count in votes.most_common(4):
                rel = get_release(rid)
                if not rel:
                    continue
                score = (count * 10
                         + sim(rel.title, dir_album) * 5
                         + (2 if rel.status == "Official" else 0)
                         - min(abs(len(rel.tracks) - len(left)), 10) * 0.2)
                if best is None or score > best[0]:
                    best = (score, rel)
            if best:
                m = match_tracks(best[1], left, recs)
                for path, tr in m.items():
                    assigned[path] = (best[1], tr, "acoustid")

    unmatched = [p for p in files if p not in assigned]
    return assigned, unmatched


# --------------------------------------------------------------------------
# tag writing
# --------------------------------------------------------------------------
def image_mime(data):
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def make_picture(data):
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = image_mime(data)
    pic.desc = "Cover"
    pic.data = data
    return pic


def write_tags(path, rel, tr, cover):
    audio = mutagen.File(path)
    if audio is None:
        raise RuntimeError("unsupported or unreadable file")

    if isinstance(audio, MP3):
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        def put(frame):
            tags.setall(frame.HashKey, [frame])
        put(TIT2(encoding=3, text=tr.title))
        put(TPE1(encoding=3, text=tr.artist))
        put(TALB(encoding=3, text=rel.title))
        put(TPE2(encoding=3, text=rel.artist))
        put(TRCK(encoding=3, text=f"{tr.number}/{tr.track_total}"))
        put(TPOS(encoding=3, text=f"{tr.disc}/{tr.disc_total}"))
        if rel.date:
            put(TDRC(encoding=3, text=rel.date))
        if rel.label:
            put(TPUB(encoding=3, text=rel.label))
        for desc, val in (("MusicBrainz Album Id", rel.mbid),
                          ("MusicBrainz Release Group Id", rel.rgid),
                          ("MusicBrainz Release Track Id", tr.track_id)):
            if val:
                put(TXXX(encoding=3, desc=desc, text=val))
        tags.setall("UFID:http://musicbrainz.org",
                    [UFID(owner="http://musicbrainz.org", data=tr.rec_id.encode())])
        if cover:
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=image_mime(cover), type=3,
                          desc="Cover", data=cover))
        tags.save(path, v2_version=3)

    elif isinstance(audio, MP4):
        audio["\xa9nam"] = [tr.title]
        audio["\xa9ART"] = [tr.artist]
        audio["\xa9alb"] = [rel.title]
        audio["aART"] = [rel.artist]
        audio["trkn"] = [(tr.number, tr.track_total)]
        audio["disk"] = [(tr.disc, tr.disc_total)]
        if rel.date:
            audio["\xa9day"] = [rel.date]
        ff = lambda v: [MP4FreeForm(v.encode())]
        audio["----:com.apple.iTunes:MusicBrainz Track Id"] = ff(tr.rec_id)
        audio["----:com.apple.iTunes:MusicBrainz Album Id"] = ff(rel.mbid)
        if cover:
            fmt = (MP4Cover.FORMAT_PNG if image_mime(cover) == "image/png"
                   else MP4Cover.FORMAT_JPEG)
            audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
        audio.save()

    elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
        d = {
            "title": tr.title, "artist": tr.artist, "album": rel.title,
            "albumartist": rel.artist,
            "tracknumber": str(tr.number), "tracktotal": str(tr.track_total),
            "discnumber": str(tr.disc), "disctotal": str(tr.disc_total),
            "musicbrainz_trackid": tr.rec_id,
            "musicbrainz_releasetrackid": tr.track_id,
            "musicbrainz_albumid": rel.mbid,
        }
        if rel.date:
            d["date"] = rel.date
        if rel.label:
            d["label"] = rel.label
        if rel.rgid:
            d["musicbrainz_releasegroupid"] = rel.rgid
        for k, v in d.items():
            audio[k] = [v]
        if cover:
            pic = make_picture(cover)
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                audio.add_picture(pic)
            else:
                audio["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
    else:
        raise RuntimeError(f"don't know how to tag {type(audio).__name__}")


# --------------------------------------------------------------------------
# renaming / sorting
# --------------------------------------------------------------------------
def build_dest(dest, rel, tr, ext, args):
    artist = rel.artist if args.artist_folder == "albumartist" else tr.artist
    album = rel.title
    if args.year_in_album and rel.date:
        album = f"{album} ({rel.date[:4]})"
    num = f"{tr.number:02d}"
    if tr.disc_total > 1:
        num = f"{tr.disc}-{num}"
    return dest / safe(artist) / safe(album) / (safe(f"{num} - {tr.title}", 150) + ext)


def unique(path, src):
    if not path.exists() or path.resolve() == src.resolve():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def process_file(src, rel, tr, method, dest, args):
    dst = unique(build_dest(dest, rel, tr, src.suffix.lower(), args), src)
    rel_dst = dst.relative_to(dest) if dst.is_relative_to(dest) else dst
    log(f"  [{method}] {src.name}  ->  {rel_dst}")
    if args.dry_run:
        return
    cover = None
    if not args.no_cover:
        cover = get_cover(rel.mbid, rel.rgid) or local_cover(src.parent)
        if cover is None:
            warn("no cover art found")
    same = dst.resolve() == src.resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not same:
        shutil.copy2(src, dst)
    try:
        write_tags(dst, rel, tr, cover)
    except Exception:
        if not same:
            dst.unlink(missing_ok=True)
        raise
    if not same and not args.keep_originals:
        src.unlink()


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="directory to scan (recursively)")
    ap.add_argument("dest", type=Path, help="root of the sorted library")
    ap.add_argument("--acoustid-key", default=os.environ.get("ACOUSTID_API_KEY"),
                    help="AcoustID key for the fingerprint fallback "
                         "(or set ACOUSTID_API_KEY)")
    ap.add_argument("--contact", required=True,
                    help="your email/URL - MusicBrainz requires it in the User-Agent")
    ap.add_argument("--dry-run", action="store_true", help="show plan, change nothing")
    ap.add_argument("--keep-originals", action="store_true",
                    help="copy instead of move")
    ap.add_argument("--no-cover", action="store_true", help="don't embed cover art")
    ap.add_argument("--min-score", type=float, default=0.8,
                    help="minimum AcoustID match score 0-1 (default 0.8)")
    ap.add_argument("--artist-folder", choices=["albumartist", "artist"],
                    default="albumartist",
                    help="which artist names the top-level folder (default albumartist)")
    ap.add_argument("--year-in-album", action="store_true",
                    help='name album folders "Album (Year)"')
    args = ap.parse_args()

    if args.acoustid_key and not shutil.which("fpcalc"):
        ap.error("fpcalc not found: sudo apt install libchromaprint-tools")
    if not args.acoustid_key:
        warn("no AcoustID key set: fingerprint fallback disabled")
    mb.set_useragent("music-organizer", "0.1", args.contact)

    src, dest = args.source.resolve(), args.dest.resolve()
    files = sorted(p for p in src.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTS
                   and not (dest != src and p.is_relative_to(dest)))
    groups = defaultdict(list)
    for p in files:
        groups[p.parent].append(p)
    log(f"{len(files)} files in {len(groups)} folders"
        f"{'  (DRY RUN)' if args.dry_run else ''}")

    done, failed, unmatched_all = 0, 0, []
    for n, (folder, flist) in enumerate(sorted(groups.items()), 1):
        log(f"\n[{n}/{len(groups)}] {folder}")
        try:
            assigned, unmatched = identify_directory(flist, args)
        except Exception as e:
            warn(f"folder failed: {e}")
            unmatched_all += flist
            continue
        for path, (rel, tr, method) in assigned.items():
            try:
                process_file(path, rel, tr, method, dest, args)
                done += 1
            except Exception as e:
                warn(f"{path.name}: {e}")
                failed += 1
        for p in unmatched:
            log(f"  [no match] {p.name} (left untouched)")
        unmatched_all += unmatched

    log(f"\nDone: {done} processed, {failed} errors, {len(unmatched_all)} unmatched")
    if unmatched_all and not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "unmatched.txt").write_text("\n".join(map(str, unmatched_all)) + "\n")
        log(f"Unmatched files listed in {dest / 'unmatched.txt'}")


if __name__ == "__main__":
    main()