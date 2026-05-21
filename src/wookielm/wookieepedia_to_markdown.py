#!/usr/bin/env python3
"""
Convert a Wookieepedia (Fandom MediaWiki) XML dump into clean Markdown files
suitable for LLM training. Each page is written with YAML frontmatter, an
optional ``## Infobox`` section extracted from the page's infobox template,
and the cleaned article body.

Usage:
    1. Download the dump from Fandom:
       https://starwars.fandom.com/wiki/Special:Statistics  (look for "Database download")
    2. Extract:  7z x starwars_pages_current.xml.7z
    3. Install deps: uv sync
    4. Install pandoc: brew install pandoc  (or apt install pandoc)
    5. Run:  uv run wookiee-wookieepedia starwars_pages_current.xml

Resume: rerun the same command. Already-converted files are skipped unless --force.
"""

import argparse
import concurrent.futures as cf
import html
import os
import re
import subprocess
import sys
from pathlib import Path

import mwxml
import mwparserfromhell
from tqdm import tqdm

from wookielm import paths

# ---- Filtering ---------------------------------------------------------------

# MediaWiki namespace 0 = main article space.
MAIN_NAMESPACE = 0

# Templates that are pure data containers — strip entirely rather than inline.
# The actual infobox content is extracted separately via find_infobox/render_infobox
# and rendered as a ``## Infobox`` section.
INFOBOX_TEMPLATE_PREFIXES = (
    "infobox", "char", "character", "starship", "planet", "species",
    "weapon", "vehicle", "organization", "battle", "event", "location",
    "book", "comic", "film", "tv", "episode", "game", "audio",
    "quote", "scroll box", "eras", "top", "bottom", "title", "youmay",
    "otheruses", "redirect", "see also",
)

# Templates that are noise wrappers — drop but keep their positional text args.
DROP_KEEP_ARGS = ("c", "cquote", "qt", "color")

# Wikilink prefixes that produce image/figure noise downstream.
FILE_LINK_PREFIXES = ("file:", "image:", "media:")

# Section headings to remove wholesale (lower-cased, exact match). These are
# out-of-universe metadata that adds noise to an in-universe LLM corpus.
# "Behind the scenes" is intentionally NOT here — it usually contains real prose.
BOILERPLATE_SECTIONS = {
    "appearances",
    "non-canon appearances",
    "sources",
    "non-canon sources",
    "appearances and sources",
    "notes and references",
    "references",
    "external links",
    "see also",
    "gallery",
    "alternate choices",
    "trivia",
}

# Pages whose cleaned body is below this many chars get dropped (mostly stubs
# or pages that were entirely boilerplate).
MIN_BODY_CHARS = 200


# ---- Infobox extraction -----------------------------------------------------

# Templates Wookieepedia routinely puts at the *top* of a page that are NOT
# data infoboxes — page-level notices, disambiguation hatnotes, era badges,
# tone/cleanup tags, etc. We skip them when scanning for the real infobox.
NON_INFOBOX_TEMPLATES = {
    "top", "bottom", "otheruses", "redirect", "youmay", "rhere", "doom",
    "multipleissues", "update", "tone", "conflicting", "cleanup", "expand",
    "stub", "merge", "image", "title", "see also", "eras", "scroll box",
    "quote", "dialogue", "cquote",
}

# Minimum named-parameter count for a template to be considered an infobox.
# Wookieepedia infoboxes routinely have 20+ named params; navigation/hatnote
# templates have 0-3. A threshold of 8 cleanly separates them.
MIN_INFOBOX_PARAMS = 8

# Field keys we never want to surface — image pointers, styling, selectors.
INFOBOX_JUNK_KEYS = {
    "image", "image1", "image2", "image3", "image4", "image5", "image6",
    "imagewidth", "imagebg", "imagebackground",
    "option1", "option2", "option3", "option4", "option5", "option6",
    "caption", "caption1", "caption2", "caption3",
    "hidep", "hideb", "hidec", "hided", "hidee", "hidef", "hideg",
    "type", "subtype", "bordercolor", "background", "headercolor",
    "color", "textcolor", "headerstyle",
}

# Pretty labels for common infobox keys. Anything not here falls back to a
# title-cased version of the key itself.
INFOBOX_KEY_LABELS = {
    "name": "Name", "homeworld": "Homeworld", "birth": "Born", "died": "Died",
    "death": "Died", "species": "Species", "gender": "Gender",
    "pronouns": "Pronouns", "height": "Height", "mass": "Mass",
    "weight": "Weight", "hair": "Hair", "haircolor": "Hair color",
    "eyes": "Eyes", "eyecolor": "Eye color", "skin": "Skin",
    "skincolor": "Skin color", "cyber": "Cybernetics",
    "cybernetics": "Cybernetics", "feathers": "Feathers", "scales": "Scales",
    "families": "Family", "family": "Family", "parents": "Parents",
    "partner": "Partner", "partners": "Partner", "spouse": "Spouse",
    "siblings": "Siblings", "children": "Children",
    "affiliation": "Affiliations", "affiliations": "Affiliations",
    "masters": "Masters", "master": "Master", "apprentices": "Apprentices",
    "apprentice": "Apprentice", "rank": "Rank", "position": "Position",
    "title": "Title", "weapon": "Weapon", "weapons": "Weapons", "era": "Era",
    "eras": "Era", "founder": "Founder", "founded": "Founded",
    "dissolved": "Dissolved", "leader": "Leader", "leaders": "Leaders",
    "headquarters": "Headquarters", "capital": "Capital",
    "language": "Language", "languages": "Language", "religion": "Religion",
    "designation": "Designation", "manufacturer": "Manufacturer",
    "designer": "Designer", "model": "Model", "class": "Class",
    "length": "Length", "width": "Width", "diameter": "Diameter",
    "crew": "Crew", "passengers": "Passengers", "armament": "Armament",
    "shielding": "Shielding", "hull": "Hull", "engines": "Engines",
    "hyperdrive": "Hyperdrive", "speed": "Speed", "maxspeed": "Max speed",
    "max speed": "Max speed", "role": "Role", "roles": "Roles",
    "battles": "Battles", "conflicts": "Conflicts", "missions": "Missions",
    "date": "Date", "location": "Location", "result": "Result",
    "casualties": "Casualties", "commanders": "Commanders",
    "forces": "Forces", "author": "Author", "authors": "Authors",
    "publisher": "Publisher", "released": "Released", "pages": "Pages",
    "isbn": "ISBN", "preceded by": "Preceded by",
    "followed by": "Followed by", "system": "System", "sector": "Sector",
    "region": "Region", "grid": "Grid coordinates",
    "rotation": "Rotation period", "orbital": "Orbital period",
    "atmosphere": "Atmosphere", "climate": "Climate", "gravity": "Gravity",
    "terrain": "Terrain", "water": "Surface water", "fauna": "Native fauna",
    "flora": "Native flora", "natives": "Native species",
    "populace": "Immigrated species", "population": "Population",
    "demonym": "Demonym", "government": "Government", "creator": "Creator",
    "produced": "Produced", "destroyed": "Destroyed", "raceuser": "Used by",
    "useruser": "Used by", "users": "Users", "owner": "Owner",
    "owners": "Owners", "operator": "Operator", "operators": "Operators",
    "members": "Members", "membership": "Members",
    "headofstate": "Head of state",
    "headofgovernment": "Head of government", "executive": "Executive",
    "judicial": "Judicial", "legislative": "Legislative",
}

# Citation template prefixes for infobox value cleanup — these emit source
# pointers, not human content.
CITATION_PREFIXES = (
    "cite", "ref", "sourcebook", "storycite", "encyclopediacite", "databank",
    "swe", "film", "tcw", "tcwa", "idwadventures", "vaderimmortal",
    "scroll", "comicstrip",
)

_BULLET_RE = re.compile(r"^(\*+)\s*(.*)$")
_BR_RE = re.compile(r'<br\s*/?>', flags=re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'[ \t]+')


def _clean_infobox_value(value_node) -> str:
    """Turn a template param value (Wikicode) into plain readable text."""
    try:
        wc = mwparserfromhell.parse(str(value_node))
    except Exception:
        return str(value_node).strip()

    for tag in list(wc.filter_tags(matches=lambda t: str(t.tag).lower() == "ref")):
        try:
            wc.remove(tag)
        except ValueError:
            pass

    for link in list(wc.filter_wikilinks()):
        try:
            target = str(link.title).strip().lower()
        except Exception:
            continue
        if any(target.startswith(p) for p in FILE_LINK_PREFIXES):
            try:
                wc.remove(link)
            except ValueError:
                pass

    for tmpl in list(wc.filter_templates(recursive=True)):
        try:
            raw_name = str(tmpl.name).strip()
        except Exception:
            continue
        name_lower = raw_name.lower()

        # Apostrophe templates: {{'s}} → 's, {{'}} → '
        if raw_name.startswith("'"):
            try:
                wc.replace(tmpl, raw_name)
            except Exception:
                pass
            continue

        # {{C|note}} → (note) — Wookieepedia uses this for parenthetical notes.
        if name_lower == "c":
            try:
                args = [str(p.value).strip() for p in tmpl.params if not p.showkey]
                text = " ".join(a for a in args if a)
                wc.replace(tmpl, f"({text})" if text else "")
            except Exception:
                pass
            continue

        if any(name_lower.startswith(p) for p in CITATION_PREFIXES):
            try:
                wc.remove(tmpl)
            except ValueError:
                pass
            continue

        try:
            wc.remove(tmpl)
        except ValueError:
            pass

    for link in list(wc.filter_wikilinks()):
        try:
            display = str(link.text) if link.text else str(link.title)
        except Exception:
            display = ""
        try:
            wc.replace(link, display)
        except Exception:
            pass

    for comment in list(wc.filter_comments()):
        try:
            wc.remove(comment)
        except ValueError:
            pass

    s = str(wc)
    s = _BR_RE.sub("\n", s)
    s = html.unescape(s)
    s = _HTML_TAG_RE.sub("", s)

    out_lines = []
    for line in s.split("\n"):
        out_lines.append(_WS_RE.sub(" ", line).strip())
    return "\n".join(out_lines).strip()


def _format_infobox_value(text: str) -> str | None:
    """Format a cleaned value as either an inline string or a nested bullet list."""
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    if any(_BULLET_RE.match(l) for l in lines):
        out: list[str] = []
        for l in lines:
            m = _BULLET_RE.match(l)
            if m:
                depth = len(m.group(1))
                content = m.group(2).strip()
                if not content:
                    continue
                indent = "  " * (depth - 1)
                out.append(f"{indent}- {content}")
            else:
                if out:
                    out[-1] = out[-1] + " " + l.strip()
                else:
                    out.append(f"- {l.strip()}")
        return "\n".join(out) if out else None

    if len(lines) == 1:
        return lines[0]

    # Multi-line but no wiki bullets (typically <br /> separators). Join short
    # entries with " / "; render long ones as a bullet list.
    if all(len(l) < 60 for l in lines):
        return " / ".join(lines)
    return "\n".join(f"- {l}" for l in lines)


def _pretty_infobox_key(key: str) -> str:
    k = key.strip().lower()
    if k in INFOBOX_KEY_LABELS:
        return INFOBOX_KEY_LABELS[k]
    return k.replace("_", " ").replace("-", " ").strip().capitalize()


def find_infobox(wikicode):
    """Return the first plausible infobox template in this wikicode, or None.

    Heuristic: the first top-level template whose name isn't a known
    navigation/hatnote and that has at least MIN_INFOBOX_PARAMS named
    parameters. Catches the diversity of Wookieepedia infobox templates
    (Character, CelestialBody, SpaceStation, IndividualShip, Government,
    Religion, Weapon, Battle, ...) without a fixed allowlist.
    """
    for tmpl in wikicode.filter_templates(recursive=False):
        try:
            name = str(tmpl.name).strip().lower()
        except Exception:
            continue
        if name in NON_INFOBOX_TEMPLATES:
            continue
        named = [p for p in tmpl.params if p.showkey]
        if len(named) >= MIN_INFOBOX_PARAMS:
            return tmpl
    return None


def render_infobox(tmpl) -> str | None:
    """Render an mwparserfromhell template as a Markdown ``## Infobox`` block."""
    body: list[str] = []
    seen: set[str] = set()
    for param in tmpl.params:
        if not param.showkey:
            continue
        key = str(param.name).strip().lower()
        if not key or key in INFOBOX_JUNK_KEYS or key in seen:
            continue
        seen.add(key)
        cleaned = _clean_infobox_value(param.value)
        if not cleaned:
            continue
        # Skip template boolean flags ("is_mobile=1", "hidden=yes", etc.).
        if cleaned.lower() in {"0", "1", "yes", "no", "true", "false"}:
            continue
        value_md = _format_infobox_value(cleaned)
        if not value_md:
            continue
        label = _pretty_infobox_key(key)
        if "\n" in value_md:
            body.append(f"- **{label}:**")
            for ln in value_md.split("\n"):
                body.append(f"  {ln}")
        else:
            body.append(f"- **{label}:** {value_md}")
    if not body:
        return None
    return "## Infobox\n\n" + "\n".join(body) + "\n"


# ---- Wikitext cleanup -------------------------------------------------------

def clean_wikicode(wikicode) -> str:
    """Strip/transform templates and file links before pandoc sees them."""
    # File:/Image:/Media: wikilinks → pandoc would emit <img>/<figure> noise.
    for link in list(wikicode.filter_wikilinks()):
        try:
            target = str(link.title).strip().lower()
        except Exception:
            continue
        if any(target.startswith(p) for p in FILE_LINK_PREFIXES):
            try:
                wikicode.remove(link)
            except ValueError:
                pass

    for template in list(wikicode.filter_templates(recursive=True)):
        try:
            raw_name = str(template.name).strip()
        except Exception:
            continue
        name = raw_name.lower()

        # Apostrophe templates: {{'s}} → 's, {{'}} → ', etc. Common on Wookieepedia
        # for possessives where a wikilink ends with a noun.
        if raw_name.startswith("'"):
            try:
                wikicode.replace(template, raw_name)
            except Exception:
                pass
            continue

        if any(name.startswith(prefix) for prefix in INFOBOX_TEMPLATE_PREFIXES):
            try:
                wikicode.remove(template)
            except ValueError:
                pass
            continue

        if name in DROP_KEEP_ARGS:
            try:
                args = [str(p.value).strip() for p in template.params if not p.showkey]
                wikicode.replace(template, " ".join(a for a in args if a))
            except Exception:
                pass
            continue

        if name.startswith(("cite", "ref")):
            try:
                wikicode.remove(template)
            except ValueError:
                pass

    for comment in list(wikicode.filter_comments()):
        try:
            wikicode.remove(comment)
        except ValueError:
            pass

    # Tags whose content is unparseable wikitext or pure noise — drop entirely.
    drop_tags = {"ref", "gallery", "imagemap", "mapframe", "timeline"}
    for tag in list(wikicode.filter_tags(matches=lambda t: str(t.tag).lower() in drop_tags)):
        try:
            wikicode.remove(tag)
        except ValueError:
            pass

    return str(wikicode)


_CATEGORY_TAG_RE = re.compile(
    r"\[\[Category:([^\]|]+)(?:\|[^\]]*)?\]\]",
    flags=re.IGNORECASE,
)


def extract_categories(wikitext: str) -> list[str]:
    """Pull [[Category:Foo]] tags out as a deduplicated list."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _CATEGORY_TAG_RE.findall(wikitext):
        c = raw.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---- Pandoc -----------------------------------------------------------------

def wikitext_to_markdown(wikitext: str) -> str:
    """Pipe cleaned wikitext through pandoc."""
    result = subprocess.run(
        ["pandoc", "-f", "mediawiki", "-t", "gfm", "--wrap=none"],
        input=wikitext.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pandoc failed: {result.stderr.decode('utf-8', errors='replace')[:200]}"
        )
    return result.stdout.decode("utf-8", errors="replace")


# ---- Post-processing the markdown ------------------------------------------

# Pandoc's mediawiki→gfm reader falls back to raw HTML for many features:
# wikilinks become <a class="wikilink">, files become <img>/<figure>, etc.
# We strip those here so the corpus is actual Markdown prose, not HTML.

_WIKILINK_A_RE = re.compile(
    r'<a\b[^>]*\bclass="wikilink"[^>]*>(.*?)</a>',
    flags=re.DOTALL,
)
_FIGURE_RE = re.compile(r'<figure\b[^>]*>.*?</figure>', flags=re.DOTALL | re.IGNORECASE)
_IMG_RE = re.compile(r'<img\b[^>]*/?>', flags=re.IGNORECASE)
_FIGCAPTION_RE = re.compile(
    r'<figcaption\b[^>]*>.*?</figcaption>', flags=re.DOTALL | re.IGNORECASE
)
_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')
_CATEGORY_A_RE = re.compile(
    r'<a\b[^>]*href="Category:[^"]*"[^>]*>[^<]*</a>',
    flags=re.IGNORECASE,
)
_EM_RE = re.compile(r'<em\b[^>]*>(.*?)</em>', flags=re.DOTALL | re.IGNORECASE)
_STRONG_RE = re.compile(r'<strong\b[^>]*>(.*?)</strong>', flags=re.DOTALL | re.IGNORECASE)
_SMALL_RE = re.compile(r'<small\b[^>]*>(.*?)</small>', flags=re.DOTALL | re.IGNORECASE)
_SUP_RE = re.compile(r'<sup\b[^>]*>(.*?)</sup>', flags=re.DOTALL | re.IGNORECASE)
_SUB_RE = re.compile(r'<sub\b[^>]*>(.*?)</sub>', flags=re.DOTALL | re.IGNORECASE)
_DOLLAR_ESCAPE_RE = re.compile(r'\\\$')
_EMPTY_BULLET_RE = re.compile(r'(?m)^[-*]\s*$\n?')
_HEADING_RE = re.compile(r'^(#+)\s+(.+?)\s*$')
_BLANKS_RE = re.compile(r'\n{3,}')


def clean_html_noise(md: str) -> str:
    """Strip leftover HTML pandoc emits when MW features don't map to GFM."""
    md = _FIGURE_RE.sub('', md)
    md = _FIGCAPTION_RE.sub('', md)
    md = _IMG_RE.sub('', md)
    md = _MD_IMAGE_RE.sub('', md)
    # Category anchors first — they also carry class="wikilink", so the generic
    # wikilink replacement below would otherwise keep their visible text behind.
    md = _CATEGORY_A_RE.sub('', md)
    md = _WIKILINK_A_RE.sub(lambda m: m.group(1), md)
    # Inline HTML pandoc falls back to when GFM can't represent the markup cleanly
    # (commonly inside tables or where stripped wikilinks left orphaned italics).
    md = _EM_RE.sub(lambda m: f'*{m.group(1)}*', md)
    md = _STRONG_RE.sub(lambda m: f'**{m.group(1)}**', md)
    md = _SMALL_RE.sub(lambda m: m.group(1), md)
    md = _SUP_RE.sub(lambda m: m.group(1), md)
    md = _SUB_RE.sub(lambda m: m.group(1), md)
    md = _BR_RE.sub('\n', md)
    md = _DOLLAR_ESCAPE_RE.sub('$', md)
    md = html.unescape(md)
    md = _EMPTY_BULLET_RE.sub('', md)
    md = _BLANKS_RE.sub('\n\n', md)
    return md


def strip_boilerplate_sections(md: str) -> str:
    """Remove out-of-universe sections (Appearances, Sources, External links, ...)."""
    out: list[str] = []
    skip_level: int | None = None
    for line in md.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip().lower()
            # Exit the skip region when a heading at or above the skipped level appears.
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if heading in BOILERPLATE_SECTIONS:
                skip_level = level
                continue  # drop the heading itself
        if skip_level is not None:
            continue
        out.append(line)
    return _BLANKS_RE.sub('\n\n', "\n".join(out)).strip() + "\n"


# ---- Paths & I/O -----------------------------------------------------------

_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(title: str) -> str:
    name = _UNSAFE_CHARS_RE.sub('_', title)
    return name.strip().replace(' ', '_')[:200]


def output_path(out_root: Path, title: str) -> Path:
    safe = sanitize_filename(title)
    shard = safe[0].upper() if safe and safe[0].isalnum() else "_"
    return out_root / shard / f"{safe}.md"


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_document(title: str, categories: list[str],
                   infobox_md: str | None, body: str) -> str:
    cats_yaml = "\n".join(f'  - "{yaml_escape(c)}"' for c in categories)
    return (
        "---\n"
        f'title: "{yaml_escape(title)}"\n'
        f'source: "Wookieepedia"\n'
        + (f"categories:\n{cats_yaml}\n" if categories else "")
        + "---\n\n"
        + (infobox_md + "\n" if infobox_md else "")
        + body
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX, and on Windows since Python 3.3


# ---- Worker ----------------------------------------------------------------

def process_page(title: str, wikitext: str):
    """Worker: extract infobox + clean → pandoc → post-process.

    Returns (title, categories, infobox_md, body, error). On success error is
    None; on a recoverable empty page error == "empty_after_clean"; otherwise
    error holds a short exception string.
    """
    try:
        categories = extract_categories(wikitext)

        infobox_md: str | None = None
        try:
            wikicode = mwparserfromhell.parse(wikitext)
            tmpl = find_infobox(wikicode)
            if tmpl is not None:
                infobox_md = render_infobox(tmpl)
                # Drop it from the body so it isn't duplicated alongside the
                # rendered Markdown infobox section.
                try:
                    wikicode.remove(tmpl)
                except ValueError:
                    pass
            cleaned = clean_wikicode(wikicode)
        except Exception:
            # Fall back to raw wikitext if parsing/cleanup fails — pandoc may
            # still produce usable output, and we don't want one bad page to
            # block the pipeline.
            cleaned = wikitext

        markdown = wikitext_to_markdown(cleaned)
        markdown = clean_html_noise(markdown)
        markdown = strip_boilerplate_sections(markdown)
        if len(markdown.strip()) < MIN_BODY_CHARS:
            return (title, categories, None, None, "empty_after_clean")
        return (title, categories, infobox_md, markdown, None)
    except Exception as e:
        return (title, [], None, None, f"{type(e).__name__}: {e}")


# ---- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dump_xml", help="Path to extracted Wookieepedia XML dump")
    parser.add_argument("output_dir", nargs="?", default=str(paths.WOOKIEEPEDIA_DIR),
                        help="Output directory for markdown files (default: corpus/wookieepedia)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N successfully converted pages (testing)")
    parser.add_argument("--log", default="conversion.log", help="Path to error log")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                        help="Worker processes (default: cpu_count)")
    parser.add_argument("--force", action="store_true",
                        help="Reconvert pages even if an output file already exists")
    args = parser.parse_args()

    dump_path = Path(args.dump_xml)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    if not dump_path.exists():
        sys.exit(f"Dump not found: {dump_path}")

    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("pandoc not found on PATH. Install it first.")

    converted = skipped = failed = resumed = empty = 0
    max_inflight = args.workers * 4
    progress = tqdm(unit="pages")

    def handle_result(fut: cf.Future) -> None:
        nonlocal converted, failed, empty
        title, categories, infobox_md, body, err = fut.result()
        if err == "empty_after_clean":
            empty += 1
        elif err is not None:
            failed += 1
            errors_log.write(f"{title}\t{err}\n")
            errors_log.flush()
        else:
            try:
                atomic_write(output_path(out_root, title),
                             build_document(title, categories, infobox_md, body))
                converted += 1
            except Exception as e:
                failed += 1
                errors_log.write(f"{title}\twrite_error\t{type(e).__name__}: {e}\n")
                errors_log.flush()
        progress.set_postfix(ok=converted, fail=failed, skip=skipped,
                             resume=resumed, empty=empty)

    with open(args.log, "a", encoding="utf-8") as errors_log, \
         open(dump_path, "rb") as dump_fh, \
         cf.ProcessPoolExecutor(max_workers=args.workers) as pool:

        dump = mwxml.Dump.from_file(dump_fh)
        pending: set[cf.Future] = set()

        for page in dump:
            if args.limit and converted >= args.limit:
                break

            progress.update(1)

            if page.namespace != MAIN_NAMESPACE or page.redirect:
                skipped += 1
                continue

            target = output_path(out_root, page.title)
            if (not args.force) and target.exists() and target.stat().st_size > 0:
                resumed += 1
                continue

            revision = None
            for rev in page:
                revision = rev  # last wins
            if revision is None or not revision.text:
                skipped += 1
                continue

            # Throttle: drain completed futures before queueing more.
            while len(pending) >= max_inflight:
                done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    handle_result(fut)

            pending.add(pool.submit(process_page, page.title, revision.text))

        # Drain remaining futures.
        for fut in cf.as_completed(pending):
            handle_result(fut)

    progress.close()
    print(f"\nDone. converted={converted}  skipped={skipped}  failed={failed}  "
          f"resumed={resumed}  empty={empty}")
    print(f"Errors logged to: {args.log}")


if __name__ == "__main__":
    main()
