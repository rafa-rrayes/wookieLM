#!/usr/bin/env python3
"""Report file count, content size, and rough token estimate per dataset."""
import glob
import json
import os

from wookielm import paths


def measure(root, exts):
    nbytes = nfiles = 0
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if fn.endswith(exts):
                nbytes += os.path.getsize(os.path.join(dp, fn))
                nfiles += 1
    return nfiles, nbytes


def human(b):
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


# (label, directory, counted extensions)
SOURCES = [
    ("subtitles",    paths.SUBTITLES_DIR,    (".txt",)),
    ("scripts",      paths.SCRIPTS_DIR,      (".md", ".txt")),
    ("wookieepedia", paths.WOOKIEEPEDIA_DIR, (".md",)),
    ("wikipedia",    paths.WIKIPEDIA_DIR,    (".md",)),
    ("facts",        paths.FACTS_DIR,        (".jsonl",)),
    ("books",        paths.BOOKS_DIR,        (".txt",)),
]


def md_path(jsonl):
    """Resolve the source markdown page a facts JSONL was generated from.

    generate_fact mirrors the input layout under facts_dataset/, so undo the
    mirror against each markdown corpus that feeds the fact pipeline.
    """
    rel = os.path.relpath(jsonl, paths.FACTS_DIR)
    for corpus in (paths.WOOKIEEPEDIA_DIR, paths.WIKIPEDIA_DIR):
        cand = os.path.splitext(os.path.join(corpus, rel))[0] + ".md"
        if os.path.exists(cand):
            return cand
    return None


def uniq_facts(jsonl):
    seen = set()
    for line in open(jsonl, encoding="utf-8"):
        try:
            seen.add(json.loads(line)["fact"])
        except Exception:
            seen.add(line)
    return len(seen)


def count_facts():
    rows = []
    for f in glob.glob(os.path.join(paths.FACTS_DIR, "**", "*.jsonl"), recursive=True):
        n = sum(1 for _ in open(f, encoding="utf-8"))
        rows.append((n, f))
    rows.sort(reverse=True)
    top = rows[:10]

    print(f"{'Article':<34}{'Facts':>7}{'Unique':>8}{'Art.words':>11}{'Art.chars':>11}{'Facts/1k-w':>11}")
    print("-" * 82)
    for n, f in top:
        name = os.path.basename(f)[:-6]
        md = md_path(f)
        if md:
            text = open(md, encoding="utf-8").read()
            words = len(text.split())
            chars = len(text)
            ratio = f"{n/(words/1000):.1f}" if words else "n/a"
        else:
            words = chars = 0
            ratio = "MISSING"
        uq = uniq_facts(f)
        print(f"{name:<34}{n:>7}{uq:>8}{words:>11,}{chars:>11,}{ratio:>11}")


def count_files():
    print(f'{"source":<14}{"files":>9}{"content size":>14}{"~tokens":>12}')
    print("-" * 49)
    tb = tf = 0
    for name, root, exts in SOURCES:
        nf, nb = measure(root, exts)
        tb += nb
        tf += nf
        print(f"{name:<14}{nf:>9,}{human(nb):>14}{int(nb / 4):>12,}")
    print("-" * 49)
    print(f'{"TOTAL":<14}{tf:>9,}{human(tb):>14}{int(tb / 4):>12,}')
    print()
    print(f"raw content bytes: {tb:,}")


def main():
    count_files()
    count_facts()


if __name__ == "__main__":
    main()
