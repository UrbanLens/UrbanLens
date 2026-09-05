"""Sphinx configuration.

Two choices here are load-bearing, and both exist so a docs build works in a
plain checkout rather than only where the application itself runs.

**AutoAPI, not autodoc.** ``sphinx.ext.autodoc`` imports every module it
documents, which for this codebase means ``django.setup()``, a settings module,
and a system GDAL/GEOS install - so the docs would build only inside the app
container or CI, and a broken import would surface as a missing page rather than
an error. ``autoapi`` parses the source instead, so `sphinx-build docs` works
anywhere Python does.

**MyST.** Everything else in ``docs/`` is Markdown. Without ``myst_parser`` the
toctree can reference exactly two files - this one's siblings ``index.rst`` and
nothing else - and the generated site is an API reference with no prose.

Build it with ``bin/build_docs.py``, which is also what CI runs; the ``api/``
tree it produces is generated, not committed.
"""

from __future__ import annotations

import pathlib

project = "UrbanLens"
copyright = "2023, Jess Mann"  # noqa: A001
author = "Jess Mann"

extensions = [
    "autoapi.extension",
    "myst_parser",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

templates_path = ["_templates"]

exclude_patterns = [
    "_build",
    "Thumbs.db",
    ".DS_Store",
    # Historical reports and per-task working notes: hundreds of pages of
    # point-in-time findings, several of which this documentation's own README
    # describes as evidence rather than reference. Including them buries the
    # dozen documents a reader actually wants.
    "reports/*",
    "prompts/*",
    "notes/*",
    "archive/*",
    "audits/*",
    "designs/*",
    "mobile/*",
]

#: Google-style docstrings, which `CLAUDE.md` requires throughout.
napoleon_google_docstring = True
napoleon_numpy_docstring = False

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

autoapi_type = "python"
autoapi_dirs = [str(_REPO_ROOT / "src" / "urbanlens")]
autoapi_root = "api"
autoapi_keep_files = False
#: Migrations are generated, and there are enough of them to dominate the
#: sidebar; `tests/` documents itself by being read, not by being rendered.
autoapi_ignore = ["*/migrations/*", "*/tests/*", "*/conftest.py", "*/node_modules/*"]
#: `imported-members` is deliberately absent: it re-documents every name an
#: `__init__.py` re-exports, which in this package roughly doubles the page
#: count and the build time for entries that already have a canonical page.
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
]

#: `.md` is the house format here; `.rst` remains for `index.rst` only.
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}

#: Markdown written for GitHub uses bare URLs and `#` anchors freely; without
#: this a heading duplicated across two documents is a build warning.
myst_heading_anchors = 3

html_theme = "sphinx_rtd_theme"
