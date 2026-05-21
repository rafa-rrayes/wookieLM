#!/usr/bin/env python3
"""Report file count, content size, and rough token estimate per dataset."""
import json, os, glob

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


data = [
    ("subtitles",    measure("subtitles", (".txt",))),
    ("scripts",      measure("scripts", (".md",))),
    ("wookieepedia", measure("wookieepedia", (".md",))),
    ("wikipedia",    measure("wikipedia", (".md",))),
    ("facts",        measure("facts_dataset", (".jsonl",))),
    ('books',         measure("books", (".txt",))),
]




# resolve md path from first line's source_path, fall back to mirrored path
def md_path(jsonl):
    try:
        with open(jsonl, encoding="utf-8") as fh:
            sp = json.loads(fh.readline())["source_path"]
        if os.path.exists(sp):
            return sp
    except Exception:
        pass
    guess = jsonl.replace("facts_dataset/", "wookieepedia/").replace(".jsonl", ".md")
    return guess if os.path.exists(guess) else None

def uniq_facts(jsonl):
    seen = set()
    for line in open(jsonl, encoding="utf-8"):
        try:
            seen.add(json.loads(line)["fact"])
        except Exception:
            seen.add(line)
    return len(seen)


def count_facts(jsonl):
    rows = []
    for f in glob.glob("facts_dataset/**/*.jsonl", recursive=True):
        n = sum(1 for _ in open(f, encoding="utf-8"))
        rows.append((n, f))
    rows.sort(reverse=True)
    top = rows[:10]

    print(f"{'Article':<34}{'Facts':>7}{'Unique':>8}{'Art.words':>11}{'Art.chars':>11}{'Facts/1k-w':>11}")
    print("-"*82)
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
    for name, (nf, nb) in data:
        tb += nb
        tf += nf
        print(f"{name:<14}{nf:>9,}{human(nb):>14}{int(nb / 4):>12,}")
    print("-" * 49)
    print(f'{"TOTAL":<14}{tf:>9,}{human(tb):>14}{int(tb / 4):>12,}')
    print()
    print(f"raw content bytes: {tb:,}")

if __name__ == "__main__":
    count_files()
    # count_facts("facts_dataset/**/*.jsonl")