#!/usr/bin/env python3
"""Build the Star Wars Wikipedia corpus (data_sources.md #3) — one tool, two stages.

A two-stage pipeline behind a single CLI:

  crawl    Compile the list of Star Wars articles via the MediaWiki API, writing
           wikipedia/articles.jsonl + articles.txt + categories.txt. Seed from
           the Category:Star Wars tree, the WikiProject Star Wars banner, or both.

  scrape   Download each listed article's rendered HTML, strip the non-content
           chrome, convert to clean GitHub-flavoured Markdown (pandoc + an
           in-process table/HTML cleaner), and write one .md per article under
           wikipedia/<Shard>/, mirroring the Wookieepedia corpus format so both
           feed the same downstream wookiee-generate-fact pipeline.

Usage:
  uv run wookiee-wikipedia crawl                  # compile the article list (~850)
  uv run wookiee-wikipedia crawl --both           # category tree ∪ WikiProject (~1000)
  uv run wookiee-wikipedia scrape                  # download every listed article
  uv run wookiee-wikipedia scrape --limit 5        # smoke-test on 5
  uv run wookiee-wikipedia <command> -h            # full options + strategy notes

Requires: pandoc on PATH (scrape); beautifulsoup4 + lxml + tqdm (declared in
pyproject). Politeness: descriptive User-Agent, maxlag=5, --sleep between
requests, exponential backoff on errors.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Tag
from tqdm import tqdm

from wookielm import paths

USER_AGENT = (
    "wookieLM/1.0 "
    "(https://github.com/rafa-rrayes/wookieLM; rafa@rayes.com.br)"
)


# ============================================================================
# Shared: MediaWiki HTTP
# ============================================================================

def _request_json(url: str, *, retries: int = 6) -> dict:
    """GET a URL and parse JSON, with maxlag handling and exponential backoff.

    429/503 responses honour Retry-After; transient network/JSON errors and the
    maxlag API error back off and retry. Any other API error raises immediately.
    """
    for attempt in range(retries):
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except HTTPError as exc:  # 429/503 carry a Retry-After hint
            if exc.code in (429, 503):
                time.sleep(_retry_after(exc) or 2 ** attempt)
                continue
            raise
        except (URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(2 ** attempt)
            continue
        if "error" in data:
            if data["error"].get("code") == "maxlag":
                time.sleep(5)
                continue
            raise RuntimeError(f"API error: {data['error']}")
        return data
    raise RuntimeError(f"giving up after {retries} retries: {url}")


def _retry_after(exc: HTTPError) -> int:
    try:
        return int(exc.headers.get("Retry-After", 0))
    except (TypeError, ValueError):
        return 0


def api_get(api_url: str, params: dict[str, str], *, retries: int = 6) -> dict:
    """GET the MediaWiki API with query params (retry policy: see _request_json)."""
    return _request_json(f"{api_url}?{urlencode(params)}", retries=retries)


# ============================================================================
# Stage 1: crawl — compile the Star Wars article list
# ============================================================================

CAT_PREFIX = "Category:"
TALK_PREFIX = "Talk:"
PROJECT_CATEGORY = "WikiProject Star Wars articles"

# Maintenance / non-content subcategories skipped by default: redirects are
# aliases (duplicates), stubs is a cleanup tag, and images/templates hold
# File- and Template-namespace pages rather than articles. Override with
# --exclude (or --exclude "" to descend into everything).
EXCLUDE_DEFAULT = (
    "Star Wars redirects",
    "Star Wars stubs",
    "Star Wars images",
    "Star Wars templates",
)

CRAWL_DESC = """\
Compile Star Wars articles from Wikipedia (data_sources.md #3).

Two seeding strategies, both via the MediaWiki API (no HTML scraping):

  category-tree (default)
    Breadth-first walk of the Category:Star Wars graph, collecting every
    main-namespace article reachable through its subcategories. Faithful to
    the franchise category hierarchy — but that hierarchy is deliberately
    narrow: it holds in-universe + media topics and omits cross-categorized
    pages like cast/crew bios (e.g. Mark Hamill lives under "American film
    actors", George Lucas under the *parent* "Works by George Lucas"). Yields
    ~850 articles.

  --wikiproject
    Seeds from the WikiProject Star Wars banner instead
    (Category:WikiProject Star Wars articles): every article talk page editors
    have tagged as in-scope. This is the human-curated answer to "what is a
    Star Wars article" and is far more complete (~thousands), including cast,
    crew, and real-world production topics. Only ns-1 (article Talk) members
    count; File/Category/Template/Draft talk pages tagged by the banner are
    ignored, and the talk title's "Talk:" prefix is stripped to recover the
    article.

  --both
    Run both strategies and union the results, tagging each article with the
    source(s) it came from (category_tree / wikiproject). Neither strategy is
    a superset of the other, so the union (~1000 articles) is the most complete
    standalone-article set.

In all modes redirect/alias pages are filtered out by default (a prop=info
pass); pass --keep-redirects to retain them. Disambiguation pages (ns-0
navigation stubs) are likewise dropped by default; pass --keep-disambiguation
to retain them.

Outputs (into --out, default corpus/wikipedia):
  articles.jsonl   one record per article: title, pageid, ns, depth, via_category
  articles.txt     plain newline-separated list of article titles (sorted)
  categories.txt   every category visited during the crawl (sorted)

Examples:
  uv run wookiee-wikipedia crawl                              # category tree (~850)
  uv run wookiee-wikipedia crawl --wikiproject                # full curated set
  uv run wookiee-wikipedia crawl --category "Star Wars" --max-depth 6
  uv run wookiee-wikipedia crawl --category "Jedi" --lang en --out ./wiki_jedi
"""


def iter_members(api_url: str, cat_title: str, sleep: float):
    """Yield every member (pages + subcats) of a category, across all pages."""
    cont: dict[str, str] = {}
    while True:
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "categorymembers",
            "cmtitle": cat_title,
            "cmtype": "page|subcat",
            "cmprop": "ids|title|type|ns",
            "cmlimit": "500",
            "maxlag": "5",
            **cont,
        }
        data = api_get(api_url, params)
        yield from data["query"]["categorymembers"]
        if "continue" in data:
            cont = data["continue"]
            time.sleep(sleep)
        else:
            return


def crawl(api_url: str, root: str, max_depth: int | None, sleep: float,
          exclude: set[str] | None = None):
    """Breadth-first walk of the category tree from `root`.

    Subcategories whose name (without the "Category:" prefix) is in `exclude`
    are not descended into, and their pages are not collected unless reached
    through another, non-excluded category.

    Returns (articles, categories) where articles maps pageid -> record and
    categories is the set of category titles visited.
    """
    exclude = exclude or set()
    root_title = root if root.startswith(CAT_PREFIX) else CAT_PREFIX + root
    queue: deque[tuple[str, int]] = deque([(root_title, 0)])
    visited_cats: set[str] = {root_title}
    articles: dict[int, dict] = {}

    bar = tqdm(desc="categories", unit="cat")
    while queue:
        cat_title, depth = queue.popleft()
        for m in iter_members(api_url, cat_title, sleep):
            if m["type"] == "subcat":
                child = m["title"]
                if child in visited_cats:
                    continue
                if child.removeprefix(CAT_PREFIX) in exclude:
                    continue
                if max_depth is not None and depth >= max_depth:
                    continue
                visited_cats.add(child)
                queue.append((child, depth + 1))
            elif m["type"] == "page" and m["ns"] == 0:
                if m["pageid"] not in articles:
                    articles[m["pageid"]] = {
                        "title": m["title"],
                        "pageid": m["pageid"],
                        "ns": m["ns"],
                        "depth": depth,
                        "via_category": cat_title.removeprefix(CAT_PREFIX),
                    }
        bar.update(1)
        bar.set_postfix(articles=len(articles), queued=len(queue))
    bar.close()
    return articles, visited_cats


def collect_wikiproject(api_url: str, project_cat: str, sleep: float) -> tuple[set[str], set[str]]:
    """Collect article titles tagged by a WikiProject banner category.

    Members of the banner category are talk pages; only article talk pages
    (ns 1) map to articles, so File/Category/Template/Draft talk pages are
    skipped. Any subcategories (e.g. assessment containers) are descended into
    so nothing tagged-but-nested is missed. Returns (article_titles, cats_seen).
    """
    root = project_cat if project_cat.startswith(CAT_PREFIX) else CAT_PREFIX + project_cat
    queue: deque[str] = deque([root])
    visited: set[str] = {root}
    titles: set[str] = set()

    bar = tqdm(desc="banner-cats", unit="cat")
    while queue:
        cat = queue.popleft()
        for m in iter_members(api_url, cat, sleep):
            if m["type"] == "subcat":
                if m["title"] not in visited:
                    visited.add(m["title"])
                    queue.append(m["title"])
            elif m["type"] == "page" and m["ns"] == 1:  # article talk page
                titles.add(m["title"].removeprefix(TALK_PREFIX))
        bar.update(1)
        bar.set_postfix(articles=len(titles), queued=len(queue))
    bar.close()
    return titles, visited


def resolve_articles(api_url: str, titles: set[str], sleep: float,
                     keep_redirects: bool) -> dict[int, dict]:
    """Resolve titles to article records via prop=info, dropping missing pages,
    non-articles (ns != 0), and (unless keep_redirects) redirect aliases.
    """
    title_list = sorted(titles)
    articles: dict[int, dict] = {}
    for i in tqdm(range(0, len(title_list), 50), desc="resolve", unit="batch"):
        batch = title_list[i:i + 50]
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "titles": "|".join(batch),
            "prop": "info",
            "maxlag": "5",
        }
        data = api_get(api_url, params)
        for page in data["query"]["pages"]:
            if page.get("missing") or page.get("ns") != 0:
                continue
            if page.get("redirect") and not keep_redirects:
                continue
            articles[page["pageid"]] = {
                "title": page["title"],
                "pageid": page["pageid"],
                "ns": page["ns"],
                "depth": None,
                "via_category": PROJECT_CATEGORY,
            }
        time.sleep(sleep)
    return articles


def filter_disambiguation(api_url: str, articles: dict[int, dict], sleep: float) -> int:
    """Drop disambiguation pages from `articles` in place; return count removed.

    Disambiguation pages are ns-0 navigation stubs (e.g. "Boba Fett
    (disambiguation)") with no article content, so they are useless for a
    corpus. They are detected structurally via the pageprops 'disambiguation'
    flag rather than by title heuristics.
    """
    ids = list(articles)
    removed = 0
    for i in tqdm(range(0, len(ids), 50), desc="disambig-check", unit="batch"):
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "pageids": "|".join(map(str, ids[i:i + 50])),
            "prop": "pageprops",
            "ppprop": "disambiguation",
            "maxlag": "5",
        }
        data = api_get(api_url, params)
        for page in data["query"]["pages"]:
            if "disambiguation" in page.get("pageprops", {}):
                articles.pop(page["pageid"], None)
                removed += 1
        time.sleep(sleep)
    return removed


def merge_sources(*sets: tuple[str, dict[int, dict]]) -> dict[int, dict]:
    """Union several {pageid: record} sets, tagging each article with the names
    of the sources it was found in (e.g. ["category_tree", "wikiproject"]).
    """
    merged: dict[int, dict] = {}
    for name, articles in sets:
        for pid, rec in articles.items():
            if pid in merged:
                merged[pid]["sources"].append(name)
            else:
                merged[pid] = {"title": rec["title"], "pageid": pid, "ns": rec["ns"],
                               "sources": [name]}
    for rec in merged.values():
        rec["sources"].sort()
    return merged


def filter_redirects(api_url: str, articles: dict[int, dict], sleep: float) -> int:
    """Drop redirect pages from `articles` in place; return how many were removed.

    Category membership includes redirect pages (aliases that point at another
    article), which are duplicates for corpus purposes. They leak in through
    nested categories like "Star Wars character redirects to lists" that an
    exact-name --exclude can't anticipate, so they are filtered structurally:
    a single prop=info pass flags each page as a redirect or not.
    """
    pageids = list(articles)
    removed = 0
    for i in tqdm(range(0, len(pageids), 50), desc="redirect-check", unit="batch"):
        batch = pageids[i:i + 50]
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "pageids": "|".join(map(str, batch)),
            "prop": "info",
            "maxlag": "5",
        }
        data = api_get(api_url, params)
        for page in data["query"]["pages"]:
            if page.get("redirect"):
                articles.pop(page["pageid"], None)
                removed += 1
        time.sleep(sleep)
    return removed


def write_outputs(out_dir: Path, articles: dict[int, dict], categories: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    records = sorted(articles.values(), key=lambda r: r["title"].lower())

    with (out_dir / "articles.jsonl").open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with (out_dir / "articles.txt").open("w", encoding="utf-8") as f:
        f.write("\n".join(r["title"] for r in records) + "\n")

    cats = sorted(c.removeprefix(CAT_PREFIX) for c in categories)
    with (out_dir / "categories.txt").open("w", encoding="utf-8") as f:
        f.write("\n".join(cats) + "\n")


def run_category_tree(api_url: str, args) -> tuple[dict[int, dict], set[str]]:
    """Category-tree mode: crawl Category:<category> and filter redirects."""
    exclude = {c.strip() for c in args.exclude.split(",") if c.strip()}
    print(f"Crawling {CAT_PREFIX}{args.category} on {args.lang}.wikipedia.org "
          f"(max_depth={args.max_depth if args.max_depth is not None else 'unlimited'}"
          f"{', excluding ' + str(sorted(exclude)) if exclude else ''})", file=sys.stderr)
    articles, categories = crawl(api_url, args.category, args.max_depth, args.sleep, exclude)
    if not args.keep_redirects:
        removed = filter_redirects(api_url, articles, args.sleep)
        print(f"Filtered out {removed} redirect pages.", file=sys.stderr)
    return articles, categories


def run_wikiproject(api_url: str, args) -> tuple[dict[int, dict], set[str]]:
    """WikiProject mode: collect banner-tagged talk pages and resolve to articles
    (resolve_articles drops redirects unless --keep-redirects)."""
    print(f"Collecting {CAT_PREFIX}{args.project_category} on {args.lang}.wikipedia.org",
          file=sys.stderr)
    titles, categories = collect_wikiproject(api_url, args.project_category, args.sleep)
    print(f"Found {len(titles)} tagged article talk pages; resolving...", file=sys.stderr)
    articles = resolve_articles(api_url, titles, args.sleep, args.keep_redirects)
    return articles, categories


def cmd_crawl(args) -> int:
    api_url = f"https://{args.lang}.wikipedia.org/w/api.php"

    if args.both:
        ct_articles, ct_cats = run_category_tree(api_url, args)
        wp_articles, wp_cats = run_wikiproject(api_url, args)
        articles = merge_sources(("category_tree", ct_articles), ("wikiproject", wp_articles))
        categories = ct_cats | wp_cats
    elif args.wikiproject:
        articles, categories = run_wikiproject(api_url, args)
    else:
        articles, categories = run_category_tree(api_url, args)

    if not args.keep_disambiguation:
        removed = filter_disambiguation(api_url, articles, args.sleep)
        print(f"Filtered out {removed} disambiguation pages.", file=sys.stderr)

    write_outputs(args.out, articles, categories)

    print(f"\nDone: {len(articles)} articles across {len(categories)} categories "
          f"-> {args.out}/", file=sys.stderr)
    return 0


# ============================================================================
# Markdown cleaning (shared by scrape; turns leftover HTML into clean text)
# ============================================================================
#
# pandoc converts most of a Wikipedia article to Markdown, but falls back to raw
# inline HTML for two things GFM can't express: tables with rowspan/colspan, and
# the empty <div> wrappers MediaWiki puts around section headings. That HTML is
# noise for the downstream LLM fact pipeline. `clean_body` rewrites it:
#
# - empty <div>/</div> heading-wrapper lines are dropped;
# - each <table> is parsed into a rectangular grid (rowspan/colspan expanded) and
#   re-rendered as either a `key: value` bullet list (infoboxes: 2-column, no
#   header row) or a GitHub-flavoured Markdown table (data tables: a <th> header
#   row, or width >= 3);
# - list/<br>/emphasis markup inside cells is flattened to plain text;
# - any inline HTML tag that survives is stripped as a safety net.

# Inline formatting whose tags add nothing once we're plain text.
_INLINE_UNWRAP = ("em", "strong", "i", "b", "abbr", "sup", "sub", "span",
                  "small", "cite", "mark", "wbr", "u", "code", "big")
# Leftover inline tags scrubbed from the final text (outside any table too).
_STRAY_INLINE_RE = re.compile(
    r"</?(?:em|strong|i|b|abbr|sup|sub|span|small|cite|mark|wbr|u|big)\b[^>]*>",
    re.IGNORECASE)
_STRAY_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
# <div>s are bare structural wrappers once attributes are stripped (heading
# wrappers, plainlist boxes, blockquote shells); the tags carry no content.
_DIV_TAG_RE = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
_EMPTY_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*][ \t]*$\n?")
# pandoc emits {{Track listing}}-style tables with a blank header row + separator,
# demoting the real header (No.|Title|Length) to the first body row. Promote it.
_EMPTY_HEADER_RE = re.compile(
    r"(?m)^\|(?:[ \t]*\|)+[ \t]*\n"        # blank header row
    r"(\|[-:\t |]+\|)[ \t]*\n"             # separator (captured)
    r"(\|.*\|)[ \t]*$")                    # real header, currently a body row
_BLANKS_RE = re.compile(r"\n{3,}")
_WS_RE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:)])")

# Unicode space/typographic noise: zero-width chars removed outright; the various
# non-breaking/figure/thin spaces folded to a plain ASCII space.
_ZERO_WIDTH_RE = re.compile("[​‌‍⁠﻿\xad]")
_USPACE = "\xa0  -   　"
_USPACE_RE = re.compile(f"[{_USPACE}]")
_DOUBLE_SPACE_RE = re.compile(r"(?<=\S) {2,}")        # interior runs only
_CODE_LINE_RE = re.compile(r"^(?: {4,}|\t)\S")        # indented code block line
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]\s|\d{1,3}[.)]\s)")
_HEAD_RE = re.compile(r"^(#{1,6})\s+\S")
_CITE_RE = re.compile(r"\[\d{1,3}\]")                 # leftover [12] ref markers
# Undo pandoc's backslash-escaping of ASCII punctuation (\$ \# \[ \. ...). Two
# variants: prose unescapes everything; table rows keep \| (a real cell pipe).
_UNESC_PROSE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_UNESC_TABLE_RE = re.compile(r"\\([!-/:-@\[-`{}~])")  # same set minus '|'


# ---- cell text -------------------------------------------------------------

_BR_SENTINEL = "\x01"


def _cell_text(cell: Tag) -> str:
    """Flatten a <td>/<th> to one clean line. <br>-separated values become a
    comma list so cast/credit lists keep their boundaries ('Trey Stokes, Chris
    Hanel'); after a label colon a space reads better ('Star Wars Legends: The
    Farlander Papers')."""
    cell = _clone(cell)
    for nested in cell.find_all("table"):       # rare nested tables -> inline text
        nested.replace_with(_flatten_nested(nested))
    items = cell.find_all("li")
    if items:                                    # an explicit list -> comma-joined
        parts = [_norm(li.get_text(" ", strip=True)) for li in items]
        return ", ".join(p for p in parts if p)
    for br in cell.find_all("br"):
        br.replace_with(_BR_SENTINEL)
    segs = [_norm(s) for s in cell.get_text(" ").split(_BR_SENTINEL)]
    return _join_segments([s for s in segs if s])


def _join_segments(segs: list[str]) -> str:
    out = ""
    for s in segs:
        if not out:
            out = s
        elif out[-1] in ":(":                    # label or open paren -> just a space
            out += " " + s
        else:
            out += ", " + s
    return out


def _flatten_nested(table: Tag) -> str:
    """Collapse a table nested inside a cell to one line: 'a: b; c: d'."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [_norm(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(": ".join(cells))
    return "; ".join(rows)


def _norm(text: str) -> str:
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _USPACE_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text.replace("\n", " ")).strip()
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def _clone(tag: Tag) -> Tag:
    return BeautifulSoup(str(tag), "lxml").find(tag.name)


# ---- grid ------------------------------------------------------------------

class _Cell:
    __slots__ = ("text", "header")

    def __init__(self, text: str, header: bool):
        self.text = text
        self.header = header


def _direct_rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _build_grid(table: Tag) -> list[list[_Cell]]:
    """Expand rowspan/colspan into a rectangular grid of _Cell."""
    grid: list[list[_Cell]] = []
    carry: dict[tuple[int, int], tuple[_Cell, int]] = {}  # (row,col) -> (cell, rows_left)
    for ri, tr in enumerate(_direct_rows(table)):
        cells = [c for c in tr.find_all(["td", "th"]) if c.find_parent("table") is table]
        row: list[_Cell] = []
        col = 0
        i = 0
        while i < len(cells) or (ri, col) in carry:
            if (ri, col) in carry:                       # a rowspan from above lands here
                cell, left = carry.pop((ri, col))
                row.append(cell)
                if left > 1:
                    carry[(ri + 1, col)] = (cell, left - 1)
                col += 1
                continue
            src = cells[i]
            i += 1
            cell = _Cell(_cell_text(src), src.name == "th")
            cspan = _span(src, "colspan")
            rspan = _span(src, "rowspan")
            for _ in range(cspan):
                row.append(cell)
                if rspan > 1:
                    carry[(ri + 1, col)] = (cell, rspan - 1)
                col += 1
        grid.append(row)
    return _trim_grid(grid)


def _span(cell: Tag, attr: str) -> int:
    try:
        return max(1, int(cell.get(attr, 1)))
    except (TypeError, ValueError):
        return 1


def _trim_grid(grid: list[list[_Cell]]) -> list[list[_Cell]]:
    """Pad ragged rows; drop all-empty rows, and columns with no data (citation
    columns left empty once references are stripped show only a header)."""
    grid = [r for r in grid if any(c.text for c in r)]
    if not grid:
        return grid
    width = max(len(r) for r in grid)
    for r in grid:
        r.extend(_Cell("", False) for _ in range(width - len(r)))
    data = grid[1:] if len(grid) > 1 else grid
    keep = [c for c in range(width) if any(r[c].text for r in data)]
    return [[r[c] for c in keep] for r in grid] if keep else []


# ---- render ----------------------------------------------------------------

def _is_data_table(grid: list[list[_Cell]]) -> bool:
    if not grid:
        return False
    width = len(grid[0])
    first = grid[0]
    header_row = (all(c.header for c in first)
                  and len({c.text for c in first}) > 1)
    return header_row or width >= 3


def _esc(text: str) -> str:
    return text.replace("|", r"\|")


def _render_gfm(grid: list[list[_Cell]]) -> str:
    width = len(grid[0])
    header = [_esc(c.text) or " " for c in grid[0]]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * width]
    for row in grid[1:]:
        lines.append("| " + " | ".join(_esc(c.text) for c in row) + " |")
    return "\n".join(lines)


def _render_kv(grid: list[list[_Cell]]) -> str:
    out: list[str] = []
    for row in grid:
        vals = [c.text for c in row]
        nonempty = [v for v in vals if v]
        if len(vals) > 1 and len(set(vals)) == 1:        # spanning title/section row
            out.append(f"\n**{vals[0]}**")
        elif len(nonempty) >= 2:
            out.append(f"- {nonempty[0]}: {', '.join(nonempty[1:])}")
        elif nonempty:
            out.append(f"- {nonempty[0]}")
    return "\n".join(out).strip()


def _render_table(table: Tag) -> str:
    caption_tag = table.find("caption")
    caption = _norm(caption_tag.get_text(" ", strip=True)) if caption_tag else ""
    grid = _build_grid(table)
    if not grid:
        return f"**{caption}**" if caption else ""
    body = _render_gfm(grid) if _is_data_table(grid) else _render_kv(grid)
    if caption and caption.lower() not in grid[0][0].text.lower():
        body = f"**{caption}**\n\n{body}"
    return body


# ---- top-level -------------------------------------------------------------

def _table_spans(html: str) -> list[tuple[int, int]]:
    """Find (start, end) of each top-level <table>...</table>, depth-matched."""
    spans, depth, start = [], 0, 0
    for m in re.finditer(r"<\s*(/?)\s*table\b[^>]*>", html, re.IGNORECASE):
        if not m.group(1):                  # opening tag
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:                         # closing tag
            depth -= 1
            if depth == 0:
                spans.append((start, m.end()))
    return spans


def _for_each_line(md: str, fn) -> str:
    """Apply fn(line) to every line except fenced/indented code (left verbatim)."""
    out, fence = [], False
    for line in md.split("\n"):
        if _FENCE_RE.match(line):
            fence = not fence
            out.append(line)
        elif fence or _CODE_LINE_RE.match(line):
            out.append(line)
        else:
            out.append(fn(line))
    return "\n".join(out)


def _unescape_line(line: str) -> str:
    if line.lstrip().startswith("|"):            # table row: keep \| cell pipes
        return _UNESC_TABLE_RE.sub(r"\1", line)
    return _CITE_RE.sub("", _UNESC_PROSE_RE.sub(r"\1", line))


def _tidy_line(line: str) -> str:
    line = _ZERO_WIDTH_RE.sub("", line)
    line = _USPACE_RE.sub(" ", line)
    line = _DOUBLE_SPACE_RE.sub(" ", line)
    if not line.strip():                              # whitespace-only -> blank
        return ""
    if not _LIST_LINE_RE.match(line):                 # non-list prose has no indent
        line = line.lstrip(" ")
    return line.rstrip()


def _drop_empty_sections(md: str) -> str:
    """Remove headings whose section (down to the next same/higher heading) has no
    content. Cascades: a parent left with only empty subsections is empty too."""
    lines = md.split("\n")
    heads, fence = [], False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fence = not fence
        elif not fence:
            m = _HEAD_RE.match(line)
            if m:
                heads.append((i, len(m.group(1))))
    if not heads:
        return md
    head_lines = {i for i, _ in heads}
    remove: set[int] = set()
    for k, (i, level) in enumerate(heads):
        end = next((heads[j][0] for j in range(k + 1, len(heads))
                    if heads[j][1] <= level), len(lines))
        if not any(lines[x].strip() and x not in head_lines for x in range(i + 1, end)):
            remove.update(range(i, end))
    if not remove:
        return md
    return "\n".join(l for x, l in enumerate(lines) if x not in remove)


def clean_body(md: str) -> str:
    """Rewrite leftover HTML and pandoc cruft (tables, <div> wrappers, stray inline
    tags, backslash-escapes, Unicode-space noise, empty sections) to clean text."""
    spans = _table_spans(md)
    if spans:
        pieces, last = [], 0
        for start, end in spans:
            pieces.append(md[last:start])
            table = BeautifulSoup(md[start:end], "lxml").find("table")
            pieces.append(_render_table(table) if table else "")
            last = end
        pieces.append(md[last:])
        md = "".join(pieces)

    md = _DIV_TAG_RE.sub("", md)             # structural <div> wrappers
    md = _STRAY_BR_RE.sub(" ", md)           # any <br> outside a table
    md = _STRAY_INLINE_RE.sub("", md)        # any leftover inline tag
    md = _EMPTY_BULLET_RE.sub("", md)        # bullets emptied by div removal
    md = _for_each_line(md, _unescape_line)  # undo \$ \# \[ ... ; drop [12] refs
    md = _for_each_line(md, _tidy_line)      # Unicode spaces, double spaces, stray indent
    md = _EMPTY_HEADER_RE.sub(r"\2\n\1", md)  # promote demoted GFM table headers
    md = _drop_empty_sections(md)            # headings left with no content
    md = _BLANKS_RE.sub("\n\n", md)
    return md.strip() + "\n"


# ============================================================================
# Stage 2: scrape — download listed articles into clean Markdown
# ============================================================================

MIN_BODY_CHARS = 80

# Section headings whose content is out-of-prose chrome; dropped wholesale.
BOILERPLATE_SECTIONS = {
    "references", "notes", "notes and references", "footnotes", "citations",
    "sources", "external links", "see also", "further reading", "bibliography",
    "works cited", "explanatory notes", "general references",
}

# CSS selectors for non-content elements removed before conversion.
DROP_SELECTORS = (
    "style", "link", "script", "sup.reference", ".mw-editsection",
    ".reference", "ol.references", ".reflist", ".mw-references-wrap",
    "cite.citation", ".Z3988", ".navbox", ".vertical-navbox", ".navbox-styles",
    ".sistersitebox", ".side-box", ".metadata", ".ambox", ".hatnote",
    ".dablink", ".shortdescription", ".noprint", ".mw-empty-elt", ".portal",
    "figure", "img", ".thumb", ".gallery", ".infobox-image", ".mw-jump-link",
    ".mw-kartographer-maplink", ".geo-inline", "#coordinates", ".toc",
)

SCRAPE_DESC = """\
Scrape the compiled Wikipedia article list into clean Markdown (data_sources.md #3).

Reads the article list produced by `crawl` (wikipedia/articles.jsonl) and
downloads each article's rendered content via the MediaWiki API, writing one
Markdown file per article that mirrors the Wookieepedia corpus format (YAML
frontmatter + prose), so both feed the same downstream wookiee-generate-fact pipeline.

Why rendered HTML, not wikitext: the content worth keeping (infoboxes, episode
tables) lives in templates that only exist once expanded. So this fetches the
parsed HTML (action=parse) rather than raw wikitext, strips the non-content
chrome (references, navboxes, edit links, images, citations, See-also/External-
links sections), drops hyperlinks to plain text, then converts to GitHub-
flavoured Markdown with pandoc. Complex multi-row tables that GFM can't express
are left as compact inline HTML and then re-rendered cleanly by the in-process
table cleaner, so episode/infobox data survives.

Output layout (under --out, default corpus/wikipedia, alongside the list files):
  <out>/<Shard>/<Sanitized_Title>.md   one cleaned article, sharded by first char

Each file:
  ---
  title: "..."
  source: "Wikipedia"
  url: "https://en.wikipedia.org/wiki/..."
  categories:
    - "..."
  ---
  <markdown body>

Resumable: articles whose output file already exists are skipped unless
--overwrite is given. Per-article failures are collected and reported at the
end rather than aborting the run.

Examples:
  uv run wookiee-wikipedia scrape                       # scrape all listed articles
  uv run wookiee-wikipedia scrape --limit 5             # smoke-test on 5
  uv run wookiee-wikipedia scrape --letters A,B --workers 8
  uv run wookiee-wikipedia scrape --overwrite

Requires: pandoc on PATH; beautifulsoup4 + lxml (declared in pyproject).
"""


# ---- Fetch -----------------------------------------------------------------

def api_parse(api_url: str, pageid: int, *, retries: int = 6) -> tuple[str, str, list[str]]:
    """Fetch (title, rendered_html, visible_categories) for a page id."""
    params = {
        "action": "parse", "pageid": str(pageid), "prop": "text|categories",
        "disabletoc": "1", "disableeditsection": "1",
        "format": "json", "formatversion": "2", "redirects": "1",
    }
    data = api_get(api_url, params, retries=retries)
    p = data["parse"]
    cats = [c["category"].replace("_", " ")
            for c in p.get("categories", []) if not c.get("hidden")]
    return p["title"], p["text"], cats


# ---- Clean & convert -------------------------------------------------------

def clean_html(raw: str) -> str:
    """Strip chrome from rendered MediaWiki HTML; drop links to plain text.

    Returns the *inner* HTML of the article body with presentational attributes
    removed, so the tables pandoc preserves as inline HTML stay lean (only
    structural colspan/rowspan survive).
    """
    soup = BeautifulSoup(raw, "lxml")
    root = soup.select_one(".mw-parser-output") or soup.body or soup

    for sel in DROP_SELECTORS:
        for el in root.select(sel):
            el.decompose()
    _drop_boilerplate_sections(root)

    for el in root.find_all(["colgroup", "col"]):  # layout-only table scaffolding
        el.decompose()
    for tr in root.find_all("tr"):                 # empty rows
        if not tr.get_text(strip=True):
            tr.decompose()
    for a in root.find_all("a"):                   # links -> their visible text
        a.unwrap()

    for tag in root.find_all(True):                # drop class/style/id/etc.
        tag.attrs = {k: v for k, v in tag.attrs.items()
                     if tag.name in ("td", "th") and k in ("colspan", "rowspan")}

    return root.decode_contents()


def _drop_boilerplate_sections(root) -> None:
    """Remove boilerplate H2 sections and everything until the next H2.

    Wikipedia wraps headings as <div class="mw-heading"><h2 id=...>Text</h2></div>,
    so a section runs from one such wrapper to the next.
    """
    for h2 in root.find_all("h2"):
        if h2.get_text(strip=True).lower() not in BOILERPLATE_SECTIONS:
            continue
        wrapper = h2.find_parent(class_="mw-heading") or h2
        for sib in list(wrapper.find_next_siblings()):
            classes = sib.get("class") or []
            if sib.name == "h2" or "mw-heading2" in classes:
                break
            sib.decompose()
        wrapper.decompose()


def html_to_markdown(cleaned_html: str) -> str:
    """Convert cleaned HTML to GitHub-flavoured Markdown via pandoc."""
    result = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none"],
        input=cleaned_html.encode("utf-8"),
        capture_output=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed: {result.stderr.decode('utf-8', 'replace')[:200]}")
    return result.stdout.decode("utf-8", "replace")


_EDIT_RE = re.compile(r"\[edit\]")
_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")


def postprocess(md: str) -> str:
    """Tidy pandoc output, then hand off to the shared cleaner.

    clean_body does the heavy lifting: it re-renders the inline HTML tables
    pandoc leaves behind, strips <div>/<span>/colgroup scaffolding and stray
    inline tags, undoes backslash-escapes, folds Unicode-space noise, and drops
    empty sections + blank runs."""
    md = _EDIT_RE.sub("", md)
    md = html_lib.unescape(md)
    md = _strip_md_boilerplate(md)
    return clean_body(md)


def _strip_md_boilerplate(md: str) -> str:
    """Safety net: drop any boilerplate sections that survived as Markdown headings."""
    out: list[str] = []
    skip_level: int | None = None
    for line in md.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            level, heading = len(m.group(1)), m.group(2).strip().lower()
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if heading in BOILERPLATE_SECTIONS:
                skip_level = level
                continue
        if skip_level is None:
            out.append(line)
    return "\n".join(out)


# ---- Document & I/O --------------------------------------------------------

_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(title: str) -> str:
    return _UNSAFE_CHARS_RE.sub("_", title).strip().replace(" ", "_")[:200]


def output_path(out_root: Path, title: str) -> Path:
    safe = sanitize_filename(title)
    shard = safe[0].upper() if safe and safe[0].isalnum() else "_"
    return out_root / shard / f"{safe}.md"


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_document(title: str, url: str, categories: list[str], body: str) -> str:
    cats_yaml = "\n".join(f'  - "{yaml_escape(c)}"' for c in categories)
    return (
        "---\n"
        f'title: "{yaml_escape(title)}"\n'
        'source: "Wikipedia"\n'
        f'url: "{url}"\n'
        + (f"categories:\n{cats_yaml}\n" if categories else "")
        + "---\n\n"
        + body
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{id(content)}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ---- Pipeline --------------------------------------------------------------

def process_one(api_url: str, lang: str, rec: dict, out_root: Path, sleep: float) -> str:
    """Fetch, clean, convert, and write one article. Returns a status string."""
    title, html, cats = api_parse(api_url, rec["pageid"])
    body = postprocess(html_to_markdown(clean_html(html)))
    if len(body.strip()) < MIN_BODY_CHARS:
        return "empty"
    url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
    atomic_write(output_path(out_root, title), build_document(title, url, cats, body))
    time.sleep(sleep)
    return "ok"


def load_articles(path: Path, letters: set[str] | None, limit: int | None) -> list[dict]:
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if letters:
        recs = [r for r in recs if (sanitize_filename(r["title"])[:1].upper() or "_") in letters]
    return recs[:limit] if limit else recs


def cmd_scrape(args) -> int:
    api_url = f"https://{args.lang}.wikipedia.org/w/api.php"
    letters = {s.strip().upper() for s in args.letters.split(",")} if args.letters else None
    recs = load_articles(args.in_path, letters, args.limit)

    pending = recs if args.overwrite else [
        r for r in recs if not output_path(args.out, r["title"]).exists()
    ]
    skipped = len(recs) - len(pending)
    print(f"{len(recs)} listed | {skipped} already done | {len(pending)} to scrape "
          f"-> {args.out}/  ({args.workers} workers)", file=sys.stderr)

    counts = {"ok": 0, "empty": 0, "error": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, api_url, args.lang, r, args.out, args.sleep): r
                for r in pending}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="scraping", unit="art"):
            rec = futs[fut]
            try:
                counts[fut.result()] += 1
            except Exception as exc:  # one bad page must not sink the batch
                counts["error"] += 1
                failures.append(f"{rec['title']} (id {rec['pageid']}): {exc}")

    print(f"\nDone: {counts['ok']} written, {counts['empty']} empty, "
          f"{counts['error']} failed.", file=sys.stderr)
    if failures:
        print("Failures:", file=sys.stderr)
        for f in failures[:20]:
            print(f"  - {f}", file=sys.stderr)
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more", file=sys.stderr)
    return 0


# ============================================================================
# CLI
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="wookiee-wikipedia", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True, metavar="{crawl,scrape}")

    # -- crawl ----------------------------------------------------------------
    pc = sub.add_parser("crawl", help="compile the Star Wars article list",
                        description=CRAWL_DESC,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pc.add_argument("--category", default="Star Wars", help='Root category (default: "Star Wars").')
    pc.add_argument("--lang", default="en", help="Wikipedia language subdomain (default: en).")
    pc.add_argument("--max-depth", type=int, default=None,
                    help="Max subcategory levels to descend (default: unlimited, cycle-guarded).")
    pc.add_argument("--out", type=Path, default=paths.WIKIPEDIA_DIR, help="Output directory (default: corpus/wikipedia).")
    pc.add_argument("--sleep", type=float, default=0.1, help="Seconds between API requests (default: 0.1).")
    pc.add_argument("--exclude", default=",".join(EXCLUDE_DEFAULT),
                    help="Comma-separated subcategory names to skip (default: maintenance cats; "
                         'pass --exclude "" to descend into everything).')
    pc.add_argument("--keep-redirects", action="store_true",
                    help="Keep redirect pages (aliases); by default they are filtered out.")
    pc.add_argument("--wikiproject", action="store_true",
                    help="Seed from the WikiProject Star Wars banner (curated, far more complete) "
                         "instead of crawling the category tree.")
    pc.add_argument("--both", action="store_true",
                    help="Run BOTH the category tree and the WikiProject banner and union the "
                         "results, tagging each article with its source(s). Most complete (~1000).")
    pc.add_argument("--project-category", default=PROJECT_CATEGORY,
                    help=f'WikiProject banner category for --wikiproject/--both (default: "{PROJECT_CATEGORY}").')
    pc.add_argument("--keep-disambiguation", action="store_true",
                    help="Keep disambiguation pages; by default these navigation stubs are dropped.")
    pc.set_defaults(func=cmd_crawl)

    # -- scrape ---------------------------------------------------------------
    ps = sub.add_parser("scrape", help="download listed articles into clean Markdown",
                        description=SCRAPE_DESC,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ps.add_argument("--in", dest="in_path", type=Path, default=paths.WIKIPEDIA_DIR / "articles.jsonl",
                    help="Article list to scrape (default: corpus/wikipedia/articles.jsonl).")
    ps.add_argument("--out", type=Path, default=paths.WIKIPEDIA_DIR, help="Output root (default: corpus/wikipedia).")
    ps.add_argument("--lang", default="en", help="Wikipedia language subdomain (default: en).")
    ps.add_argument("--letters", help="Comma-separated shard letters to limit to (e.g. A,B,C).")
    ps.add_argument("--limit", type=int, help="Only process the first N articles (after --letters).")
    ps.add_argument("--workers", type=int, default=4, help="Concurrent fetch/convert workers (default: 4).")
    ps.add_argument("--sleep", type=float, default=0.1, help="Seconds each worker waits after a write (default: 0.1).")
    ps.add_argument("--overwrite", action="store_true", help="Re-scrape articles even if their file exists.")
    ps.set_defaults(func=cmd_scrape)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
