# wookieLM repo makeover

Goal: turn a flat, jumbled root into a clean, idiomatic Python project. Fix the
broken corpus paths (data moved to `corpus/` but scripts still default to root).

## Decisions (confirmed with user)
- Full `src/wookielm/` package + `wookiee-*` console-script entry points.
- Single shared `paths.py` as the source of truth for on-disk layout.
- Delete `wookiepedia.zip` (254 MB source dump; already extracted into corpus/).

## Target layout
```
README.md  pyproject.toml  uv.lock  .gitignore
docs/data_sources.md
tasks/todo.md  tasks/lessons.md
src/wookielm/{__init__,paths,wikipedia,generate_fact,subtitles,
              wookieepedia_to_markdown,extract_books,fetch_continuity,
              tag_continuity,count}.py
corpus/   (gitignored data: wookieepedia, wikipedia, subtitles, scripts, books, facts_dataset)
continuity/   (tracked title lists)
```

## Tasks
- [x] Remove cruft: `__pycache__/`, `.DS_Store`, `wookiepedia.zip`
- [x] Scaffold: `src/wookielm/`, `docs/`, `tasks/`; add `__init__.py` + `paths.py`
- [x] `git mv` 8 scripts -> `src/wookielm/`; `data_sources.md` -> `docs/`
- [x] Repoint every script's path defaults to `paths.py` (killed the broken root defaults)
- [x] Add `main()` wrappers where missing (count, generate_fact) for entry points
- [x] Rewrite `pyproject.toml`: build-system + `[project.scripts]`
- [x] Rewrite `.gitignore` for the new layout
- [x] Rewrite `README.md` for the new layout + CLI
- [x] `uv sync`; verify all modules import and `wookiee-count` runs

## Review
- All 8 scripts now live in `src/wookielm/` and are exposed as `wookiee-*` console
  scripts (verified registered in `.venv/bin` and via `-h`).
- `paths.py` is the single source of truth; every script's defaults point under
  `corpus/`. Before, defaults pointed at root (`wookieepedia`, `facts_dataset`,
  …) which were stale after the data moved to `corpus/` — those were silently
  broken and are now fixed.
- `count.py` rewritten on `paths.py`; also fixed a latent undercount (it counted
  only `.md` scripts, missing the 2 `.txt` scripts → now 8). Fresh stats wired
  into the README. Facts grew to ~17.5M tokens; total ~122M.
- Verified: `git mv` preserved history (R/RM in status), `corpus/` stays
  gitignored, `continuity/` stays tracked, `uv sync` builds the package, all
  modules import, and `wookiee-tag-continuity --dry-run` resolves both
  `continuity/` and `corpus/wookieepedia/` end-to-end.
- NOT committed — left for user review (no commit was requested).

## Follow-ups (optional, not done)
- `wookiee-wookieepedia --log` still defaults to `conversion.log` in CWD; could
  route under `corpus/`. Left as-is (it's an error log).
