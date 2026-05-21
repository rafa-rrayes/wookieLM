"""Generate self-contained facts from Wookieepedia markdown pages using Gemini.

Walks an input directory of .md files, splits large pages into breadcrumb-prefixed
chunks (see build_chunks), and asks Gemini to extract N standalone facts per chunk
via structured JSON output. Facts that lean on the source instead of standing alone
(e.g. "described in the passage", "this droid") are dropped before writing, since
the fine-tuned model never sees the source. Results are written as JSONL, one file
per source page, mirroring the input directory layout. Resumable: pages that
already have an output file are skipped unless --overwrite is set.

Each chunk is prefixed with its heading *breadcrumb* — the chain of ancestor
headings leading to the chunk's content — so a chunk read in isolation still says
what part of what article it came from. The article title lives in the YAML
frontmatter (the markdown bodies start at H2), so it is synthesized as the H1 root
of every breadcrumb.

Usage:
    export GEMINI_API_KEY=...
    uv run wookiee-generate-fact --limit 5 --letters A
    uv run wookiee-generate-fact --article "Anakin Skywalker" --overwrite
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field
from tqdm import tqdm

from wookielm import paths

try:  # optional: only needed for the --ollama (local model) backend
    import ollama as _ollama
except ImportError:
    _ollama = None

# Transient failures worth retrying for either backend. pydantic ValidationError
# subclasses ValueError, so malformed JSON from the model is retried too.
RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    genai_errors.APIError, json.JSONDecodeError, ValueError,
    ConnectionError, TimeoutError, OSError,
)
if _ollama is not None:
    RETRYABLE_EXC += (_ollama.ResponseError,)


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")

# References to the source document itself. The trained model never sees the
# source, so a fact that points back at it is unusable. Also catches hedging
# non-answers like "the passage does not specify ...". Kept conservative so
# legitimate in-universe phrasings ("the Treaty of Coruscant", "a section of the
# galaxy") survive.
SOURCE_REF_RE = re.compile(
    r"""(?ix)
    \b(?:
        (?:the|this|that|above|following|preceding|given|provided|aforementioned)\s+
            (?:passage|text|article|page|document|excerpt|snippet|infobox|section|paragraph|content|description|entry)
      | (?:passage|text|article|page|document|excerpt|infobox|section|paragraph)\s+(?:above|below)
      | (?:according\s+to|based\s+on)\s+(?:the\s+|this\s+)?
            (?:passage|text|article|page|document|excerpt|infobox|section)
      | (?:described|mentioned|stated|shown|listed|noted|discussed|seen|referenced)\s+
            (?:above|below|here|earlier|previously)
      | as\s+(?:described|mentioned|stated|noted|shown|discussed)
      | the\s+subject(?:'s)?
    )\b
    """,
)

# A bare demonstrative standing in for the subject ("This droid was built by..."),
# which has no antecedent once the fact stands alone.
SUBJECT_SUBSTITUTION_RE = re.compile(
    r"""(?ix)
    \bthis\s+(?:droid|character|planet|ship|starship|vessel|species|person|individual|
                being|creature|weapon|battle|war|event|organization|group|vehicle|
                item|object|moon|system|location|figure)\b
    """,
)


class FactSet(BaseModel):
    facts: list[str] = Field(
        description="Self-contained factual statements, each standing alone."
    )


@dataclass
class Block:
    """One paragraph of body text together with the breadcrumb above it.

    breadcrumb is the tuple of heading lines (e.g. ``"## Biography"``) from the
    H1 title down to the section this paragraph lives in, inclusive.
    """

    breadcrumb: tuple[str, ...]
    text: str


@dataclass
class Chunk:
    title: str
    index: int
    total: int
    text: str  # already breadcrumb-prefixed; see render_chunk / build_chunks


def word_count(text: str) -> int:
    return len(text.split())


def read_article(path: Path) -> tuple[str, str]:
    """Return (title, body_without_frontmatter)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("_", " ")
    m = FRONTMATTER_RE.match(raw)
    if m:
        tm = TITLE_RE.search(m.group(1))
        if tm:
            title = tm.group(1).strip()
        body = raw[m.end():]
    else:
        body = raw
    return title, body.strip()


def split_blocks(title: str, body: str) -> list[Block]:
    """Walk the body, tagging each paragraph with its heading breadcrumb.

    Paragraphs are blank-line-separated runs of text. The breadcrumb always
    starts with the synthesized ``# {title}`` root, followed by the active body
    headings (deeper headings replace shallower siblings via the usual stack).
    """
    root = f"# {title}"
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []  # (level, heading_line)
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            crumb = (root, *(line for _, line in stack))
            blocks.append(Block(breadcrumb=crumb, text=text))
        buf = []

    for line in body.split("\n"):
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, line.strip()))
        elif not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return blocks


def render_chunk(blocks: list[Block]) -> str:
    """Render packed blocks, emitting each heading only when the branch changes.

    The first block emits its full breadcrumb (last_crumb starts empty). Later
    blocks emit only the headings that differ from what's already shown, so the
    breadcrumb appears once at the top and section headings appear inline as the
    chunk crosses into them.
    """
    parts: list[str] = []
    last_crumb: tuple[str, ...] = ()
    for blk in blocks:
        crumb = blk.breadcrumb
        i = 0
        while i < len(crumb) and i < len(last_crumb) and crumb[i] == last_crumb[i]:
            i += 1
        new_headings = crumb[i:]
        if parts:
            parts.append("")  # blank line before any new heading group or paragraph
        if new_headings:
            parts.extend(new_headings)  # stacked breadcrumb, no blanks between them
            parts.append("")            # blank line between headings and content
        parts.append(blk.text)
        last_crumb = crumb
    return "\n".join(parts)


def _tail_blocks(group: list[Block], budget: int) -> list[Block]:
    """Trailing blocks of `group` summing to ~budget words (>=1 block, never cut)."""
    out: list[Block] = []
    words = 0
    for blk in reversed(group):
        w = word_count(blk.text)
        if out and words + w > budget:
            break
        out.append(blk)
        words += w
    out.reverse()
    return out


def build_chunks(title: str, body: str, max_words: int, overlap_ratio: float = 0.2) -> list[Chunk]:
    """Split an article into breadcrumb-prefixed chunks of up to ~max_words words.

    Whole article <= max_words -> one chunk (the title as its H1). Otherwise pack
    paragraphs greedily; paragraphs are never split, so a chunk may exceed
    max_words if a single paragraph is larger.

    Consecutive chunks overlap: each chunk after the first begins with the tail of
    the previous chunk, ~overlap_ratio of max_words (whole paragraphs only). New
    content per chunk is therefore ~(1 - overlap_ratio) of max_words, so facts that
    straddle a boundary still appear in a chunk intact. overlap_ratio=0 disables it.
    """
    body = body.strip()
    if word_count(body) <= max_words:
        return [Chunk(title=title, index=0, total=1, text=f"# {title}\n\n{body}".strip())]

    blocks = split_blocks(title, body)
    overlap_words = int(max_words * overlap_ratio)
    new_budget = max(1, max_words - overlap_words)

    # Greedily pack blocks into base groups of *new* content up to new_budget.
    groups: list[list[Block]] = []
    cur: list[Block] = []
    cur_words = 0
    for blk in blocks:
        w = word_count(blk.text)
        if cur and cur_words + w > new_budget:
            groups.append(cur)
            cur, cur_words = [], 0
        cur.append(blk)
        cur_words += w
    if cur:
        groups.append(cur)

    # Prepend the tail of the previous group to each subsequent group as overlap.
    rendered: list[str] = []
    for i, group in enumerate(groups):
        if i > 0 and overlap_words > 0:
            group = _tail_blocks(groups[i - 1], overlap_words) + group
        rendered.append(render_chunk(group))

    total = len(rendered)
    return [Chunk(title=title, index=i, total=total, text=t) for i, t in enumerate(rendered)]


def build_prompt(chunk: Chunk, fact_count: int) -> str:
    location = ""
    if chunk.total > 1:
        location = f" (chunk {chunk.index + 1} of {chunk.total})"
    return f"""You are building a high-quality Star Wars knowledge dataset from \
Wookieepedia for supervised fine-tuning (SFT). A model will be trained on these \
facts and at training time it sees ONLY the fact — it will NOT have this passage \
in front of it. Every fact must therefore be completely self-contained and make \
sense on its own to a Star Wars fan who has never seen this passage.

From the passage below, extract up to {fact_count} diverse, self-contained facts.

=== SELF-CONTAINED FACTS (the most important rule) ===
- Each fact is a single, standalone declarative statement.
- ALWAYS name the specific subject inside the fact itself. The fact must be \
unambiguous when read cold, with no surrounding context.
- NEVER refer to the source. Do not write "the passage", "this passage", "the \
text", "the article", "the page", "the infobox", "the section", "as described", \
"as mentioned", "according to the text", "described above", "shown below", or \
anything like them. The reader cannot see any of it.
- NEVER use a bare "this/that/the" + noun to stand in for a subject.
    BAD:  "This droid was manufactured by Industrial Automaton."   (which droid?)
    BAD:  "The passage states he was born on Tatooine."            (refers to the source; "he" is unclear)
    GOOD: "The protocol droid C-3PO was built by Anakin Skywalker on Tatooine."

=== GROUNDING ===
- Every fact must be fully supported by the passage. Use no outside knowledge and \
invent nothing.
- If the passage cannot support {fact_count} genuinely distinct, high-quality \
facts, produce FEWER. Never pad with trivial rephrasings, near-duplicates, or \
hallucinated facts.

=== COVERAGE & VARIETY ===
- Draw on different aspects: identity, origin, manufacturer/creator, affiliations, \
events, relationships, chronology, cause/effect, and other notable details.
- Mix simple lookups with facts that combine multiple details from the passage.

=== STYLE EXAMPLES (an unrelated subject, shown only to illustrate the form) ===
GOOD:
  "Bossk was a Trandoshan bounty hunter."
  "Bossk piloted a modified Corellian freighter called the Hound's Tooth."
BAD (never produce facts like these):
  "He was a bounty hunter."                          (subject not named)
  "According to the passage, he hunted Jedi."        (refers to the passage; "he" is unclear)
  "This bounty hunter piloted a freighter."          (bare "this" stands in for the subject)

PASSAGE{location}:
{chunk.text}
"""


def leaks_source(fact: str) -> bool:
    """True if a fact references its source or uses a bare demonstrative subject."""
    return bool(SOURCE_REF_RE.search(fact) or SUBJECT_SUBSTITUTION_RE.search(fact))


# A backend "caller" is an async function: prompt -> (facts, prompt_tokens, output_tokens).
async def _call_gemini(client: genai.Client, model: str, prompt: str) -> tuple[list[str], int, int]:
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=FactSet,
        temperature=0.7,
    )
    resp = await client.aio.models.generate_content(model=model, contents=prompt, config=config)
    factset = FactSet.model_validate_json(resp.text)
    usage = getattr(resp, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", 0) or 0
    out_tok = getattr(usage, "candidates_token_count", 0) or 0
    return factset.facts, in_tok, out_tok


async def _call_ollama(client, model: str, prompt: str) -> tuple[list[str], int, int]:
    resp = await client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        format=FactSet.model_json_schema(),
        options={"temperature": 0.7},
    )
    factset = FactSet.model_validate_json(resp.message.content)
    in_tok = getattr(resp, "prompt_eval_count", 0) or 0
    out_tok = getattr(resp, "eval_count", 0) or 0
    return factset.facts, in_tok, out_tok


def make_caller(ollama_model: str, gemini_model: str):
    """Return (caller, label) for the chosen backend. Raises RuntimeError on a
    missing dependency or API key. --ollama wins if both are given."""
    if ollama_model:
        if _ollama is None:
            raise RuntimeError("--ollama needs the 'ollama' package: "
                               "uv add ollama  (or run with: uv run --with ollama ...)")
        client = _ollama.AsyncClient()

        async def caller(prompt: str) -> tuple[list[str], int, int]:
            return await _call_ollama(client, ollama_model, prompt)
        return caller, ollama_model

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment, "
                           "or use --ollama <model> for a local model.")
    client = genai.Client(api_key=api_key)

    async def caller(prompt: str) -> tuple[list[str], int, int]:
        return await _call_gemini(client, gemini_model, prompt)
    return caller, gemini_model


async def generate_for_chunk(
    caller,
    chunk: Chunk,
    fact_count: int,
    semaphore: asyncio.Semaphore,
    max_retries: int = 5,
) -> tuple[list[str], int, int]:
    """Return (facts, prompt_tokens, output_tokens) via the given backend caller."""
    prompt = build_prompt(chunk, fact_count)
    async with semaphore:
        delay = 2.0
        for attempt in range(1, max_retries + 1):
            try:
                return await caller(prompt)
            except RETRYABLE_EXC:
                if attempt == max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        return [], 0, 0


async def process_page(
    caller,
    src: Path,
    out: Path,
    fact_count: int,
    max_words: int,
    overlap_ratio: float,
    append: bool,
    semaphore: asyncio.Semaphore,
) -> tuple[Path, int, int, int, int, str | None]:
    """Generate facts for one page. Returns (path, kept, dropped, in_tok, out_tok, error)."""
    try:
        title, body = read_article(src)
        chunks = build_chunks(title, body, max_words, overlap_ratio)
        results = await asyncio.gather(
            *(generate_for_chunk(caller, c, fact_count, semaphore) for c in chunks)
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        # Append: add to the existing file in place. Otherwise write to a temp file
        # and atomically replace, so a crashed run never leaves a half-written page.
        target = out if append else out.with_suffix(out.suffix + ".tmp")
        total = 0
        dropped = 0
        in_tok_sum = 0
        out_tok_sum = 0
        with target.open("a" if append else "w", encoding="utf-8") as f:
            for chunk, (facts, in_tok, out_tok) in zip(chunks, results):
                in_tok_sum += in_tok
                out_tok_sum += out_tok
                for fact in facts:
                    fact = fact.strip()
                    if not fact or leaks_source(fact):
                        dropped += 1
                        continue
                    record = {
                        "fact": fact,
                        "source_page": title,
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    total += 1
        if not append:
            target.replace(out)
        return src, total, dropped, in_tok_sum, out_tok_sum, None
    except Exception as e:
        return src, 0, 0, 0, 0, f"{type(e).__name__}: {e}"


def collect_pages(
    input_dir: Path,
    output_dir: Path,
    min_words: int,
    overwrite: bool,
    letters: list[str] | None,
    shuffle: bool,
    limit: int,
    skip_legends: bool,
    articles: list[str] | None = None,
    append: bool = False,
) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []

    if articles:
        # Exact article names: spaces <-> underscores, case-insensitive.
        wanted = {a.replace(" ", "_").lower() for a in articles}
        for md in input_dir.rglob("*.md"):
            if md.stem.lower() not in wanted:
                continue
            rel = md.relative_to(input_dir)
            out = output_dir / rel.with_suffix(".jsonl")
            if out.exists() and not overwrite and not append:
                print(f"Skipping {md.stem} (output exists; use --overwrite or --append)")
                continue
            pairs.append((md, out))
        return pairs

    roots = [input_dir / L for L in letters] if letters else [input_dir]
    for root in roots:
        if not root.exists():
            print(f"Warning: directory not found, skipping: {root}")
            continue
        for md in root.rglob("*.md"):
            if skip_legends and md.stem.endswith("_Legends"):
                continue
            rel = md.relative_to(input_dir)
            out = output_dir / rel.with_suffix(".jsonl")
            if out.exists() and not overwrite and not append:
                continue
            try:
                size_words = word_count(md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if size_words < min_words:
                continue
            pairs.append((md, out))
    if shuffle:
        random.shuffle(pairs)
    else:
        pairs.sort()
    if limit > 0:
        pairs = pairs[:limit]
    return pairs


async def main_async(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_log = output_dir / ".failed.log"
    backend = "ollama" if args.ollama else "gemini"
    model = args.ollama or args.model

    letters = [s.strip() for s in args.letters.split(",")] if args.letters else None
    articles = [s.strip() for s in args.article.split(",")] if args.article else None
    pages = collect_pages(
        input_dir=input_dir,
        output_dir=output_dir,
        min_words=args.min_words,
        overwrite=args.overwrite,
        letters=letters,
        shuffle=args.shuffle,
        limit=args.limit,
        skip_legends=args.skip_legends,
        articles=articles,
        append=args.append,
    )
    if not pages:
        print("No pages to process (everything already done or filtered out).")
        return 0

    print(
        f"Processing {len(pages)} pages with backend={backend} model={model} "
        f"facts_per_chunk={args.facts_per_chunk} max_words={args.max_words} "
        f"concurrency={args.concurrency}"
    )
    if args.dry_run:
        for src, out in pages[:20]:
            title, body = read_article(src)
            n_chunks = len(build_chunks(title, body, args.max_words, args.overlap_ratio))
            print(f"  {src} -> {out}  [{n_chunks} chunk(s)]")
        if len(pages) > 20:
            print(f"  ... and {len(pages) - 20} more")
        return 0

    try:
        caller, _ = make_caller(args.ollama, args.model)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        process_page(
            caller, src, out,
            args.facts_per_chunk, args.max_words, args.overlap_ratio,
            args.append, semaphore,
        )
        for src, out in pages
    ]

    pbar = tqdm(total=len(tasks), unit="page")
    total_facts = 0
    total_dropped = 0
    failures = 0
    in_tok_total = 0
    out_tok_total = 0
    # Local models are free; only Gemini incurs token cost.
    price_in = 0.0 if args.ollama else args.price_in / 1_000_000.0
    price_out = 0.0 if args.ollama else args.price_out / 1_000_000.0

    def postfix() -> str:
        cost = in_tok_total * price_in + out_tok_total * price_out
        return (
            f"fail={failures} facts={total_facts} dropped={total_dropped} "
            f"in={in_tok_total/1000:.1f}k out={out_tok_total/1000:.1f}k "
            f"${cost:.4f}"
        )

    for coro in asyncio.as_completed(tasks):
        src, count, dropped, in_tok, out_tok, err = await coro
        pbar.update(1)
        in_tok_total += in_tok
        out_tok_total += out_tok
        if err:
            failures += 1
            with failed_log.open("a", encoding="utf-8") as f:
                f.write(f"{src}\t{err}\n")
        else:
            total_facts += count
            total_dropped += dropped
        pbar.set_postfix_str(postfix())
    pbar.close()
    final_cost = in_tok_total * price_in + out_tok_total * price_out
    print(
        f"Done. pages={len(pages)} facts={total_facts} dropped={total_dropped} "
        f"failures={failures} input_tokens={in_tok_total:,} "
        f"output_tokens={out_tok_total:,} total_cost=${final_cost:.4f}"
    )
    if failures:
        print(f"See {failed_log} for details.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input-dir", default=str(paths.WOOKIEEPEDIA_DIR),
                   help="Markdown corpus to read (default: corpus/wookieepedia).")
    p.add_argument("--output-dir", default=str(paths.FACTS_DIR),
                   help="Where JSONL facts are written (default: corpus/facts_dataset).")
    p.add_argument("--model", default="gemini-3.1-flash-lite",
                   help="Gemini model to use (ignored when --ollama is set).")
    p.add_argument("--ollama", default="",
                   help="Use a local Ollama model instead of Gemini, e.g. --ollama gemma3. "
                        "Needs Ollama running locally and the 'ollama' package installed.")
    p.add_argument("--price-in", type=float, default=0.25,
                   help="USD per 1M input tokens (default 0.25, gemini-3.1-flash-lite).")
    p.add_argument("--price-out", type=float, default=1.50,
                   help="USD per 1M output tokens (default 1.50, gemini-3.1-flash-lite).")
    p.add_argument("--facts-per-chunk", type=int, default=50,
                   help="Facts to request per chunk (default 50).")
    p.add_argument("--max-words", type=int, default=6000,
                   help="Max words per chunk before splitting (default 6000).")
    p.add_argument("--overlap-ratio", type=float, default=0.2,
                   help="Fraction of each chunk that repeats the tail of the previous chunk "
                        "(default 0.2; 0 disables overlap).")
    p.add_argument("--min-words", type=int, default=80,
                   help="Skip pages with fewer than this many words.")
    p.add_argument("--concurrency", type=int, default=1,
                   help="Max in-flight Gemini requests.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only process the first N pages (0 = all).")
    p.add_argument("--letters", default="",
                   help="Comma-separated top-level dirs to include (e.g. A,B,L).")
    p.add_argument("--article", default="",
                   help="Comma-separated exact article names (e.g. 'Boba Fett,Darth Vader'). "
                        "Overrides --letters and --min-words; searches the whole input dir.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true",
                      help="Re-generate from scratch even if output JSONL already exists.")
    mode.add_argument("--append", action="store_true",
                      help="Append new facts to an existing output JSONL instead of skipping it "
                           "(accumulate facts across multiple passes).")
    p.add_argument("--shuffle", action="store_true",
                   help="Randomize page order (useful for sampling runs).")
    p.add_argument("--skip-legends", action="store_true",
                   help="Skip non-canon Legends pages (filenames ending in _Legends.md).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be done (with chunk counts), do not call the API.")
    return p.parse_args()


def main() -> None:
    sys.exit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
