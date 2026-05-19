# wookieLM

A small pipeline for building a Star Wars text corpus from Wookieepedia and
turning it into training data for LLMs. The end goal is two artifacts:

1. A clean Markdown corpus of every Wookieepedia article (for pretraining).
2. A large synthetic Q&A dataset generated from those articles (for SFT).

The Markdown corpus is organized by article title (one file per page). There are currently 171,440 markdown files.

## Pipeline

```
starwars_pages_current.xml.7z         ← Wookieepedia XML dump (raw)
        │  unpack
        ▼
starwars_pages_current.xml
        │  wookieepedia_to_markdown.py ← clean Markdown, YAML frontmatter
        ▼
wookieepedia/<Letter>/<Page>.md
        │  generate_qa.py  (Gemini)
        ▼
qa_dataset/<Letter>/<Page>.jsonl      ← question/answer pairs per page
```

## Getting the data

The XML dump is the same one Fandom publishes:

```bash
wget https://s3.amazonaws.com/wikia_xml_dumps/s/st/starwars_pages_current.xml.7z
7z x starwars_pages_current.xml.7z
```

## Setup

Python 3.13+, managed with [uv](https://github.com/astral-sh/uv):

```bash
uv sync
```

For the Q&A step, set a Gemini API key:

```bash
export GEMINI_API_KEY=...
```

## Scripts

### `wookieepedia_to_markdown.py`

Streams the XML dump and writes one Markdown file per article into
`wookieepedia/<First-Letter>/<Page_Title>.md`. Uses multiprocessing and an
aggressive set of quality filters (drops redirects, HTML noise, inline
templates) so the output is suitable for LLM training. Infobox detection
uses a parameter-count heuristic (any template with enough `|key=value` 
pairs is treated as an infobox), so it covers `SpaceStation`, `ShipSeries`,
`CelestialBody`, etc. without a hand-maintained whitelist.

```bash
uv run wookieepedia_to_markdown.py
```

### `generate_qa.py`

Walks `wookieepedia/`, splits oversized pages into ~6000-word chunks
(preserving the title and a `[Chunk i of n]` header on every chunk), and
asks Gemini to produce N grounded Q&A pairs per chunk using structured JSON
output. Results are written as JSONL, one file per source page, mirroring
the input directory layout.

```bash
# quick test on a handful of pages
uv run generate_qa.py --letters A --limit 5

# full run (resumable: pages with existing output are skipped)
uv run generate_qa.py --skip-legends --concurrency 16
```

Useful flags:

| Flag | Default | Purpose |
|---|---|---|
| `--model` | `gemini-3.1-flash-lite` | Gemini model name |
| `--qa-per-chunk` | `100` | Q&A pairs requested per chunk |
| `--chunk-words` | `6000` | Split pages larger than this |
| `--min-words` | `80` | Skip stub pages |
| `--concurrency` | `8` | In-flight API requests |
| `--letters A,B` | all | Only process these top-level dirs |
| `--limit N` | `0` | Stop after N pages |
| `--skip-legends` | off | Drop non-canon `*_Legends.md` pages |
| `--shuffle` | off | Randomize order (useful for sampling) |
| `--overwrite` | off | Regenerate even if output exists |
| `--dry-run` | off | Print plan without calling the API |
| `--price-in` / `--price-out` | `0.25` / `1.50` | USD per 1M input/output tokens, for the live cost meter |

The progress bar shows a live cost estimate (`$0.0123`) computed from each
response's `usage_metadata`, and a final summary prints total tokens and
cost.

## Cost

Measured on a 1,000-page sample with `--model gemini-3.1-flash-lite`:

| Metric | Value |
|---|---|
| Pages | 1,000 |
| Q&A pairs generated | 20,269 (~20 per page) |
| Input tokens | 840,866 |
| Output tokens | 906,807 |
| Wall time | 7m 09s (concurrency 8) |
| Cost | **$1.57** |

Per-unit:

- **$0.00157 per page** (~$1.57 / 1k pages)
- **$0.0000775 per Q&A pair** (~$77 per 1M pairs)
- Output tokens drive ~87% of cost; input is negligible by comparison.

Extrapolating to the full corpus:  **~$270 total**, give or take.

For an estimate before committing, run a shuffled sample:

```bash
uv run generate_qa.py --shuffle --limit 2000
```

## Output formats

Markdown article (`wookieepedia/L/Luke_Skywalker.md`):

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

Q&A JSONL (`qa_dataset/L/Luke_Skywalker.jsonl`), one record per line:

```json
{"question": "Where was Luke Skywalker born?", "answer": "Polis Massa, 19 BBY.", "source_page": "Luke Skywalker", "source_path": "wookieepedia/L/Luke_Skywalker.md", "chunk_index": 0, "chunk_count": 8, "model": "gemini-3.1-flash-lite"}
```

## Layout

```
wookieepedia_to_markdown.py    XML → Markdown converter
generate_qa.py                 Markdown → Q&A via Gemini
wookieepedia/                   Markdown corpus (171k+ pages)
qa_dataset/                    Generated Q&A pairs (mirrors wookieepedia/)
starwars_pages_current.xml     Raw Wookieepedia dump
nanochat/                      Training framework (see its own README)
```
