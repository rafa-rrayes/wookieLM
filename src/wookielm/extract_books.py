"""Extract the text layer from the Star Wars novel PDFs in corpus/books/ into .txt files.

Raw extraction: keeps front/back matter (copyright, TOC, "About the Author"),
matching the two hand-made .txt files already in corpus/books/. Pages are joined with a
single newline (no form-feed page markers). Files that already have a .txt are
skipped so existing extractions are never clobbered.

Output name = PDF name with the "_OceanofPDF.com_" prefix and ".pdf" stripped.

Usage:
    uv run wookiee-extract-books            # extract all PDFs lacking a .txt
    uv run wookiee-extract-books --force    # re-extract even if .txt exists
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz  # PyMuPDF

from wookielm import paths

BOOKS_DIR = paths.BOOKS_DIR
PREFIX = "_OceanofPDF.com_"


def out_name(pdf: Path) -> str:
    stem = pdf.stem
    if stem.startswith(PREFIX):
        stem = stem[len(PREFIX):]
    return stem


def extract(pdf: Path) -> str:
    parts: list[str] = []
    with fitz.open(pdf) as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)


def main() -> None:
    force = "--force" in sys.argv[1:]
    pdfs = sorted(BOOKS_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {BOOKS_DIR}")
        return

    done = skipped = failed = 0
    for pdf in pdfs:
        out = BOOKS_DIR / f"{out_name(pdf)}.txt"
        if out.exists() and not force:
            print(f"skip   {out.name} (exists)")
            skipped += 1
            continue
        try:
            text = extract(pdf)
        except Exception as exc:  # noqa: BLE001 - report and continue the batch
            print(f"FAIL   {pdf.name}: {exc}")
            failed += 1
            continue
        out.write_text(text, encoding="utf-8")
        kb = len(text.encode("utf-8")) // 1024
        print(f"ok     {out.name} ({kb} KB)")
        done += 1

    print(f"\nextracted {done}, skipped {skipped}, failed {failed}")


if __name__ == "__main__":
    main()
