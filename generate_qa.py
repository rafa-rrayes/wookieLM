"""Generate Q&A pairs from Wookieepedia markdown pages using Gemini.

Walks an input directory of .md files, splits very large pages into chunks
(preserving the title in each chunk), and asks Gemini to produce N Q&A pairs
per chunk via structured JSON output. Results are written as JSONL, one file
per source page, mirroring the input directory layout. Resumable: pages that
already have an output file are skipped unless --overwrite is set.

Usage:
    export GEMINI_API_KEY=...
    uv run --with google-genai --with pydantic --with tqdm \
        generate_qa.py --limit 5 --letters A
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


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
H2_RE = re.compile(r"^## ", re.MULTILINE)


class QAPair(BaseModel):
    question: str = Field(description="A specific, factual question answerable from the passage.")
    answer: str = Field(description="A concise, accurate answer grounded in the passage. 1-4 sentences.")


class QASet(BaseModel):
    pairs: list[QAPair]


@dataclass
class Chunk:
    title: str
    index: int
    total: int
    text: str

    def prompt_body(self) -> str:
        header = f"Page title: {self.title}"
        if self.total > 1:
            header += f"\n[Chunk {self.index + 1} of {self.total}]"
        return f"{header}\n\n{self.text}"


def parse_markdown(path: Path) -> tuple[str, str]:
    """Return (title, body_without_frontmatter)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("_", " ")
    m = FRONTMATTER_RE.match(raw)
    if m:
        fm = m.group(1)
        tm = TITLE_RE.search(fm)
        if tm:
            title = tm.group(1).strip()
        body = raw[m.end():]
    else:
        body = raw
    return title, body.strip()


def word_count(text: str) -> int:
    return len(text.split())


def split_into_chunks(title: str, body: str, max_words: int) -> list[Chunk]:
    """Split body into chunks of up to ~max_words words.

    Strategy: split on ## headings; greedily accumulate sections until a chunk
    would exceed max_words. Sections larger than max_words are further split on
    blank lines, falling back to a hard word slice if a single paragraph is
    still too large.
    """
    if word_count(body) <= max_words:
        return [Chunk(title=title, index=0, total=1, text=body)]

    parts: list[str] = []
    last = 0
    for m in H2_RE.finditer(body):
        if m.start() == 0:
            continue
        parts.append(body[last:m.start()].strip())
        last = m.start()
    parts.append(body[last:].strip())
    parts = [p for p in parts if p]

    expanded: list[str] = []
    for section in parts:
        if word_count(section) <= max_words:
            expanded.append(section)
            continue
        paragraphs = re.split(r"\n\s*\n", section)
        buf: list[str] = []
        buf_words = 0
        for para in paragraphs:
            pw = word_count(para)
            if pw > max_words:
                if buf:
                    expanded.append("\n\n".join(buf))
                    buf, buf_words = [], 0
                tokens = para.split()
                for i in range(0, len(tokens), max_words):
                    expanded.append(" ".join(tokens[i:i + max_words]))
            elif buf_words + pw > max_words:
                expanded.append("\n\n".join(buf))
                buf, buf_words = [para], pw
            else:
                buf.append(para)
                buf_words += pw
        if buf:
            expanded.append("\n\n".join(buf))

    chunks_text: list[str] = []
    buf: list[str] = []
    buf_words = 0
    for piece in expanded:
        pw = word_count(piece)
        if buf and buf_words + pw > max_words:
            chunks_text.append("\n\n".join(buf))
            buf, buf_words = [piece], pw
        else:
            buf.append(piece)
            buf_words += pw
    if buf:
        chunks_text.append("\n\n".join(buf))

    return [
        Chunk(title=title, index=i, total=len(chunks_text), text=t)
        for i, t in enumerate(chunks_text)
    ]


def build_prompt(chunk: Chunk, qa_count: int) -> str:
    return (
        f"You are building a high-quality Star Wars training dataset from "
        f"Wookieepedia. From the passage below, write exactly {qa_count} "
        f"diverse question/answer pairs.\n\n"
        f"Rules:\n"
        f"- Each Q&A must be fully grounded in the passage. No outside knowledge.\n"
        f"- Cover different facts, characters, events, dates, relationships, "
        f"and concepts mentioned in the passage. Avoid duplicates and trivial "
        f"rephrasings.\n"
        f"- Questions should be specific and standalone (mention the subject by "
        f"name; do not rely on context like 'in this passage').\n"
        f"- Answers should be concise (1-4 sentences) and accurate. Quote names, "
        f"dates, and titles exactly as they appear.\n"
        f"- Mix difficulty: simple lookups, multi-hop reasoning, comparisons, "
        f"cause/effect, chronology.\n"
        f"- Do NOT invent facts. If the passage does not support {qa_count} "
        f"distinct pairs, produce as many high-quality grounded pairs as you "
        f"can rather than padding with hallucinations.\n\n"
        f"PASSAGE:\n{chunk.prompt_body()}"
    )


async def generate_for_chunk(
    client: genai.Client,
    model: str,
    chunk: Chunk,
    qa_count: int,
    semaphore: asyncio.Semaphore,
    max_retries: int = 5,
) -> tuple[list[QAPair], int, int]:
    """Return (pairs, prompt_tokens, output_tokens)."""
    prompt = build_prompt(chunk, qa_count)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=QASet,
        temperature=0.7,
    )
    async with semaphore:
        delay = 2.0
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=prompt, config=config,
                )
                qaset = QASet.model_validate_json(resp.text)
                usage = getattr(resp, "usage_metadata", None)
                in_tok = getattr(usage, "prompt_token_count", 0) or 0
                out_tok = getattr(usage, "candidates_token_count", 0) or 0
                return qaset.pairs, in_tok, out_tok
            except (genai_errors.APIError, json.JSONDecodeError, ValueError) as e:
                if attempt == max_retries:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        return [], 0, 0


async def process_page(
    client: genai.Client,
    model: str,
    src: Path,
    out: Path,
    qa_count: int,
    chunk_words: int,
    semaphore: asyncio.Semaphore,
) -> tuple[Path, int, int, int, str | None]:
    """Generate Q&A for one page. Returns (path, pair_count, in_tok, out_tok, error)."""
    try:
        title, body = parse_markdown(src)
        chunks = split_into_chunks(title, body, chunk_words)
        results = await asyncio.gather(
            *(generate_for_chunk(client, model, c, qa_count, semaphore) for c in chunks)
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        total = 0
        in_tok_sum = 0
        out_tok_sum = 0
        with tmp.open("w", encoding="utf-8") as f:
            for chunk, (pairs, in_tok, out_tok) in zip(chunks, results):
                in_tok_sum += in_tok
                out_tok_sum += out_tok
                for p in pairs:
                    record = {
                        "question": p.question,
                        "answer": p.answer,
                        "source_page": title,
                        "source_path": str(src),
                        "chunk_index": chunk.index,
                        "chunk_count": chunk.total,
                        "model": model,
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    total += 1
        tmp.replace(out)
        return src, total, in_tok_sum, out_tok_sum, None
    except Exception as e:
        return src, 0, 0, 0, f"{type(e).__name__}: {e}"


def collect_pages(
    input_dir: Path,
    output_dir: Path,
    min_words: int,
    overwrite: bool,
    letters: list[str] | None,
    shuffle: bool,
    limit: int,
    skip_legends: bool,
) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    roots = [input_dir / L for L in letters] if letters else [input_dir]
    for root in roots:
        if not root.exists():
            continue
        for md in root.rglob("*.md"):
            if skip_legends and md.stem.endswith("_Legends"):
                continue
            rel = md.relative_to(input_dir)
            out = output_dir / rel.with_suffix(".jsonl")
            if out.exists() and not overwrite:
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
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key and not args.dry_run:
        print("ERROR: set GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment.", file=sys.stderr)
        return 2

    client = genai.Client(api_key=api_key) if api_key else None
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_log = output_dir / ".failed.log"

    letters = [s.strip() for s in args.letters.split(",")] if args.letters else None
    pages = collect_pages(
        input_dir=input_dir,
        output_dir=output_dir,
        min_words=args.min_words,
        overwrite=args.overwrite,
        letters=letters,
        shuffle=args.shuffle,
        limit=args.limit,
        skip_legends=args.skip_legends,
    )
    if not pages:
        print("No pages to process (everything already done or filtered out).")
        return 0

    print(
        f"Processing {len(pages)} pages with model={args.model} "
        f"qa_per_chunk={args.qa_per_chunk} chunk_words={args.chunk_words} "
        f"concurrency={args.concurrency}"
    )
    if args.dry_run:
        for src, out in pages[:20]:
            print(f"  {src} -> {out}")
        if len(pages) > 20:
            print(f"  ... and {len(pages) - 20} more")
        return 0

    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        process_page(
            client, args.model, src, out,
            args.qa_per_chunk, args.chunk_words, semaphore,
        )
        for src, out in pages
    ]

    pbar = tqdm(total=len(tasks), unit="page")
    total_pairs = 0
    failures = 0
    in_tok_total = 0
    out_tok_total = 0
    price_in = args.price_in / 1_000_000.0
    price_out = args.price_out / 1_000_000.0

    def postfix() -> str:
        cost = in_tok_total * price_in + out_tok_total * price_out
        return (
            f"fail={failures} pairs={total_pairs} "
            f"in={in_tok_total/1000:.1f}k out={out_tok_total/1000:.1f}k "
            f"${cost:.4f}"
        )

    for coro in asyncio.as_completed(tasks):
        src, count, in_tok, out_tok, err = await coro
        pbar.update(1)
        in_tok_total += in_tok
        out_tok_total += out_tok
        if err:
            failures += 1
            with failed_log.open("a", encoding="utf-8") as f:
                f.write(f"{src}\t{err}\n")
        else:
            total_pairs += count
        pbar.set_postfix_str(postfix())
    pbar.close()
    final_cost = in_tok_total * price_in + out_tok_total * price_out
    print(
        f"Done. pages={len(pages)} pairs={total_pairs} failures={failures} "
        f"input_tokens={in_tok_total:,} output_tokens={out_tok_total:,} "
        f"total_cost=${final_cost:.4f}"
    )
    if failures:
        print(f"See {failed_log} for details.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input-dir", default="wookiepedia")
    p.add_argument("--output-dir", default="qa_dataset")
    p.add_argument("--model", default="gemini-3.1-flash-lite")
    p.add_argument("--price-in", type=float, default=0.25,
                   help="USD per 1M input tokens (default 0.25, gemini-3.1-flash-lite).")
    p.add_argument("--price-out", type=float, default=1.50,
                   help="USD per 1M output tokens (default 1.50, gemini-3.1-flash-lite).")
    p.add_argument("--qa-per-chunk", type=int, default=100,
                   help="Q&A pairs to request per chunk (default 100).")
    p.add_argument("--chunk-words", type=int, default=6000,
                   help="Max words per chunk before splitting (default 6000).")
    p.add_argument("--min-words", type=int, default=80,
                   help="Skip pages with fewer than this many words.")
    p.add_argument("--concurrency", type=int, default=8,
                   help="Max in-flight Gemini requests.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only process the first N pages (0 = all).")
    p.add_argument("--letters", default="",
                   help="Comma-separated top-level dirs to include (e.g. A,B,L).")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-generate even if output JSONL already exists.")
    p.add_argument("--shuffle", action="store_true",
                   help="Randomize page order (useful for sampling runs).")
    p.add_argument("--skip-legends", action="store_true",
                   help="Skip non-canon Legends pages (filenames ending in _Legends.md).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be done, do not call the API.")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
