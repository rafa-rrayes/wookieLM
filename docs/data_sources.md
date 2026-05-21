# Star Wars Corpus — Exhaustive Data Source Catalog

A maximalist inventory of every text-bearing Star Wars data source worth considering for an LLM training corpus. This is intentionally over-inclusive — prune later. Dedup, structuring, and licensing are deliberately out of scope here.

## Column legend

- **Acquisition** — how you'd actually get the text: `XML dump`, `scrape`, `API`, `OCR` (scan→text), `ASR` (audio→text), `subtitle file`, `data-mine` (extract from game files), `manual`.
- **Lang** — `ML` = pull multiple languages; `EN` = English-dominant; `JP` = strong Japanese catalog.
- **Volume** — rough relative token yield: `XXL` > `XL` > `L` > `M` > `S` > `XS`.
- **Effort** — relative work to acquire + clean: `1` (trivial, reuse XML pipeline) → `5` (brutal: OCR/ASR/data-mining at scale).
- **Canon reliability** — value for *factual recall* specifically. `High` = authoritative; `Med` = mostly reliable; `Low` = contradicts/invents canon; `N/A` = real-world meta (true, but not in-universe).

---

## 1. Wiki & encyclopedic

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 1 | Wookieepedia (English) | XML dump | EN | XXL | 1 | High | Your backbone. 171k+ pages. |
| 2 | Wookieepedia (other languages) | XML dump | ML | XL | 1 | High | German, French, Spanish, Russian, Polish, etc. Same pipeline. |
| 3 | Wikipedia — SW category tree | XML dump | ML | L | 2 | N/A | Out-of-universe register: production, box office, bios. |
| 4 | Simple English Wikipedia (SW) | XML dump | EN | XS | 1 | N/A | Simplified phrasings; cheap recall reinforcement. |
| 5 | Wikiquote (SW) | XML dump / scrape | ML | S | 1 | High | Curated, pre-transcribed lines. |
| 6 | Other SW Fandom wikis | XML dump | ML | M | 1 | Med–High | Per-game wikis, SWTOR wiki, fan-project wikis. |
| 7 | Fandom talk / discussion pages | XML dump | ML | M | 2 | Low | Already in dumps unless filtered. Editorial, not lore. |
| 8 | Independent lore wikis | scrape | EN | S | 3 | Med | Non-Fandom encyclopedias, niche timeline sites. |
| 9 | Wookieepedia revision history | XML dump (full) | EN | XXL | 4 | Med | Older revisions = near-dup noise; usually skip. |

## 2. Structured data

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 10 | Wikidata | API / SPARQL | ML | M | 2 | High | Triples → templated Q&A with no LLM cost. Top recall value. |
| 11 | DBpedia | dump / SPARQL | ML | M | 2 | High | Overlaps Wikidata, different extraction, sometimes richer text. |
| 12 | StarWars.com Databank | scrape / API | EN | M | 3 | High | Official entity entries. |
| 13 | Fan databases (timelines, registries) | scrape | EN | S | 3 | Med–High | Species lists, ship registries, character indexes, chronologies. |
| 14 | Wookieepedia infobox extracts | from your dump | ML | M | 1 | High | You already parse these — emit as standalone fact tables too. |

## 3. Scripts & screen text

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 15 | Movie scripts (films) | scrape | EN | M | 2 | High | IMSDb, Script Slug. Shooting vs. transcript quality varies. |
| 16 | TV scripts / transcripts | scrape | EN | L | 3 | High | Clone Wars, Rebels, Andor, etc. Often fan-transcribed. |
| 17 | Subtitles — films | subtitle file | ML | M | 2 | High | OpenSubtitles. Strip timing. |
| 18 | Subtitles — series | subtitle file | ML | XL | 2 | High | Near-complete spoken coverage of all shows. Best ML dialogue. |
| 19 | Closed-caption rips | data-mine | ML | M | 4 | High | Where no subtitle exists; rip from disc/stream. |
| 20 | Theme-park ride/show scripts | scrape / manual | EN | XS | 3 | Med | Galaxy's Edge, attraction spiels — canon-adjacent. |

## 4. Published prose

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 21 | Novels — canon | OCR / ebook | ML | XL | 4 | High | Hundreds of titles. Richest narrative prose. |
| 22 | Novels — Legends | OCR / ebook | ML | XXL | 4 | Med | Huge EU back-catalog. Tag as Legends. |
| 23 | Young-reader / junior novels | OCR / ebook | ML | L | 4 | Med–High | Simplified retellings; good recall reinforcement. |
| 24 | Reference books | OCR | EN | L | 4 | High | Visual Dictionaries, Essential Guides, technical manuals. Dense facts. |
| 25 | Comics & graphic novels | OCR | EN | L | 5 | Med–High | Dialogue + captions. OCR of panels is painful. |
| 26 | Manga adaptations | OCR | JP/ML | M | 5 | Med | Strong JP catalog; vertical text OCR is hard. |
| 27 | RPG sourcebooks (WEG d6, Saga, FFG) | OCR | EN | L | 4 | High | Extremely dense lore. Underrated for recall. |
| 28 | Magazines (Insider, etc.) | OCR / scrape | EN | M | 4 | Med | Lore + interviews mixed. |
| 29 | Roleplaying adventure modules | OCR | EN | M | 4 | Med | Scenario text, NPC stats, location lore. |
| 30 | Art / "making of" books | OCR | EN | S | 4 | N/A | Production commentary, concept text. |
| 31 | Cookbooks / lifestyle tie-ins | OCR | EN | XS | 4 | Med | (Yes, these exist — in-universe flavor text.) |

## 5. Game text

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 32 | Dialogue trees | data-mine | ML | XL | 4 | High | KOTOR, SWTOR, Fallen Order, Jedi Survivor. SWTOR alone is enormous. |
| 33 | In-game codex / lore entries | data-mine | ML | L | 4 | High | Often the densest in-universe encyclopedic text. |
| 34 | Item / unit / ability descriptions | data-mine | ML | M | 4 | High | Galaxy of Heroes, card games, strategy titles. |
| 35 | Quest / mission text | data-mine | ML | L | 4 | Med | Objectives, briefings, journals. |
| 36 | Strategy guides | OCR / scrape | EN | M | 4 | Low | Walkthroughs; mostly meta, light lore. |
| 37 | Game manuals | OCR | ML | S | 3 | Med | Backstory sections. |

## 6. Spoken audio → text (ASR)

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 38 | YouTube — lore/official channels | ASR | ML | XL | 4 | Med | Your entry. Quality varies wildly by channel. |
| 39 | Audiobooks | ASR | ML | XL | 5 | High | If no ebook source; otherwise prefer text. |
| 40 | Radio dramas (NPR dramatizations) | ASR | EN | M | 4 | High | Canon-adjacent original-trilogy adaptations. |
| 41 | Audio dramas / full-cast originals | ASR | EN | M | 5 | Med–High | |
| 42 | Official podcasts | ASR | EN | M | 4 | N/A | Behind-the-scenes, interviews. |
| 43 | DVD/Blu-ray commentaries | ASR | EN | M | 5 | N/A | Creator commentary tracks. |
| 44 | Featurettes / EPK / BTS | ASR | EN | S | 5 | N/A | |
| 45 | Convention panels (Celebration, etc.) | ASR | EN | M | 5 | N/A | Q&A, announcements. |
| 46 | Cast/crew interviews (video/audio) | ASR | ML | M | 5 | N/A | |

## 7. Community & discussion

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 47 | Reddit (lore subs) | API / dump | EN | XL | 2 | Low–Med | Natural Q&A register. Pushshift-style dumps. |
| 48 | Forums (TFN, Jedi Council, SW.com) | scrape | EN | XL | 3 | Low–Med | Decades of discussion. |
| 49 | Fan fiction (AO3, FFN) | scrape / dump | ML | XXL | 3 | Low | Massive in-universe prose, but non-canon. Hurts recall. |
| 50 | Discord lore servers | API | EN | M | 4 | Low | Hard to access at scale; ToS issues. |
| 51 | Stack-Exchange (Sci-Fi & Fantasy SE) | dump | EN | M | 1 | Med | Clean Q&A dumps; SW-tagged questions are gold for recall. |
| 52 | Quora SW topics | scrape | EN | M | 3 | Low | Q&A register, unreliable facts. |
| 53 | Blog / fan-site articles | scrape | EN | L | 3 | Low–Med | Lore explainers, theory posts. |
| 54 | Tumblr / social lore posts | scrape / API | EN | M | 4 | Low | |

## 8. Real-world / meta

| # | Source | Acquisition | Lang | Volume | Effort | Canon reliability | Notes |
|---|--------|-------------|------|--------|--------|-------------------|-------|
| 55 | News & reviews coverage | scrape | ML | L | 3 | N/A | Release coverage, criticism. |
| 56 | Academic papers / theses on SW | scrape / API | EN | M | 3 | N/A | Cultural studies, film analysis. |
| 57 | Press kits / EPK text | scrape | EN | S | 3 | N/A | |
| 58 | Trading-card flavor text (CCG/LCG) | scrape / OCR | EN | M | 3 | Med | Dense lore per token; data-mining wikis often have it. |
| 59 | Merchandise / packaging copy | OCR / scrape | EN | S | 4 | Med | Toy bios (Kenner/Hasbro bios are surprisingly lore-rich). |
| 60 | Action-figure / collectible bios | scrape | EN | S | 3 | Med | Cardback bios — concise canon snippets. |
| 61 | Official style guides / brand bibles | manual | EN | XS | 5 | High | Rare, internal; mostly inaccessible. |
| 62 | Patents / legal filings (props, tech) | scrape | EN | XS | 4 | N/A | Trivia-tier. |
| 63 | LEGO SW set descriptions / instructions | scrape | EN | S | 3 | Med | Set names + minifig lore. |
| 64 | Museum / exhibition catalog text | OCR | EN | XS | 4 | N/A | Touring SW exhibitions. |

---

## Quick-start priority (value × ease, for a recall-maximizing corpus)

**Do first (reuse XML pipeline / clean dumps):** 1, 2, 3, 5, 6, 10, 11, 51
**High value, moderate effort:** 12, 14, 15, 16, 17, 18, 27, 47
**Big payoff, heavy lifting:** 21, 22, 24, 32, 33, 39
**Include only if maximizing volume (cuts against recall):** 49, 48, 52, 53, 54

## Flagged caveats (kept for honesty, not blended in)

- **Fan fiction / forums / Quora / Tumblr (47–54 partial):** enormous volume, but they teach plausible-sounding *falsehoods*. They actively work against a factual-recall goal. Tag heavily or weight down.
- **Revision history (9):** explodes token count with near-duplicates. Usually skip.
- **Machine-translated fan content (any ML scrape):** can poison multilingual quality. Prefer human-authored ML dumps (Wookieepedia, subtitles) over scraped translations.
- **OCR-heavy sources (21–31, comics especially):** budget for cleanup; comic/manga panel OCR is the worst offender.