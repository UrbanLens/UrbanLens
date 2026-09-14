"""Sphinx configuration.

Uses AutoAPI (parses source, no Django setup needed) and MyST for Markdown.
Build with ``bin/build_docs.py``.
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

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
# Built via a hidden glob toctree in index.rst to keep navigation short.

#: Google-style docstrings, as required.
napoleon_google_docstring = True
napoleon_numpy_docstring = False

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

autoapi_type = "python"
autoapi_dirs = [str(_REPO_ROOT / "src" / "urbanlens")]
autoapi_root = "api"
autoapi_keep_files = False
#: Migrations and tests are excluded from the API reference.
autoapi_ignore = ["*/migrations/*", "*/tests/*", "*/conftest.py", "*/node_modules/*"]
#: Skip re-exported names; they already have a canonical page.
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
]

#: `.md` is the house format; `.rst` remains for `index.rst` only.
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}

myst_heading_anchors = 3

html_theme = "sphinx_rtd_theme"
