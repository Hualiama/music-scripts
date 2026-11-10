#!/usr/bin/env python3
"""
music_organizer.py - retag from MusicBrainz, embed cover art, rename and sort
music files into  <dest>/<Album Artist>/<Album>/<NN - Title>.<ext>

Supported formats: mp3, flac, m4a/mp4, ogg, oga, opus

Each folder is treated as one album and identified in this order:
  0. Files that already carry MusicBrainz IDs (e.g. tagged by Picard) are
     matched straight to that release.
  1. MusicBrainz album search, using the tags (and the folder name).
  2. Files still unmatched: MusicBrainz recording search (title, artist,
     length).
  3. Files still unmatched: AcoustID fingerprints; the leftover files in the
     folder vote on the release. Skipped if no AcoustID key is given.

Every candidate release is scored the way Picard scores them: per-track
title, length, track number and artist agreement; album title and artist;
track count; year; and preferred release status, type, country and format.
A match is only applied when it scores at least --min-score (default 0.80).
Anything below is left untouched and written to unmatched.txt with the best
candidate and its MusicBrainz link. With --interactive you're asked to pick
(or paste a release link) instead.

Setup (Debian Bookworm):
  sudo apt install libchromaprint-tools python3-venv     # fpcalc, for AcoustID
  python3 -m venv ~/.venvs/musicorg && . ~/.venvs/musicorg/bin/activate
  pip install mutagen pyacoustid musicbrainzngs requests
  # optional, free AcoustID application key: https://acoustid.org/new-application
  export ACOUSTID_API_KEY=xxxxxxxx

Usage:
  ./music_organizer.py /music/unsorted /music/sorted --contact you@example.com --dry-run
  ./music_organizer.py /music/unsorted /music/sorted --contact you@example.com --interactive

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
from types import SimpleNamespace

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
USER_AGENT = {"User-Agent": "music-organizer/0.2"}
LOCAL_COVER_NAMES = ["cover", "folder", "front", "album"]
MB_URL = "https://musicbrainz.org/release/"
MIN_TRACK = 0.60   # a single file must match its track at least this well
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


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
    length: float          # seconds, 0 if unknown


@dataclass
class Release:
    mbid: str
    rgid: str
    title: str
    artist: str
    date: str
    label: str
    status: str
    primary_type: str
    secondary_types: list
    country: str
    formats: list
    tracks: list

    def describe(self):
        fmt = "+".join(sorted({f for f in self.formats if f}))
        bits = [self.date[:4], self.country, fmt, f"{len(self.tracks)} tracks",
                self.status, "/".join([self.primary_type] + self.secondary_types)]
        return f"{self.artist} - {self.title}  [{', '.join(b for b in bits if b)}]"


@dataclass
class Info:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    albumartist: str = ""
    number: int = 0
    disc: int = 0
    year: str = ""
    length: float = 0.0
    mb_album: str = ""
    mb_recording: str = ""
    mb_reltrack: str = ""


@dataclass
class Hints:
    album: str = ""
    artist: str = ""
    year: str = ""


@dataclass
class Candidate:
    score: float
    release: Release
    matches: dict          # path -> (Track, pair score)


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


_EXTRA = re.compile(
    r"\s*[\(\[][^)\]]*\b(feat|ft|featuring|remaster(ed)?|version|edit|mono|stereo|"
    r"bonus|explicit|clean|deluxe|expanded|edition)\b[^)\]]*[\)\]]", re.I)


def clean_title(s):
    s = _EXTRA.sub("", s or "")
    return re.sub(r"\s+(feat\.?|ft\.?|featuring)\s.*$", "", s, flags=re.I)


def tsim(a, b):
    """Similarity that forgives (feat. X), (Remastered 2011), [Deluxe] etc."""
    return max(sim(a, b), sim(clean_title(a), clean_title(b)))


def safe(name, limit=120):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "Unknown"


def to_int(v, default=0):
    try:
        return int(str(v).split("/")[0])
    except (ValueError, TypeError):
        return default


def year_of(s):
    m = re.search(r"\b(19|20)\d{2}\b", s or "")
    return m.group(0) if m else ""


def majority(values):
    values = [v for v in values if v]
    return Counter(values).most_common(1)[0][0] if values else ""


def wavg(parts):
    """Weighted average that ignores components that are None (unknown)."""
    parts = [(w, v) for w, v in parts if v is not None]
    total = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / total if total else 0.0


def log(msg):
    print(msg, flush=True)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# reading what we already know about the files
# --------------------------------------------------------------------------
def read_info(path):
    it = Info(path)
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is not None:
        it.length = float(getattr(f.info, "length", 0) or 0)
        tags = f.tags

        def first(*keys):
            for k in keys:
                try:
                    v = tags.get(k) if tags is not None else None
                except Exception:
                    v = None
                if v:
                    return str(v[0]).strip()
            return ""

        it.title, it.artist, it.album = first("title"), first("artist"), first("album")
        it.albumartist = first("albumartist", "album artist")
        it.number, it.disc = to_int(first("tracknumber")), to_int(first("discnumber"))
        it.year = year_of(first("originaldate", "date", "year"))
        it.mb_album = first("musicbrainz_albumid")
        it.mb_recording = first("musicbrainz_trackid")
        it.mb_reltrack = first("musicbrainz_releasetrackid")
    if not it.title:
        stem = path.stem
        m = re.match(r"^\s*(?:(\d{1,2})[-.])?(\d{1,3})[\s.\-_]+(.+)$", stem)
        if m:
            if not it.disc and m.group(1):
                it.disc = int(m.group(1))
            if not it.number:
                it.number = int(m.group(2))
            stem = m.group(3)
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            if not it.artist:
                it.artist = a.strip()
            stem = t
        it.title = stem.strip()
    return it


def folder_hints(folder, root):
    """Guess artist/album/year from 'Artist - Album (2001) [FLAC]' style names."""
    if folder == root:
        return Hints()
    name = folder.name
    year = year_of(name)
    name = re.sub(r"[\[(][^\])]*[\])]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    artist, album = "", name
    if " - " in name:
        a, b = [x.strip() for x in name.split(" - ", 1)]
        if re.fullmatch(r"(19|20)\d{2}", a):
            album, year = b, year or a
        else:
            artist, album = a, b
    if not artist and folder.parent != root:
        artist = folder.parent.name
    return Hints(album, artist, year)


def tag_hints(items):
    return Hints(
        album=majority([i.album for i in items]),
        artist=majority([i.albumartist for i in items]) or majority([i.artist for i in items]),
        year=majority([i.year for i in items]),
    )


# --------------------------------------------------------------------------
# MusicBrainz / AcoustID / Cover Art Archive
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=1024)
def get_release(mbid):
    try:
        r = mb.get_release_by_id(
            mbid, includes=["recordings", "artist-credits", "release-groups",
                            "labels", "media"])["release"]
    except (mb.MusicBrainzError, KeyError) as e:
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
                length=to_int(t.get("length") or rec.get("length")) / 1000,
            ))
    label = ""
    try:
        label = r["label-info-list"][0]["label"]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    rg = r.get("release-group", {}) or {}
    return Release(
        mbid=r["id"],
        rgid=rg.get("id", ""),
        title=r.get("title", ""),
        artist=r.get("artist-credit-phrase", ""),
        date=r.get("date", ""),
        label=label,
        status=r.get("status", ""),
        primary_type=rg.get("primary-type") or rg.get("type", ""),
        secondary_types=list(rg.get("secondary-type-list", []) or []),
        country=r.get("country", ""),
        formats=[m.get("format", "") for m in mediums],
        tracks=tracks,
    )


# With --mb-host + --remote-search, lookups go to the local mirror (no rate
# limit) while searches still go to musicbrainz.org at 1 request/second.
_SPLIT = {"on": False, "local": "", "last": 0.0}


def mb_search(fn, **kwargs):
    if not _SPLIT["on"]:
        return fn(**kwargs)
    wait = 1.0 - (time.monotonic() - _SPLIT["last"])
    if wait > 0:
        time.sleep(wait)
    mb.set_hostname("musicbrainz.org", use_https=True)
    try:
        return fn(**kwargs)
    finally:
        _SPLIT["last"] = time.monotonic()
        mb.set_hostname(_SPLIT["local"], use_https=False)


_LUCENE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def lq(field, value):
    value = _LUCENE.sub(r"\\\1", (value or "").lower()).strip()
    return f"{field}:({value})" if value else ""


def stub_from_mb(r):
    """Lightweight release summary from a MusicBrainz search result."""
    rg = r.get("release-group", {}) or {}
    tc = to_int(r.get("medium-track-count")) or sum(
        to_int(m.get("track-count")) for m in r.get("medium-list", []))
    return {"id": r["id"], "title": r.get("title", ""), "status": r.get("status", ""),
            "type": rg.get("primary-type") or rg.get("type", ""),
            "secondary": list(rg.get("secondary-type-list", []) or []),
            "country": r.get("country", ""), "track_count": tc,
            "year": year_of(r.get("date", "")), "mbscore": to_int(r.get("ext:score"))}


def stub_from_acoustid(r, rg):
    d = r.get("date") or {}
    year = str(d.get("year", "")) if isinstance(d, dict) else year_of(str(d))
    return {"id": r["id"], "title": r.get("title") or rg.get("title", ""), "status": "",
            "type": rg.get("type", ""), "secondary": list(rg.get("secondarytypes", []) or []),
            "country": r.get("country", ""), "track_count": to_int(r.get("track_count")),
            "year": year, "mbscore": 0}


def rank_stubs(stubs, hints, n, prefs):
    """Order releases before fetching them: like Picard, prefer official
    albums that match the album name and track count, earliest first."""
    years = [int(s["year"]) for s in stubs if s["year"]]
    earliest = min(years) if years else None

    def rank(st):
        s = st["mbscore"] / 100
        if hints.album:
            s += 2 * tsim(hints.album, st["title"])
        s += 0.5 if st["status"] in ("", "Official") else 0
        s += {"Album": 0.5, "EP": 0.25, "Single": 0.25}.get(st["type"], 0)
        s += 0 if st["secondary"] else 0.5
        if n > 1 and st["track_count"]:
            k = st["track_count"]
            s += 1 - min(abs(k - n) / max(k, n), 1)
        if prefs.countries and st["country"] in prefs.countries:
            s += 0.25
        if earliest and st["year"] and int(st["year"]) == earliest:
            s += 0.3
        return s

    return sorted(stubs, key=rank, reverse=True)


def acoustid_lookup(path, key, min_score):
    """Return [(recording_id, [release stubs])], best first."""
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
        time.sleep(0.35)  # AcoustID allows ~3 requests/second
        resp = acoustid.lookup(key, fp, duration,
                               meta="recordings releasegroups releases")
    except Exception as e:
        warn(f"{path.name}: acoustid failed ({e})")
        return []
    out = []
    for res in sorted(resp.get("results", []), key=lambda r: -r.get("score", 0)):
        if res.get("score", 0) < min_score:
            continue
        for rec in res.get("recordings", []) or []:
            stubs = [stub_from_acoustid(r, rg)
                     for rg in rec.get("releasegroups", []) or []
                     for r in rg.get("releases", []) or []]
            stubs += [stub_from_acoustid(r, {}) for r in rec.get("releases", []) or []]
            out.append((rec["id"], stubs))
    if out and not out[0][1]:
        try:
            r = mb.get_recording_by_id(out[0][0], includes=["releases"])["recording"]
            out[0] = (out[0][0], [stub_from_mb(x) for x in r.get("release-list", [])])
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
# scoring
# --------------------------------------------------------------------------
def dur_score(a, b):
    """1.0 within 3 s, falling to 0 at 20 s apart. None if either is unknown."""
    if not a or not b:
        return None
    d = abs(a - b)
    if d <= 3:
        return 1.0
    if d >= 20:
        return 0.0
    return 1.0 - (d - 3) / 17


def pair_score(it, t, recs=()):
    """How well one file matches one track on a release (0-1)."""
    known = bool(t.rec_id and (t.rec_id in recs or t.rec_id == it.mb_recording)) \
        or bool(it.mb_reltrack and it.mb_reltrack == t.track_id)
    ts = tsim(it.title, t.title) if it.title else None
    if not known and (ts is None or ts < 0.5):
        return 0.0
    num = None
    if it.number:
        num = 1.0 if it.number == t.number and (not it.disc or it.disc == t.disc) else 0.0
    art = tsim(it.artist, t.artist) if it.artist and t.artist else None
    s = wavg([(0.45, ts), (0.30, dur_score(it.length, t.length)),
              (0.15, num), (0.10, art)])
    return max(s, 0.9) if known else s


def assign(rel, items, recs):
    """Best one-to-one file->track assignment (greedy on the best pairs first)."""
    pairs = []
    for it in items:
        r = recs.get(it.path, ())
        for t in rel.tracks:
            s = pair_score(it, t, r)
            if s > 0:
                pairs.append((s, it.path, t))
    pairs.sort(key=lambda p: -p[0])
    out, used = {}, set()
    for s, path, t in pairs:
        if path in out or t.track_id in used:
            continue
        out[path] = (t, s)
        used.add(t.track_id)
    return out


def pref_score(rel, hints, prefs):
    status = {"Official": 1.0, "": 0.7}.get(rel.status, 0.3)
    ptype = {"Album": 1.0, "EP": 0.8, "Single": 0.7}.get(rel.primary_type, 0.5)
    sec = 1.0
    if rel.secondary_types:   # compilation/live/remix etc. unless the tags say so
        hinted = any(norm(x) in norm(hints.album) for x in rel.secondary_types)
        sec = 1.0 if hinted else 0.4
    parts = [(1, status), (1, ptype), (1, sec)]
    if prefs.countries:
        parts.append((1, 1.0 if rel.country in prefs.countries else 0.5))
    if prefs.formats:
        fm = [norm(f) for f in rel.formats]
        parts.append((1, 1.0 if any(p in f for p in prefs.formats for f in fm) else 0.5))
    return wavg(parts)


def release_score(rel, items, recs, hints, prefs, single=False):
    m = assign(rel, items, recs)
    if not m:
        return Candidate(0.0, rel, m)
    track_part = sum(s for _, s in m.values()) / len(items)   # unmatched files count 0
    count_part = None
    if not single:
        n, k = len(items), len(rel.tracks)
        count_part = 1.0 - min(abs(k - n) / max(k, n), 1.0)
    album_part = tsim(hints.album, rel.title) if hints.album else None
    artist_part = tsim(hints.artist, rel.artist) if hints.artist else None
    date_part = None
    ry = year_of(rel.date)
    if hints.year and ry:
        date_part = max(0.0, 1.0 - abs(int(hints.year) - int(ry)) / 5)
    score = wavg([(0.45, track_part), (0.20, album_part), (0.10, artist_part),
                  (0.12, count_part), (0.03, date_part),
                  (0.10, pref_score(rel, hints, prefs))])
    return Candidate(score, rel, m)


# --------------------------------------------------------------------------
# candidate searches
# --------------------------------------------------------------------------
def album_candidates(items, th, fh, prefs, args):
    queries = []
    for h in (th, fh):
        if h.album and not any(norm(h.album) == norm(q[0]) for q in queries):
            queries.append((h.album, h.artist))
    seen, stubs = set(), []
    for album, artist in queries:
        variants = [lq("release", album) + (" AND " + lq("artist", artist) if artist else "")]
        if artist:
            variants.append(lq("release", album))
        for q in variants:
            try:
                res = mb_search(mb.search_releases, query=q, limit=25).get(
                    "release-list", [])
            except mb.MusicBrainzError as e:
                warn(f"album search failed: {e}")
                res = []
            for r in res:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    stubs.append(stub_from_mb(r))
            if len(res) >= 10:    # enough to choose from; skip the looser query
                break
    rank_hints = th if th.album else fh
    out = []
    for st in rank_stubs(stubs, rank_hints, len(items), prefs)[:args.candidates]:
        rel = get_release(st["id"])
        if rel:
            out.append(release_score(rel, items, {}, th, prefs))
    return sorted(out, key=lambda c: -c.score)


def recording_candidates(it, prefs):
    if not it.title:
        return [], Hints()
    h = Hints(it.album, it.albumartist or it.artist, it.year)
    base = lq("recording", it.title)
    if it.artist:
        base += " AND " + lq("artist", it.artist)
    queries = []
    if it.length:
        ms = int(it.length * 1000)
        queries.append(f"{base} AND dur:[{max(ms - 5000, 0)} TO {ms + 5000}]")
    queries.append(base)
    recs = []
    for q in queries:
        try:
            recs = mb_search(mb.search_recordings, query=q, limit=15).get(
                "recording-list", [])
        except mb.MusicBrainzError as e:
            warn(f"recording search failed: {e}")
            recs = []
        if recs:
            break
    ranked = []
    for rec in recs:
        s = wavg([(0.5, tsim(it.title, rec.get("title", ""))),
                  (0.3, dur_score(it.length, to_int(rec.get("length")) / 1000)),
                  (0.2, tsim(it.artist, rec.get("artist-credit-phrase", ""))
                   if it.artist else None)])
        if s >= 0.7:
            ranked.append((s, rec))
    ranked.sort(key=lambda x: -x[0])
    cands = []
    for _, rec in ranked[:3]:
        stubs = rank_stubs([stub_from_mb(r) for r in rec.get("release-list", [])], h, 1, prefs)
        for st in stubs[:2]:
            rel = get_release(st["id"])
            if rel:
                cands.append(release_score(rel, [it], {it.path: [rec["id"]]}, h, prefs,
                                           single=True))
    return sorted(cands, key=lambda c: -c.score), h


# --------------------------------------------------------------------------
# deciding
# --------------------------------------------------------------------------
def ask_user(folder, label, items, cands, scorer):
    log(f"\n  ? {folder.name}  ({label}, {len(items)} file(s)) - no confident match")
    shown = cands[:5]
    for n, c in enumerate(shown, 1):
        log(f"    {n}) {c.score:.2f}  {c.release.describe()}  "
            f"{len(c.matches)}/{len(items)} files matched")
        log(f"       {MB_URL}{c.release.mbid}")
    while True:
        try:
            ans = input(f"    pick 1-{len(shown)}, s=skip, or paste a release "
                        f"MBID/URL [s]: ").strip()
        except EOFError:
            return None
        if ans.lower() in ("", "s"):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(shown):
            return shown[int(ans) - 1]
        m = UUID_RE.search(ans)
        if m:
            rel = get_release(m.group(0).lower())
            if rel:
                c = scorer(rel)
                if c.matches:
                    return c
                log("    none of the files matched that release's tracks")
            continue
        log("    didn't understand that")


def identify_directory(folder, files, root, args, prefs):
    """Return ({path: (Release, Track, method, score)}, [unmatched], {path: best Candidate})."""
    items = [read_info(p) for p in files]
    th, fh = tag_hints(items), folder_hints(folder, root)
    assigned, best_seen = {}, {}

    def remaining():
        return [i for i in items if i.path not in assigned]

    def consider(cands, method, subset, scorer, label):
        """Apply the best candidate if confident (or the user picks one)."""
        cands = [c for c in cands if c.score > 0]
        if not cands:
            return
        best = cands[0]
        close = (len(cands) > 1 and cands[1].release.rgid != best.release.rgid
                 and best.score - cands[1].score < 0.02)
        chosen = None
        if best.score >= args.min_score and not (args.interactive and close):
            chosen = best
        elif args.interactive:
            chosen = ask_user(folder, label, subset, cands, scorer)
            method = "manual" if chosen else method
        if chosen:
            log(f"  -> {chosen.score:.2f} {chosen.release.describe()}")
            for path, (t, s) in chosen.matches.items():
                if path not in assigned and s >= MIN_TRACK:
                    assigned[path] = (chosen.release, t, method, chosen.score)
        for path in (i.path for i in subset):
            if path not in assigned and (path not in best_seen
                                         or best.score > best_seen[path].score):
                best_seen[path] = best

    # 0) MusicBrainz IDs already in the tags
    ids = Counter(i.mb_album for i in items if i.mb_album)
    if ids and not args.ignore_existing_ids:
        left = remaining()
        cands = [release_score(r, left, {}, th, prefs)
                 for r in filter(None, (get_release(i) for i, _ in ids.most_common(2)))]
        consider(sorted(cands, key=lambda c: -c.score), "existing-id", left,
                 lambda r: release_score(r, left, {}, th, prefs), "existing IDs")

    # 1) MusicBrainz album search
    left = remaining()
    if left and (th.album or fh.album):
        consider(album_candidates(left, th, fh, prefs, args), "album-search", left,
                 lambda r: release_score(r, left, {}, th, prefs), "album search")

    # 2) MusicBrainz recording search, file by file
    for it in remaining():
        cands, h = recording_candidates(it, prefs)
        consider(cands, "track-search", [it],
                 lambda r, it=it, h=h: release_score(r, [it], {}, h, prefs, single=True),
                 f"track search: {it.path.name}")

    # 3) AcoustID fingerprints
    left = remaining()
    if left and args.acoustid_key:
        recs, results, stubs_by_id, votes = {}, {}, {}, Counter()
        for it in left:
            res = acoustid_lookup(it.path, args.acoustid_key, args.acoustid_min)
            results[it.path] = res
            recs[it.path] = [rid for rid, _ in res]
            ids = set()
            for _, sts in res:
                for st in sts:
                    stubs_by_id.setdefault(st["id"], st)
                    ids.add(st["id"])
            votes.update(ids)
        if votes:
            order = {st["id"]: n for n, st in enumerate(
                rank_stubs(list(stubs_by_id.values()), th if th.album else fh,
                           len(left), prefs))}
            top = sorted(votes, key=lambda rid: (-votes[rid], order.get(rid, 1e9)))
            cands = [release_score(r, left, recs, th, prefs)
                     for r in filter(None, map(get_release, top[:args.candidates]))]
            consider(sorted(cands, key=lambda c: -c.score), "acoustid", left,
                     lambda r: release_score(r, left, recs, th, prefs), "fingerprints")
        # files the folder vote couldn't place (e.g. a folder of loose singles)
        for it in remaining():
            res = results.get(it.path) or []
            if not res:
                continue
            h = Hints(it.album, it.albumartist or it.artist, it.year)
            stubs = rank_stubs([st for _, sts in res for st in sts], h, 1, prefs)
            r1 = {it.path: recs[it.path]}
            cands = [release_score(r, [it], r1, h, prefs, single=True)
                     for r in filter(None, map(get_release, [s["id"] for s in stubs[:2]]))]
            consider(sorted(cands, key=lambda c: -c.score), "acoustid", [it],
                     lambda r, it=it, h=h, r1=r1: release_score(r, [it], r1, h, prefs,
                                                                single=True),
                     f"fingerprint: {it.path.name}")

    unmatched = [p for p in files if p not in assigned]
    return assigned, unmatched, {p: best_seen[p] for p in unmatched if p in best_seen}


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

        def ff(v):
            return [MP4FreeForm(v.encode())]
        audio["----:com.apple.iTunes:MusicBrainz Track Id"] = ff(tr.rec_id)
        audio["----:com.apple.iTunes:MusicBrainz Album Id"] = ff(rel.mbid)
        if tr.track_id:
            audio["----:com.apple.iTunes:MusicBrainz Release Track Id"] = ff(tr.track_id)
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


def process_file(src, rel, tr, method, score, dest, args):
    dst = unique(build_dest(dest, rel, tr, src.suffix.lower(), args), src)
    rel_dst = dst.relative_to(dest) if dst.is_relative_to(dest) else dst
    log(f"  [{method} {score:.2f}] {src.name}  ->  {rel_dst}")
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
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="directory to scan (recursively)")
    ap.add_argument("dest", type=Path, help="root of the sorted library")
    ap.add_argument("--contact", required=True,
                    help="your email/URL - MusicBrainz requires it in the User-Agent")
    ap.add_argument("--acoustid-key", default=os.environ.get("ACOUSTID_API_KEY"),
                    help="AcoustID key for the fingerprint fallback "
                         "(or set ACOUSTID_API_KEY)")
    ap.add_argument("--mb-host", default="",
                    help="your own MusicBrainz mirror, e.g. localhost:5000 "
                         "(plain HTTP, no rate limit)")
    ap.add_argument("--remote-search", action="store_true",
                    help="with --mb-host: send searches to musicbrainz.org, for a "
                         "mirror without its own search server")
    ap.add_argument("--dry-run", action="store_true", help="show plan, change nothing")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="ask when no release scores above --min-score, or when two "
                         "different albums are nearly tied")
    ap.add_argument("--min-score", type=float, default=0.80,
                    help="minimum score 0-1 for a match to be applied (default 0.80); "
                         "raise it for fewer wrong matches, lower it for fewer "
                         "unmatched files")
    ap.add_argument("--candidates", type=int, default=6,
                    help="releases fetched and scored per search (default 6; more is "
                         "more thorough but slower - MusicBrainz allows 1 request/s)")
    ap.add_argument("--prefer-country", default="",
                    help="comma list of preferred release countries, e.g. US,XW")
    ap.add_argument("--prefer-format", default="",
                    help='comma list of preferred media formats, e.g. "Digital Media,CD"')
    ap.add_argument("--ignore-existing-ids", action="store_true",
                    help="don't trust MusicBrainz IDs already in the files")
    ap.add_argument("--acoustid-min", type=float, default=0.8,
                    help="minimum AcoustID fingerprint score 0-1 (default 0.8)")
    ap.add_argument("--keep-originals", action="store_true",
                    help="copy instead of move")
    ap.add_argument("--no-cover", action="store_true", help="don't embed cover art")
    ap.add_argument("--artist-folder", choices=["albumartist", "artist"],
                    default="albumartist",
                    help="which artist names the top-level folder (default albumartist)")
    ap.add_argument("--year-in-album", action="store_true",
                    help='name album folders "Album (Year)"')
    args = ap.parse_args(argv)

    if args.acoustid_key and not shutil.which("fpcalc"):
        ap.error("fpcalc not found: sudo apt install libchromaprint-tools")
    if not args.acoustid_key:
        warn("no AcoustID key set: fingerprint fallback disabled")
    mb.set_useragent("music-organizer", "0.2", args.contact)
    if args.remote_search and not args.mb_host:
        ap.error("--remote-search needs --mb-host")
    if args.mb_host:
        mb.set_hostname(args.mb_host, use_https=False)
        mb.set_rate_limit(False)
        if args.remote_search:
            _SPLIT.update(on=True, local=args.mb_host)
    prefs = SimpleNamespace(
        countries=[c.strip().upper() for c in args.prefer_country.split(",") if c.strip()],
        formats=[norm(f) for f in args.prefer_format.split(",") if f.strip()],
    )

    src, dest = args.source.resolve(), args.dest.resolve()
    files = sorted(p for p in src.rglob("*")
                   if p.is_file() and p.suffix.lower() in EXTS
                   and not (dest != src and p.is_relative_to(dest)))
    groups = defaultdict(list)
    for p in files:
        groups[p.parent].append(p)
    log(f"{len(files)} files in {len(groups)} folders"
        f"{'  (DRY RUN)' if args.dry_run else ''}")

    done, failed, unmatched_all, best_all = 0, 0, [], {}
    for n, (folder, flist) in enumerate(sorted(groups.items()), 1):
        log(f"\n[{n}/{len(groups)}] {folder}")
        try:
            assigned, unmatched, best = identify_directory(folder, flist, src, args, prefs)
        except Exception as e:
            warn(f"folder failed: {e}")
            unmatched_all += flist
            continue
        for path, (rel, tr, method, score) in assigned.items():
            try:
                process_file(path, rel, tr, method, score, dest, args)
                done += 1
            except Exception as e:
                warn(f"{path.name}: {e}")
                failed += 1
        for p in unmatched:
            c = best.get(p)
            hint = f" - best guess {c.score:.2f}: {c.release.describe()}" if c else ""
            log(f"  [no match] {p.name} (left untouched){hint}")
        unmatched_all += unmatched
        best_all.update(best)

    log(f"\nDone: {done} processed, {failed} errors, {len(unmatched_all)} unmatched")
    if unmatched_all and not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        lines = []
        for p in unmatched_all:
            c = best_all.get(p)
            lines.append(f"{p}\tbest guess {c.score:.2f}: {c.release.describe()}  "
                         f"{MB_URL}{c.release.mbid}" if c else str(p))
        (dest / "unmatched.txt").write_text("\n".join(lines) + "\n")
        log(f"Unmatched files listed in {dest / 'unmatched.txt'}")


if __name__ == "__main__":
    main()
