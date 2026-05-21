"""Canonical on-disk layout for the wookieLM corpus.

Every pipeline stage resolves its default input/output directories from here, so
moving the corpus only ever means editing one file. Override any of these per run
with the relevant CLI flag.

Layout::

    <repo>/
        corpus/          generated + scraped data (gitignored)
            wookieepedia/  wikipedia/  subtitles/  scripts/  books/  facts_dataset/
        continuity/      tracked canon/Legends title lists
"""

from __future__ import annotations

from pathlib import Path

# src/wookielm/paths.py -> parents[2] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

CORPUS_DIR = REPO_ROOT / "corpus"
CONTINUITY_DIR = REPO_ROOT / "continuity"

# Per-source corpora (all under corpus/).
WOOKIEEPEDIA_DIR = CORPUS_DIR / "wookieepedia"
WIKIPEDIA_DIR = CORPUS_DIR / "wikipedia"
SUBTITLES_DIR = CORPUS_DIR / "subtitles"
SCRIPTS_DIR = CORPUS_DIR / "scripts"
BOOKS_DIR = CORPUS_DIR / "books"
FACTS_DIR = CORPUS_DIR / "facts_dataset"
