#!/usr/bin/env python3
"""
Bulk-add artists to Lidarr from a CSV and trigger a search/download.

No third-party packages needed (Python 3.7+ stdlib only).

CSV format (header row optional):
    artist,mbid
    Mogwai,
    Boards of Canada,
    Explosions in the Sky,a3cb23fc-acd3-4ce0-8f36-1e5aa6a18432

- Column 1: artist name
- Column 2 (optional): MusicBrainz artist ID. If given, it is used for an
  exact lookup, which avoids any name-matching ambiguity.

Usage:
    export LIDARR_URL=http://localhost:8686
    export LIDARR_API_KEY=your_api_key     # Lidarr > Settings > General
    python3 lidarr_add_artists.py artists.csv --dry-run   # preview first
    python3 lidarr_add_artists.py artists.csv             # do it
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Lidarr:
    def __init__(self, url, api_key):
        self.base = url.rstrip("/") + "/api/v1"
        self.api_key = api_key

    def request(self, method, path, params=None, body=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Cannot reach Lidarr at {self.base}: {e.reason}")

    def get(self, path, **params):
        return self.request("GET", path, params or None)

    def post(self, path, body):
        return self.request("POST", path, body=body)


def pick_by_name(items, wanted, label):
    """Return the item whose 'name' matches `wanted` (case-insensitive), or the first item."""
    if not items:
        sys.exit(f"No {label}s found in Lidarr - create one in the UI first.")
    if wanted:
        for it in items:
            if it["name"].lower() == wanted.lower():
                return it
        names = ", ".join(i["name"] for i in items)
        sys.exit(f"{label} '{wanted}' not found. Available: {names}")
    return items[0]


def read_csv(path):
    rows = []
    # Be forgiving about encoding: files re-saved by Excel are often cp1252, not UTF-8
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if enc != "utf-8-sig":
        print(f"Note: {path} is not UTF-8, read it as {enc} (accented/non-Latin names may be garbled)")
    if True:
        reader = csv.reader(text.splitlines())
        for i, row in enumerate(reader):
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            mbid = row[1].strip() if len(row) > 1 else ""
            # Skip a header row
            if i == 0 and name.lower() in ("artist", "name", "artist name", "artistname"):
                continue
            rows.append((name, mbid))
    return rows


def find_artist(lidarr, name, mbid, loose):
    """Look up an artist. Returns (lookup_result, note) or (None, reason)."""
    if mbid:
        results = lidarr.get("/artist/lookup", term=f"lidarr:{mbid}")
        if results:
            return results[0], "matched by MBID"
        return None, f"MBID {mbid} not found"

    results = lidarr.get("/artist/lookup", term=name)
    if not results:
        return None, "no search results"

    for r in results:
        if r.get("artistName", "").strip().lower() == name.lower():
            return r, "exact name match"

    if loose:
        return results[0], f"loose match -> '{results[0].get('artistName')}'"
    top = ", ".join(r.get("artistName", "?") for r in results[:3])
    return None, f"no exact match (top results: {top}); use --loose or add an MBID"


def main():
    p = argparse.ArgumentParser(description="Bulk-add artists from a CSV to Lidarr.")
    p.add_argument("csv_file")
    p.add_argument("--url", default=os.environ.get("LIDARR_URL", "http://localhost:8686"))
    p.add_argument("--api-key", default=os.environ.get("LIDARR_API_KEY"))
    p.add_argument("--root-folder", help="Root folder path (default: first one in Lidarr)")
    p.add_argument("--quality-profile", help="Quality profile name (default: first one)")
    p.add_argument("--metadata-profile", help="Metadata profile name (default: first one)")
    p.add_argument(
        "--monitor",
        default="all",
        choices=["all", "future", "missing", "existing", "latest", "first", "none"],
        help="Which albums to monitor (default: all)",
    )
    p.add_argument("--no-search", action="store_true", help="Add artists but don't start a search")
    p.add_argument("--loose", action="store_true", help="Accept the top search result if no exact name match")
    p.add_argument("--delay", type=float, default=300.0, help="Seconds to wait after each add/search (default: 300 = 5 min)")
    p.add_argument("--dry-run", action="store_true", help="Show what would happen without adding anything")
    args = p.parse_args()

    if not args.api_key:
        sys.exit("Set LIDARR_API_KEY or pass --api-key (Lidarr > Settings > General).")

    lidarr = Lidarr(args.url, args.api_key)

    # Connectivity check + gather settings
    try:
        lidarr.get("/system/status")
    except RuntimeError as e:
        sys.exit(str(e))

    root = args.root_folder
    if not root:
        folders = lidarr.get("/rootfolder")
        if not folders:
            sys.exit("No root folder configured in Lidarr - add one under Settings > Media Management.")
        root = folders[0]["path"]
    quality = pick_by_name(lidarr.get("/qualityprofile"), args.quality_profile, "quality profile")
    metadata = pick_by_name(lidarr.get("/metadataprofile"), args.metadata_profile, "metadata profile")

    print(f"Lidarr:            {args.url}")
    print(f"Root folder:       {root}")
    print(f"Quality profile:   {quality['name']}")
    print(f"Metadata profile:  {metadata['name']}")
    print(f"Monitor:           {args.monitor}   Search on add: {not args.no_search}")
    if args.dry_run:
        print("*** DRY RUN - nothing will be added ***")
    print()

    existing = {a["foreignArtistId"] for a in lidarr.get("/artist")}
    rows = read_csv(args.csv_file)
    print(f"{len(rows)} artists in CSV, {len(existing)} already in Lidarr\n")

    added, skipped, failed = 0, 0, []

    for idx, (name, mbid) in enumerate(rows):
        is_last = idx == len(rows) - 1
        try:
            match, note = find_artist(lidarr, name, mbid, args.loose)
            if match is None:
                print(f"[FAIL] {name}: {note}")
                failed.append((name, mbid, note))
                continue

            if match["foreignArtistId"] in existing:
                print(f"[SKIP] {match['artistName']}: already in Lidarr")
                skipped += 1
                continue

            if args.dry_run:
                print(f"[DRY ]  {match['artistName']} ({note})")
                continue

            payload = dict(match)
            payload.update(
                {
                    "qualityProfileId": quality["id"],
                    "metadataProfileId": metadata["id"],
                    "rootFolderPath": root,
                    "monitored": args.monitor != "none",
                    "monitorNewItems": "all",
                    "addOptions": {
                        "monitor": args.monitor,
                        "searchForMissingAlbums": not args.no_search,
                    },
                }
            )
            lidarr.post("/artist", payload)
            existing.add(match["foreignArtistId"])
            print(f"[ OK ] {match['artistName']} ({note})")
            added += 1
            if not is_last and args.delay > 0:
                print(f"       waiting {args.delay:g}s before the next artist...")
                time.sleep(args.delay)

        except RuntimeError as e:
            print(f"[FAIL] {name}: {e}")
            failed.append((name, mbid, str(e)))

    print(f"\nDone. Added: {added}  Skipped: {skipped}  Failed: {len(failed)}")

    if failed:
        out = "lidarr_failed.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["artist", "mbid", "reason"])
            w.writerows(failed)
        print(f"Failures written to {out} - fix names or add MBIDs and re-run.")


if __name__ == "__main__":
    main()