"""Fetch the canon/Legends continuity split from Wookieepedia's era categories.

Wookieepedia's era template auto-files every in-universe article into exactly one
of two maintenance categories: "Category:Legends articles" or "Category:Canon
articles". That signal is the only authoritative canon/Legends marker, and it was
stripped when the wiki was converted to markdown locally, so we pull it back from
the live MediaWiki API and cache the full title lists to disk.

Output (one page title per line, namespace-0 articles only):
    continuity/legends_titles.txt
    continuity/canon_titles.txt

Titles match the `title:` frontmatter field of the local .md files exactly,
including "/Legends" subpage suffixes (e.g. "Vibroblade Brigade/Legends").

Usage:
    uv run fetch_continuity.py            # fetch both; skip if already cached
    uv run fetch_continuity.py --force    # re-fetch even if cached
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://starwars.fandom.com/api.php"
UA = "wookieLM-research/1.0 (rafa@rayes.com.br)"
OUT_DIR = Path(__file__).parent / "continuity"

# Output tag -> source era categories. "noncanon" merges the in-universe
# non-canonical tiers (cut content, easter eggs, April Fools, crossovers) that
# exist within both the canon and Legends continuities.
CATEGORIES = {
    "legends": ["Category:Legends articles"],
    "canon": ["Category:Canon articles"],
    "noncanon": ["Category:Non-canon articles", "Category:Non-canon Legends articles"],
}


def fetch_members(category: str) -> list[str]:
    """Return every namespace-0 page title in `category`, following cmcontinue."""
    titles: list[str] = []
    cmcontinue: str | None = None
    page = 0
    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": category,
            "cmlimit": "500",
            "cmnamespace": "0",     # main/article namespace only
            "cmtype": "page",
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        url = f"{API}?{urllib.parse.urlencode(params)}"

        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                break
            except Exception as exc:  # noqa: BLE001 - retry transient API/network errors
                wait = 2 ** attempt
                print(f"  retry {attempt + 1}/5 after error: {exc} (waiting {wait}s)")
                time.sleep(wait)
        else:
            raise RuntimeError(f"giving up on {category} after repeated failures")

        titles.extend(m["title"] for m in data["query"]["categorymembers"])
        page += 1
        print(f"  {category}: {len(titles):,} titles ({page} requests)", end="\r")

        cont = data.get("continue")
        if not cont:
            break
        cmcontinue = cont["cmcontinue"]
        time.sleep(0.1)  # be polite to the API

    print()
    return titles


def main() -> None:
    force = "--force" in sys.argv[1:]
    OUT_DIR.mkdir(exist_ok=True)

    for tag, categories in CATEGORIES.items():
        out = OUT_DIR / f"{tag}_titles.txt"
        if out.exists() and not force:
            n = sum(1 for _ in out.open(encoding="utf-8"))
            print(f"skip   {out.name} (cached, {n:,} titles)")
            continue
        titles: set[str] = set()
        for category in categories:
            print(f"fetch  {category} ...")
            titles.update(fetch_members(category))
        out.write_text("\n".join(sorted(titles)) + "\n", encoding="utf-8")
        print(f"ok     {out.name} ({len(titles):,} titles)")


if __name__ == "__main__":
    main()
