"""Stamp a `continuity:` field into every Wookieepedia .md file's frontmatter.

Reads the canon/Legends title lists produced by wookiee-fetch-continuity and matches
each local article by its `title:` frontmatter field (exact match, including any
"/Legends" subpage suffix). Each file is tagged:

    continuity: canon        Category:Canon articles
    continuity: legends      Category:Legends articles
    continuity: non-canon    Category:Non-canon (Legends) articles — cut content,
                             easter eggs, April Fools, crossovers (fake in-universe)
    continuity: real-world   none of the above: films, authors, BTS, real-world
                             years, magazine articles, disambiguation/nav pages

The edit is strictly additive and idempotent: it inserts one line after the
`title:` line, or replaces an existing `continuity:` line. The article body is
never touched (matched and rewritten byte-for-byte around the frontmatter).

Usage:
    uv run wookiee-tag-continuity --dry-run            # report distribution, write nothing
    uv run wookiee-tag-continuity --dry-run --limit 50 # sample a few
    uv run wookiee-tag-continuity                       # apply to all files
"""

from __future__ import annotations

import argparse
import re
from collections import Counter

from wookielm import paths

WOOK_DIR = paths.WOOKIEEPEDIA_DIR
CONT_DIR = paths.CONTINUITY_DIR

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)


def load_titles(name: str) -> set[str]:
    path = CONT_DIR / name
    if not path.exists():
        raise SystemExit(f"missing {path} — run `uv run wookiee-fetch-continuity` first")
    return {ln.rstrip("\n") for ln in path.open(encoding="utf-8") if ln.strip()}


def retag(text: str, tag: str) -> str:
    """Insert/replace the continuity line inside the frontmatter, body untouched."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("no frontmatter")
    fm, rest = m.group(1), text[m.end():]
    lines = [ln for ln in fm.split("\n") if not ln.startswith("continuity:")]
    out, inserted = [], False
    for ln in lines:
        out.append(ln)
        if not inserted and ln.startswith("title:"):
            out.append(f"continuity: {tag}")
            inserted = True
    if not inserted:                       # no title line: append at end of frontmatter
        out.append(f"continuity: {tag}")
    return f"---\n{chr(10).join(out)}\n---\n{rest}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="process at most N files (0 = all)")
    args = ap.parse_args()

    canon = load_titles("canon_titles.txt")
    legends = load_titles("legends_titles.txt")
    noncanon = load_titles("noncanon_titles.txt")
    print(f"loaded {len(canon):,} canon + {len(legends):,} legends + "
          f"{len(noncanon):,} non-canon titles")

    files = sorted(WOOK_DIR.rglob("*.md"))
    if args.limit:
        files = files[: args.limit]

    counts: Counter[str] = Counter()
    no_fm = no_title = changed = 0
    rw_samples: list[str] = []

    for i, f in enumerate(files, 1):
        text = f.read_text(encoding="utf-8")
        tm = TITLE_RE.search(text)
        if not FRONTMATTER_RE.match(text):
            no_fm += 1
            continue
        if not tm:
            no_title += 1
            continue
        title = tm.group(1).strip()

        if title in canon:                 # precedence: canon > legends > non-canon
            tag = "canon"
        elif title in legends:
            tag = "legends"
        elif title in noncanon:
            tag = "non-canon"
        else:
            tag = "real-world"
            if len(rw_samples) < 25:
                rw_samples.append(title)
        counts[tag] += 1

        if not args.dry_run:
            new = retag(text, tag)
            if new != text:
                f.write_text(new, encoding="utf-8")
                changed += 1
        if i % 20000 == 0:
            print(f"  ...{i:,}/{len(files):,}")

    total = sum(counts.values())
    print(f"\n{'mode:':<12}{'DRY RUN (no writes)' if args.dry_run else 'APPLIED'}")
    print(f"{'files:':<12}{len(files):,}")
    for tag in ("canon", "legends", "non-canon", "real-world"):
        n = counts[tag]
        pct = f"{n / total * 100:.1f}%" if total else "—"
        print(f"  {tag:<11}{n:>9,}  {pct:>6}")
    if no_fm or no_title:
        print(f"  {'skipped':<11}{no_fm + no_title:>9,}  (no frontmatter: {no_fm}, no title: {no_title})")
    if not args.dry_run:
        print(f"{'written:':<12}{changed:,}")
    print("\nreal-world sample (tagged neither canon nor legends):")
    for t in rw_samples:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
