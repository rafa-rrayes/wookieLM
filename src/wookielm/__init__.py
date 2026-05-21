"""wookieLM — a pipeline for building a multi-source Star Wars text corpus.

The package ships one module per pipeline stage, each exposed as a ``wookiee-*``
console script (see ``pyproject.toml``). All on-disk locations live in
:mod:`wookielm.paths` so the corpus layout has a single source of truth.
"""

__version__ = "0.1.0"
