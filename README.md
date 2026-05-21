# wookieLM

A pipeline for building a **multi-source Star Wars text corpus** and turning it
into training data for LLMs. The end goal is two artifacts:

1. A clean **Markdown corpus** assembled from several Star Wars sources (for pretraining).
2. A large synthetic **facts dataset** generated from that corpus (for SFT).

The project started as a Wookieepedia-only pipeline and has since grown to pull
in other sources. [`data_sources.md`](data_sources.md) is the full catalog of
*candidate* Star Wars text sources (an over-inclusive roadmap); what's actually
built so far is the subset below.

## Corpus (current)

Snapshot from `uv run count.py` (token estimate is `bytes / 4`):

| Source | Built by | Files | Size | ~Tokens |
|---|---|--:|--:|--:|
| Wookieepedia | `wookieepedia_to_markdown.py` | 171,440 | 344 MB | ~90.2M |
| Wikipedia (SW) | `wikipedia.py` (crawl + scrape) | 1,003 | 14.7 MB | ~3.9M |
| Subtitles | `subtitles.py` | 371 | 4.2 MB | ~1.1M |
| Movie scripts | added manually (see `data_sources.md` #15) | 6 | 969 KB | ~0.25M |
| **Facts** (SFT) | `generate_fact.py` | 1,303 | 4.4 MB | ~1.2M |
| **Total** | | 174,123 | 368 MB | ~96.5M |

## Pipeline

```
data_sources.md  ← full catalog of candidate sources (roadmap)

SOURCES                                                 MARKDOWN CORPUS         SFT
─────────────────────────────────────────────────────  ───────────────         ───
Wookieepedia XML dump ── wookieepedia_to_markdown.py ──┐  wookieepedia/
Wikipedia (SW) ───────── wikipedia.py crawl ───────────┤  wikipedia/    ── generate_fact.py ──► facts_dataset/
                         └ wikipedia.py scrape ────────┼─►                  (self-contained facts, JSONL)
OpenSubtitles / .srt ─── subtitles.py ─────────────────┤  subtitles/   ┐
Movie scripts (manual) ────────────────────────────────┘  scripts/     ┴─► pretraining (direct)
```

`generate_fact.py` consumes Markdown pages with YAML frontmatter, so it runs on
the `wookieepedia/` and `wikipedia/` corpora (which share that format). The
subtitle transcripts and movie scripts feed pretraining directly.

## Setup

Python 3.14+, managed with [uv](https://github.com/astral-sh/uv):

```bash
uv sync
```

External tools and keys, by step:

- **pandoc** on PATH — HTML/wikitext → Markdown (`wookieepedia_to_markdown.py`, `wikipedia.py scrape`). `brew install pandoc`
- **7z** — to unpack the Wookieepedia dump.
- **`requests`** — only for `subtitles.py api` mode (optional dep; `uv run --with requests subtitles.py api`).
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) — for `generate_fact.py` (unless using `--ollama`).
- `OPENSUBTITLES_API_KEY` — for `subtitles.py api` mode.

## Sources & scripts

### `wookieepedia_to_markdown.py` — XML dump → Markdown

Streams the Fandom XML dump and writes one Markdown file per article into
`wookieepedia/<First-Letter>/<Page_Title>.md`. Uses multiprocessing and
aggressive quality filters (drops redirects, HTML noise, inline templates) so the
output suits LLM training. Infobox detection uses a parameter-count heuristic, so
it covers `SpaceStation`, `ShipSeries`, `CelestialBody`, etc. without a
hand-maintained whitelist.

```bash
# Get the dump (same one Fandom publishes):
wget https://s3.amazonaws.com/wikia_xml_dumps/s/st/starwars_pages_current.xml.7z
7z x starwars_pages_current.xml.7z

uv run wookieepedia_to_markdown.py starwars_pages_current.xml ./wookieepedia
```

Resumable: already-converted files are skipped unless `--force`.

### `wikipedia.py` — Wikipedia SW corpus (two stages)

One tool with two subcommands, run in order.

**`crawl`** compiles the list of Star Wars articles via the MediaWiki API
(`data_sources.md` #3). Three strategies: a breadth-first walk of the
`Category:Star Wars` tree (default, ~850 articles), the human-curated
`--wikiproject` banner (~thousands, includes cast/crew/production), or `--both`
to union them (~1000, most complete). Redirects and disambiguation pages are
filtered out by default. Writes `articles.jsonl`, `articles.txt`, and
`categories.txt` into `--out` (default `./wikipedia`).

```bash
uv run wikipedia.py crawl                  # category tree (~850)
uv run wikipedia.py crawl --both           # category tree ∪ WikiProject (~1000)
```

**`scrape`** reads `wikipedia/articles.jsonl` and downloads each article's
rendered HTML via the API (not raw wikitext, so infoboxes and episode tables
survive), strips chrome (references, navboxes, images, See-also/External-links
sections), converts to GitHub-flavoured Markdown with pandoc, cleans the
leftover tables/HTML, and writes one file per article mirroring the Wookieepedia
layout and frontmatter — so both feed the same `generate_fact.py` pipeline.

```bash
uv run wikipedia.py scrape                 # scrape all listed articles
uv run wikipedia.py scrape --limit 5       # smoke test
uv run wikipedia.py scrape --workers 8 --overwrite
```

Resumable: existing files are skipped unless `--overwrite`. Polite by default
(descriptive User-Agent, `maxlag=5`, `--sleep`, backoff).

### `subtitles.py` — subtitles → cleaned transcripts

Gathers English Star Wars subtitles into a transcript corpus. `api` mode
downloads via the official OpenSubtitles REST API (bulk-scraping subtitle sites
violates their terms, so the API is the only automatic path); `local` mode
processes `.srt`/`.vtt` files you already have. Both run the same cleaning
pipeline and emit `<out>/<title-slug>/<slug>.txt` plus a `manifest.jsonl`.

```bash
uv run subtitles.py list                              # built-in title list
export OPENSUBTITLES_API_KEY=...
uv run --with requests subtitles.py api  --out ./subtitles
uv run --with requests subtitles.py local --in ./my_subs --out ./subtitles
```

### `generate_fact.py` — Markdown → self-contained facts (Gemini / Ollama)

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
uv run generate_fact.py --letters A --limit 5

# full run over the Wookieepedia corpus
uv run generate_fact.py --skip-legends --concurrency 16

# a single named article, regenerated
uv run generate_fact.py --article "Anakin Skywalker" --overwrite

# point it at the Wikipedia corpus instead
uv run generate_fact.py --input-dir wikipedia --output-dir facts_dataset
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--input-dir` | `wookieepedia` | Markdown corpus to read |
| `--output-dir` | `facts_dataset` | Where JSONL is written |
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

### `count.py` — corpus stats

Reports file count, size, and a rough token estimate per source, plus the
pages with the most generated facts.

```bash
uv run count.py
```

## Output formats

Markdown article (`wookieepedia/L/Luke_Skywalker.md`; `wikipedia/` is the same
shape with `source: "Wikipedia"` and a `url:`):

```markdown
---
title: "Luke Skywalker"
source: "Wookieepedia"
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

Facts JSONL (`facts_dataset/L/Luke_Skywalker.jsonl`), one record per line:

```json
{"fact": "Luke Skywalker was born on Polis Massa in 19 BBY.", "source_page": "Luke Skywalker"}
```

## Layout

```
data_sources.md                full catalog of candidate sources (roadmap)
wookieepedia_to_markdown.py    Wookieepedia XML → Markdown
wikipedia.py                   Wikipedia SW corpus: crawl article list + scrape → Markdown
subtitles.py                   subtitles → cleaned transcripts
generate_fact.py               Markdown → self-contained facts (Gemini/Ollama)
count.py                       corpus stats

wookieepedia/                  Markdown corpus (171k+ pages)
wikipedia/                     Markdown corpus + article list (articles.jsonl/.txt)
subtitles/                     cleaned transcripts + manifest.jsonl
scripts/                       movie scripts (Markdown, added manually)
facts_dataset/                 generated facts (mirrors the source corpus layout)
```
