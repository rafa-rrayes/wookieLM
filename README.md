# wookieeLM

A pipeline for building a **multi-source Star Wars text corpus** and turning it
into training data for LLMs. The end goal is two artifacts:

1. A clean **Markdown corpus** assembled from several Star Wars sources (for pretraining).
2. A large synthetic **facts dataset** generated from that corpus (for SFT).

The project started as a Wookieepedia-only pipeline and has since grown to pull
in other sources. [`docs/data_sources.md`](docs/data_sources.md) is the full
catalog of *candidate* Star Wars text sources (an over-inclusive roadmap); what's
actually built so far is the subset below.

## Repository layout

```
README.md                      this file
pyproject.toml                 package metadata + console-script entry points
docs/data_sources.md           full catalog of candidate sources (roadmap)
tasks/                         working notes (todo, lessons)

src/wookielm/                  the pipeline (one module per stage)
  paths.py                       single source of truth for the on-disk layout
  wookieepedia_to_markdown.py    Wookieepedia XML dump → Markdown
  wikipedia.py                   Wikipedia SW corpus: crawl list + scrape → Markdown
  subtitles.py                   subtitles → cleaned transcripts
  extract_books.py               novel PDFs → plain text
  generate_fact.py               Markdown → self-contained facts (Gemini/Ollama)
  fetch_continuity.py            pull canon/Legends title lists from Wookieepedia
  tag_continuity.py              stamp continuity: into Wookieepedia frontmatter
  count.py                       corpus stats

corpus/                        all data (gitignored — rebuilt by the pipeline)
  wookieepedia/                  Markdown corpus (171k+ pages)
  wikipedia/                     Markdown corpus + article list (articles.jsonl/.txt)
  subtitles/                     cleaned transcripts + manifest.jsonl
  scripts/                       movie scripts (added manually)
  books/                         novel text (extracted from PDFs)
  facts_dataset/                 generated facts (mirrors the source corpus layout)

continuity/                    tracked canon/Legends/non-canon title lists
```

Every stage reads its default input/output directories from
[`src/wookielm/paths.py`](src/wookielm/paths.py), so the corpus location has a
single source of truth. Override any of them with the relevant CLI flag.

## Corpus (current)

Snapshot from `uv run wookiee-count` (token estimate is `bytes / 4`):

| Source | Files | Content Size | ~Tokens |
|---|---:|---:|---:|
| subtitles | 371 | 4.2 MB | 1,094,520 |
| scripts | 8 | 1.2 MB | 319,689 |
| wookieepedia | 171,441 | 347.2 MB | 91,026,094 |
| wikipedia | 1,003 | 17.0 MB | 4,451,050 |
| facts | 6,996 | 66.7 MB | 17,496,842 |
| books | 50 | 29.7 MB | 7,782,508 |
| **TOTAL** | **179,869** | **466.0 MB** | **122,170,704** |

## Pipeline

```
docs/data_sources.md  ← full catalog of candidate sources (roadmap)

SOURCES                                          MARKDOWN CORPUS              SFT
───────────────────────────────────────────────  ────────────────            ───
Wookieepedia XML dump ── wookiee-wookieepedia ──┐  corpus/wookieepedia/
Wikipedia (SW) ───────── wookiee-wikipedia crawl ┤  corpus/wikipedia/   ── wookiee-generate-fact ──► corpus/facts_dataset/
                         └ wookiee-wikipedia scrape ─►                       (self-contained facts, JSONL)
OpenSubtitles / .srt ─── wookiee-subtitles ─────┤  corpus/subtitles/  ┐
Novel PDFs ───────────── wookiee-extract-books ─┤  corpus/books/      ┼─► pretraining (direct)
Movie scripts (manual) ────────────────────────┘  corpus/scripts/    ┘
```

`wookiee-generate-fact` consumes Markdown pages with YAML frontmatter, so it runs
on the `wookieepedia/` and `wikipedia/` corpora (which share that format). The
subtitle transcripts, novel text, and movie scripts feed pretraining directly.

## Setup

Python 3.14+, managed with [uv](https://github.com/astral-sh/uv):

```bash
uv sync
```

This installs the dependencies and the `wookiee-*` console commands into the
project venv. External tools and keys, by step:

- **pandoc** on PATH — HTML/wikitext → Markdown (`wookiee-wookieepedia`,
  `wookiee-wikipedia scrape`). `brew install pandoc`
- **7z** — to unpack the Wookieepedia dump.
- **`requests`** — only for `wookiee-subtitles api` mode. Pull it in ad hoc with
  `uv run --with requests wookiee-subtitles api`, or install the extra:
  `uv sync --extra subtitles`.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — for `wookiee-generate-fact` (unless using `--ollama`).
- `OPENSUBTITLES_API_KEY` — for `wookiee-subtitles api` mode.

## Commands

### `wookiee-wookieepedia` — XML dump → Markdown

Streams the Fandom XML dump and writes one Markdown file per article into
`corpus/wookieepedia/<First-Letter>/<Page_Title>.md`. Uses multiprocessing and
aggressive quality filters (drops redirects, HTML noise, inline templates) so the
output suits LLM training. Infobox detection uses a parameter-count heuristic, so
it covers `SpaceStation`, `ShipSeries`, `CelestialBody`, etc. without a
hand-maintained whitelist.

```bash
# Get the dump (same one Fandom publishes):
wget https://s3.amazonaws.com/wikia_xml_dumps/s/st/starwars_pages_current.xml.7z
7z x starwars_pages_current.xml.7z

uv run wookiee-wookieepedia starwars_pages_current.xml   # → corpus/wookieepedia/
```

Resumable: already-converted files are skipped unless `--force`.

### `wookiee-wikipedia` — Wikipedia SW corpus (two stages)

One tool with two subcommands, run in order.

**`crawl`** compiles the list of Star Wars articles via the MediaWiki API
(`docs/data_sources.md` #3). Three strategies: a breadth-first walk of the
`Category:Star Wars` tree (default, ~850 articles), the human-curated
`--wikiproject` banner (~thousands, includes cast/crew/production), or `--both`
to union them (~1000, most complete). Redirects and disambiguation pages are
filtered out by default. Writes `articles.jsonl`, `articles.txt`, and
`categories.txt` into `--out` (default `corpus/wikipedia`).

```bash
uv run wookiee-wikipedia crawl                  # category tree (~850)
uv run wookiee-wikipedia crawl --both           # category tree ∪ WikiProject (~1000)
```

**`scrape`** reads `corpus/wikipedia/articles.jsonl` and downloads each article's
rendered HTML via the API (not raw wikitext, so infoboxes and episode tables
survive), strips chrome (references, navboxes, images, See-also/External-links
sections), converts to GitHub-flavoured Markdown with pandoc, cleans the
leftover tables/HTML, and writes one file per article mirroring the Wookieepedia
layout and frontmatter — so both feed the same `wookiee-generate-fact` pipeline.

```bash
uv run wookiee-wikipedia scrape                 # scrape all listed articles
uv run wookiee-wikipedia scrape --limit 5       # smoke test
uv run wookiee-wikipedia scrape --workers 8 --overwrite
```

Resumable: existing files are skipped unless `--overwrite`. Polite by default
(descriptive User-Agent, `maxlag=5`, `--sleep`, backoff).

### `wookiee-subtitles` — subtitles → cleaned transcripts

Gathers English Star Wars subtitles into a transcript corpus. `api` mode
downloads via the official OpenSubtitles REST API (bulk-scraping subtitle sites
violates their terms, so the API is the only automatic path); `local` mode
processes `.srt`/`.vtt` files you already have. Both run the same cleaning
pipeline and emit `<out>/<title-slug>/<slug>.txt` plus a `manifest.jsonl`
(default `--out` is `corpus/subtitles`).

```bash
uv run wookiee-subtitles list                                  # built-in title list
export OPENSUBTITLES_API_KEY=...
uv run --with requests wookiee-subtitles api                   # → corpus/subtitles
uv run --with requests wookiee-subtitles local --in ./my_subs
```

### `wookiee-extract-books` — novel PDFs → text

Extracts the text layer from the Star Wars novel PDFs in `corpus/books/` into
`.txt` files (keeping front/back matter). Skips PDFs that already have a `.txt`
unless `--force`.

```bash
uv run wookiee-extract-books            # extract all PDFs lacking a .txt
uv run wookiee-extract-books --force    # re-extract even if .txt exists
```

### `wookiee-generate-fact` — Markdown → self-contained facts (Gemini / Ollama)

Walks an input corpus, splits oversized pages into overlapping,
breadcrumb-prefixed chunks (the heading chain is prepended so a chunk read in
isolation still says what article it came from), and asks the model to extract N
**self-contained** facts per chunk via structured JSON. Facts that lean on the
source (e.g. "described in the passage", "this droid") are dropped before writing,
since the fine-tuned model never sees the source. Output is JSONL, one file per
page, mirroring the input layout. Resumable: pages with existing output are
skipped unless `--overwrite`/`--append`.

```bash
# quick test on a handful of pages
uv run wookiee-generate-fact --letters A --limit 5

# full run over the Wookieepedia corpus
uv run wookiee-generate-fact --skip-legends --concurrency 16

# a single named article, regenerated
uv run wookiee-generate-fact --article "Anakin Skywalker" --overwrite

# point it at the Wikipedia corpus instead
uv run wookiee-generate-fact --input-dir corpus/wikipedia --output-dir corpus/facts_dataset
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--input-dir` | `corpus/wookieepedia` | Markdown corpus to read |
| `--output-dir` | `corpus/facts_dataset` | Where JSONL is written |
| `--model` | `gemini-3.1-flash-lite` | Gemini model name |
| `--ollama` | off | Use a local Ollama model instead of Gemini (e.g. `--ollama gemma3`) |
| `--facts-per-chunk` | `50` | Facts requested per chunk |
| `--max-words` | `6000` | Split pages larger than this |
| `--overlap-ratio` | `0.2` | Fraction of each chunk repeating the previous chunk's tail (0 disables) |
| `--min-words` | `80` | Skip stub pages |
| `--concurrency` | `1` | In-flight requests |
| `--letters A,B` | all | Only these top-level dirs |
| `--article "X,Y"` | — | Exact article names (overrides `--letters`/`--min-words`) |
| `--limit N` | `0` | Stop after N pages |
| `--skip-legends` | off | Drop non-canon `*_Legends.md` pages |
| `--shuffle` | off | Randomize order (useful for sampling) |
| `--overwrite` | off | Regenerate even if output exists |
| `--append` | off | Accumulate facts across passes instead of skipping existing output |
| `--dry-run` | off | Print plan (with chunk counts) without calling the API |
| `--price-in` / `--price-out` | `0.25` / `1.50` | USD per 1M input/output tokens, for the live cost meter |

The progress bar shows a live cost estimate from each response's
`usage_metadata`, and a final summary prints total tokens and cost. To estimate
before committing to a full run, sample with `--shuffle --limit 2000`. (Ollama
runs are free, so the meter reads `$0`.)

### `wookiee-fetch-continuity` / `wookiee-tag-continuity` — canon/Legends split

The Wookieepedia era banner that marks an article as canon or Legends is
stripped during Markdown conversion. `wookiee-fetch-continuity` pulls the
authoritative title lists back from the live MediaWiki API into `continuity/`,
and `wookiee-tag-continuity` stamps a `continuity:` field into each page's
frontmatter by matching titles.

```bash
uv run wookiee-fetch-continuity                    # cache canon/Legends/non-canon title lists
uv run wookiee-tag-continuity --dry-run            # report the distribution, write nothing
uv run wookiee-tag-continuity                       # apply continuity: tags to all pages
```

### `wookiee-count` — corpus stats

Reports file count, size, and a rough token estimate per source, plus the
pages with the most generated facts.

```bash
uv run wookiee-count
```

## Output formats

Markdown article (`corpus/wookieepedia/L/Luke_Skywalker.md`; `corpus/wikipedia/`
is the same shape with `source: "Wikipedia"` and a `url:`):

```markdown
---
title: "Luke Skywalker"
source: "Wookieepedia"
continuity: canon
categories:
  - "Jedi Masters of the Jedi Order"
  - ...
---

## Infobox

- **Name:** Luke Skywalker
- **Homeworld:** Tatooine
- ...

## Biography

...
```

Facts JSONL (`corpus/facts_dataset/L/Luke_Skywalker.jsonl`), one record per line:

```json
{"fact": "Luke Skywalker was born on Polis Massa in 19 BBY.", "source_page": "Luke Skywalker"}
```
