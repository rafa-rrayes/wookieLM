#!/usr/bin/env python3
"""
Gather and structure English Star Wars subtitles into a corpus.

Two modes:
  api    : download subtitles from the OpenSubtitles REST API (needs a free API key)
  local  : process .srt/.vtt files you already have in a folder

Both modes run the same cleaning/structuring pipeline and emit:
  <out>/<title-slug>/<slug>.txt  one cleaned transcript per movie/episode,
                                 grouped into one directory per title
  <out>/manifest.jsonl           one JSON record per transcript, with metadata

The OpenSubtitles API is the only "automatic" path here. Bulk-scraping subtitle
sites violates their terms of use and the files are copyrighted transcriptions,
so this script uses the official API (rate-limited, requires an account) instead.

Usage:
  export OPENSUBTITLES_API_KEY=xxxx
  uv run --with requests wookiee-subtitles api               # default out: corpus/subtitles
  uv run --with requests wookiee-subtitles local --in ./my_subs
  uv run wookiee-subtitles list                              # print the built-in title list

API key resolution order: --api-key flag  >  OPENSUBTITLES_API_KEY env var.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable

from wookielm import paths

try:
    import requests
except ImportError:
    requests = None  # only needed for `api` mode; checked at runtime


# --------------------------------------------------------------------------- #
# Built-in title list. EDIT THIS FREELY.
# `imdb_id` makes API matching far more reliable than title search alone; leave
# it as None to fall back to a title query. `kind` is "movie" or "tv".
# --------------------------------------------------------------------------- #

@dataclass
class Title:
    name: str
    year: int | None
    kind: str            # "movie" | "tv"
    imdb_id: str | None = None   # numeric IMDb id WITHOUT the "tt" prefix
    seasons: int | None = None   # for tv, how many seasons to walk (best effort)


TITLES: list[Title] = [
    # ---- Skywalker Saga ----
    Title("Star Wars: Episode I – The Phantom Menace",       1999, "movie", "0120915"),
    Title("Star Wars: Episode II – Attack of the Clones",    2002, "movie", "0121765"),
    Title("Star Wars: Episode III – Revenge of the Sith",    2005, "movie", "0121766"),
    Title("Star Wars: Episode IV – A New Hope",              1977, "movie", "0076759"),
    Title("Star Wars: Episode V – The Empire Strikes Back",  1980, "movie", "0080684"),
    Title("Star Wars: Episode VI – Return of the Jedi",      1983, "movie", "0086190"),
    Title("Star Wars: Episode VII – The Force Awakens",      2015, "movie", "2488496"),
    Title("Star Wars: Episode VIII – The Last Jedi",         2017, "movie", "2527336"),
    Title("Star Wars: Episode IX – The Rise of Skywalker",   2019, "movie", "2527338"),
    # ---- Anthology films ----
    Title("Rogue One: A Star Wars Story",                    2016, "movie", "3748528"),
    Title("Solo: A Star Wars Story",                         2018, "movie", "3778644"),
    Title("The Clone Wars (film)",                           2008, "movie", "1185834"),
    # ---- Animated / live-action series ----
    Title("Star Wars: The Clone Wars",                       2008, "tv", "0458290", seasons=7),
    Title("Star Wars Rebels",                                2014, "tv", "2930604", seasons=4),
    Title("Star Wars Resistance",                            2018, "tv", "7440726", seasons=2),
    Title("Star Wars: The Bad Batch",                        2021, "tv", "12708542", seasons=3),
    Title("The Mandalorian",                                 2019, "tv", "8111088", seasons=3),
    Title("The Book of Boba Fett",                           2021, "tv", "13668894", seasons=1),
    Title("Obi-Wan Kenobi",                                  2022, "tv", "8466564", seasons=1),
    Title("Andor",                                           2022, "tv", "9253284", seasons=2),
    Title("Ahsoka",                                          2023, "tv", "13622776", seasons=1),
    Title("The Acolyte",                                     2024, "tv", "12262202", seasons=1),
    Title("Skeleton Crew",                                   2024, "tv", "14688458", seasons=1),
    Title("Star Wars: Maul – Shadow Lord",                   2026, "tv", "36594331", seasons=1),
    
    # ---- "Tales of" anthology (each season is a standalone release) ----
    Title("Star Wars: Tales of the Jedi",                    2022, "tv", "20723374", seasons=1),
    Title("Star Wars: Tales of the Empire",                  2024, "tv", "32019314", seasons=1),
    Title("Star Wars: Tales of the Underworld",              2025, "tv", "36414431", seasons=1),
]


# --------------------------------------------------------------------------- #
# Subtitle cleaning pipeline (shared by both modes)
# --------------------------------------------------------------------------- #

# SRT/VTT timecode lines like:  00:01:23,456 --> 00:01:25,789
TIMECODE_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}")
# Lone sequence-number lines in SRT
SEQNUM_RE = re.compile(r"^\d+$")
# Inline markup: <i>, {\an8}, etc.
TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
# Bracketed/parenthesised sound cues: [explosion], (sighs)
CUE_RE = re.compile(r"^\s*[\[(].*?[\])]\s*$")
# Speaker labels at line start: "LUKE:" / "HAN:" — kept by default, see strip_speakers
SPEAKER_RE = re.compile(r"^[A-Z][A-Z0-9 .'\-]{1,30}:\s")
# VTT cue settings line / NOTE / STYLE blocks
VTT_META_RE = re.compile(r"^(WEBVTT|NOTE|STYLE|::cue).*", re.IGNORECASE)


def _strip_html_entities(text: str) -> str:
    common = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
              "&#39;": "'", "&apos;": "'", "&nbsp;": " "}
    for k, v in common.items():
        text = text.replace(k, v)
    return text


def clean_subtitle_text(raw: str, strip_speakers: bool = False,
                        strip_cues: bool = True) -> str:
    """Turn raw SRT/VTT content into clean prose lines.

    - removes timecodes, sequence numbers, VTT metadata, inline tags
    - optionally removes [sound cues] and SPEAKER: labels
    - collapses hard-wrapped subtitle lines, dedups consecutive repeats
    """
    raw = raw.replace("\ufeff", "")  # BOM
    raw = _strip_html_entities(raw)
    lines_out: list[str] = []

    for block in re.split(r"\r?\n\r?\n", raw):
        block = block.strip()
        if not block:
            continue
        block_lines: list[str] = []
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if TIMECODE_RE.search(line):
                continue
            if SEQNUM_RE.match(line):
                continue
            if VTT_META_RE.match(line):
                continue
            line = TAG_RE.sub("", line).strip()
            if not line:
                continue
            if strip_cues and CUE_RE.match(line):
                continue
            # remove a trailing-only cue, e.g. "Run!  [door slams]"
            if strip_cues:
                line = re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", line).strip()
            if strip_speakers:
                line = SPEAKER_RE.sub("", line).strip()
            line = re.sub(r"^[-–]\s*", "", line).strip()  # dialogue dashes
            if line:
                block_lines.append(line)
        if block_lines:
            # join hard-wrapped lines within a single cue into one line
            lines_out.append(" ".join(block_lines))

    # collapse exact consecutive duplicate cues (common in subtitle rips)
    deduped: list[str] = []
    for ln in lines_out:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)

    text = "\n".join(deduped)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\s-]", "", name).strip().lower()
    name = re.sub(r"[\s_-]+", "-", name)
    return name or "untitled"


# Episode suffix on a slug, e.g. "...-s01e02"; used to find the parent title dir.
EP_SUFFIX_RE = re.compile(r"-s\d{1,2}e\d{1,3}$")


def title_slug_for(slug: str) -> str:
    """Directory a transcript belongs in: the show slug for an episode, or the
    slug itself for a movie / standalone file."""
    return EP_SUFFIX_RE.sub("", slug)


# --------------------------------------------------------------------------- #
# Output writing
# --------------------------------------------------------------------------- #

@dataclass
class CorpusRecord:
    slug: str
    title_slug: str           # directory this transcript lives in (one per title)
    title: str
    year: int | None
    kind: str
    language: str
    source: str               # "opensubtitles" | "local"
    source_detail: str        # file id / original filename
    char_count: int
    line_count: int
    imdb_id: str | None = None
    season: int | None = None
    episode: int | None = None


def write_outputs(out_dir: Path, slug: str, text: str, record: CorpusRecord,
                  manifest_fh) -> None:
    title_dir = out_dir / record.title_slug
    title_dir.mkdir(parents=True, exist_ok=True)
    (title_dir / f"{slug}.txt").write_text(text, encoding="utf-8")
    manifest_fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    manifest_fh.flush()


def already_done(out_dir: Path, slug: str) -> bool:
    """True if a non-empty transcript for this slug already exists on disk."""
    f = out_dir / title_slug_for(slug) / f"{slug}.txt"
    try:
        return f.is_file() and f.stat().st_size > 0
    except OSError:
        return False


def load_done_slugs(out_dir: Path) -> set[str]:
    """Slugs recorded in an existing manifest (used to report resume state)."""
    done: set[str] = set()
    mp = out_dir / "manifest.jsonl"
    if not mp.is_file():
        return done
    for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("slug"):
                done.add(rec["slug"])
        except json.JSONDecodeError:
            continue
    return done


def open_manifest(out_dir: Path, force: bool):
    """Append to the manifest when resuming; truncate only on --force.

    On resume we de-duplicate by slug so repeated runs don't pile up stale
    records for the same title (last write wins).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    mp = out_dir / "manifest.jsonl"
    if force or not mp.is_file():
        return mp.open("w", encoding="utf-8")
    return mp.open("a", encoding="utf-8")


def dedup_manifest(manifest_path: Path) -> None:
    """Rewrite the manifest keeping only the last record per slug, in order."""
    if not manifest_path.is_file():
        return
    records: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            slug = json.loads(line).get("slug")
        except json.JSONDecodeError:
            continue
        if slug:
            records[slug] = line  # later lines overwrite earlier ones
    manifest_path.write_text("\n".join(records.values()) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# LOCAL mode
# --------------------------------------------------------------------------- #

def run_local(in_dir: Path, out_dir: Path, strip_speakers: bool,
              strip_cues: bool, force: bool = False) -> None:
    files = sorted([p for p in in_dir.rglob("*")
                    if p.suffix.lower() in (".srt", ".vtt")])
    if not files:
        print(f"No .srt/.vtt files found under {in_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.jsonl"
    prior = load_done_slugs(out_dir)
    if prior and not force:
        print(f"Resuming: {len(prior)} title(s) already in manifest.")
    written = skipped = 0
    with open_manifest(out_dir, force) as mf:
        for path in files:
            slug = slugify(path.stem)
            if not force and already_done(out_dir, slug):
                skipped += 1
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"  skip {path.name}: {e}", file=sys.stderr)
                continue
            text = clean_subtitle_text(raw, strip_speakers, strip_cues)
            if not text:
                print(f"  empty after cleaning: {path.name}", file=sys.stderr)
                continue
            rec = CorpusRecord(
                slug=slug,
                title_slug=title_slug_for(slug),
                title=path.stem,
                year=None,
                kind="unknown",
                language="en",
                source="local",
                source_detail=path.name,
                char_count=len(text),
                line_count=text.count("\n") + 1,
            )
            write_outputs(out_dir, slug, text, rec, mf)
            written += 1
            print(f"  wrote {slug}.txt  ({len(text):,} chars)")

    dedup_manifest(manifest_path)
    print(f"\nDone. {written} new, {skipped} skipped -> {out_dir}")
    print(f"Manifest: {manifest_path}")


# --------------------------------------------------------------------------- #
# API mode (OpenSubtitles REST API v1)
# --------------------------------------------------------------------------- #

OS_BASE = "https://api.opensubtitles.com/api/v1"


class OpenSubtitlesClient:
    def __init__(self, api_key: str, user_agent: str = "sw-corpus/1.0"):
        if requests is None:
            print("The 'requests' library is required for api mode: "
                  "pip install requests", file=sys.stderr)
            sys.exit(1)
        self.s = requests.Session()
        self.s.headers.update({
            "Api-Key": api_key,
            # OpenSubtitles expects the app identifier in X-User-Agent (their
            # undocumented-but-required header), not the standard User-Agent.
            "X-User-Agent": user_agent,
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, **params):
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: dict, headers: dict | None = None):
        return self._request("POST", path, json=payload, headers=headers)

    def _request(self, method: str, path: str, headers: dict | None = None, **kw):
        url = f"{OS_BASE}{path}"
        for attempt in range(5):
            r = self.s.request(method, url, timeout=30, headers=headers, **kw)
            if r.status_code == 429:               # rate limited
                wait = int(r.headers.get("Retry-After", 2 + attempt * 2))
                print(f"    rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 406:
                # After the header fix, a 406 here almost always means the
                # daily download quota is exhausted; surface the body so it's
                # obvious rather than a bare HTTP error.
                detail = ""
                try:
                    detail = r.json().get("message") or r.text[:200]
                except Exception:
                    detail = r.text[:200]
                raise RuntimeError(f"406 from {path}: {detail}")
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def search(self, *, imdb_id: str | None, query: str | None,
               kind: str, season: int | None = None,
               episode: int | None = None) -> list[dict]:
        params: dict = {"languages": "en", "order_by": "download_count"}
        if imdb_id:
            key = "parent_imdb_id" if kind == "tv" and season else "imdb_id"
            params[key] = imdb_id
        elif query:
            params["query"] = query
        if kind == "tv" and season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode
        data = self._get("/subtitles", **params)
        return data.get("data", [])

    def download(self, file_id: int) -> str:
        """Request a temporary download URL, then fetch the subtitle text.

        The /download endpoint is special: it returns 406 unless Accept is
        '*/*' (every other endpoint is fine with application/json). It also
        counts against your daily quota.
        """
        info = self._post("/download", {"file_id": file_id},
                          headers={"Accept": "*/*"})
        link = info.get("link")
        if not link:
            raise RuntimeError(f"no download link returned for file {file_id}")
        r = self.s.get(link, timeout=60)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text


def _best_file_id(result: dict) -> int | None:
    files = result.get("attributes", {}).get("files", [])
    if not files:
        return None
    return files[0].get("file_id")


def run_api(api_key: str, out_dir: Path, strip_speakers: bool,
            strip_cues: bool, sleep: float, max_episodes: int,
            force: bool = False) -> None:
    client = OpenSubtitlesClient(api_key)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.jsonl"
    prior = load_done_slugs(out_dir)
    if prior and not force:
        print(f"Resuming: {len(prior)} title(s)/episode(s) already in manifest.")
    written = skipped = 0

    with open_manifest(out_dir, force) as mf:
        for t in TITLES:
            print(f"\n=== {t.name} ({t.kind}) ===")
            if t.kind == "movie":
                status = _fetch_one(
                    client, out_dir, mf, t, season=None, episode=None,
                    strip_speakers=strip_speakers, strip_cues=strip_cues,
                    sleep=sleep, force=force)
                written += status == "fetched"
                skipped += status == "skipped"
            else:
                for s in range(1, (t.seasons or 1) + 1):
                    print(f"  -- season {s} --")
                    for ep in range(1, max_episodes + 1):
                        status = _fetch_one(
                            client, out_dir, mf, t, season=s, episode=ep,
                            strip_speakers=strip_speakers, strip_cues=strip_cues,
                            sleep=sleep, force=force)
                        if status == "fetched":
                            written += 1
                        elif status == "skipped":
                            skipped += 1
                            # already have it -> keep going to reach new episodes
                            continue
                        else:  # "none": no result for this episode number
                            break  # assume the season ended here

    dedup_manifest(manifest_path)
    print(f"\nDone. {written} new, {skipped} skipped -> {out_dir}")
    print(f"Manifest: {manifest_path}")


def _fetch_one(client, out_dir, mf, t: Title, *, season, episode,
               strip_speakers, strip_cues, sleep, force) -> str:
    """Returns 'fetched', 'skipped', or 'none' (no result for this slot)."""
    if season is not None:
        slug = slugify(f"{t.name}-s{season:02d}e{episode:02d}")
        title_disp = f"{t.name} S{season:02d}E{episode:02d}"
    else:
        slug = slugify(t.name)
        title_disp = t.name

    # Skip BEFORE any network call so re-runs don't burn download quota.
    if not force and already_done(out_dir, slug):
        print(f"    skip {slug} (already have it)")
        return "skipped"

    try:
        results = client.search(imdb_id=t.imdb_id, query=t.name, kind=t.kind,
                                 season=season, episode=episode)
    except Exception as e:
        print(f"    search failed: {e}", file=sys.stderr)
        return "none"
    if not results:
        return "none"
    file_id = _best_file_id(results[0])
    if not file_id:
        return "none"
    try:
        raw = client.download(file_id)
    except Exception as e:
        print(f"    download failed (file {file_id}): {e}", file=sys.stderr)
        return "none"
    text = clean_subtitle_text(raw, strip_speakers, strip_cues)
    if not text:
        return "none"

    rec = CorpusRecord(
        slug=slug, title_slug=title_slug_for(slug),
        title=title_disp, year=t.year, kind=t.kind, language="en",
        source="opensubtitles", source_detail=str(file_id),
        char_count=len(text), line_count=text.count("\n") + 1,
        imdb_id=t.imdb_id, season=season, episode=episode,
    )
    write_outputs(out_dir, slug, text, rec, mf)
    print(f"    wrote {slug}.txt  ({len(text):,} chars)")
    time.sleep(sleep)
    return "fetched"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def resolve_api_key(cli_key: str | None) -> str:
    key = cli_key or os.environ.get("OPENSUBTITLES_API_KEY")
    if not key:
        print("No API key. Set OPENSUBTITLES_API_KEY or pass --api-key.\n"
              "Get a free key at https://www.opensubtitles.com/en/consumers",
              file=sys.stderr)
        sys.exit(2)
    return key


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", type=Path, default=paths.SUBTITLES_DIR,
                        help="output directory (default: corpus/subtitles)")
    common.add_argument("--strip-speakers", action="store_true",
                        help="remove SPEAKER: labels at line starts")
    common.add_argument("--keep-cues", action="store_true",
                        help="keep [sound cues] / (stage directions)")
    common.add_argument("--force", action="store_true",
                        help="re-download/re-process everything, ignoring existing "
                             "files (default: skip existing and resume)")

    pa = sub.add_parser("api", parents=[common], help="download via OpenSubtitles API")
    pa.add_argument("--api-key", default=None)
    pa.add_argument("--sleep", type=float, default=1.0,
                    help="seconds between downloads (be polite to the API)")
    pa.add_argument("--max-episodes", type=int, default=30,
                    help="max episode number to probe per season")

    pl = sub.add_parser("local", parents=[common], help="process local .srt/.vtt")
    pl.add_argument("--in", dest="in_dir", type=Path, required=True,
                    help="folder to scan recursively for .srt/.vtt files")

    sub.add_parser("list", help="print the built-in title list and exit")

    args = p.parse_args(argv)

    if args.mode == "list":
        for t in TITLES:
            extra = f"  imdb=tt{t.imdb_id}" if t.imdb_id else ""
            seas = f"  seasons={t.seasons}" if t.seasons else ""
            print(f"[{t.kind:5}] {t.name} ({t.year}){extra}{seas}")
        return

    strip_cues = not args.keep_cues
    if args.mode == "local":
        run_local(args.in_dir, args.out, args.strip_speakers, strip_cues,
                  force=args.force)
    elif args.mode == "api":
        key = resolve_api_key(args.api_key)
        run_api(key, args.out, args.strip_speakers, strip_cues,
                args.sleep, args.max_episodes, force=args.force)


if __name__ == "__main__":
    main()