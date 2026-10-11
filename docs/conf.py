"""
Sphinx configuration.

Uses AutoAPI (parses source, no Django setup needed) and MyST for Markdown.
Build with ``bin/build_docs.py``.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from docutils.statemachine import StringList
from sphinx.directives.other import TocTree, glob_re
from sphinx.util import docname_join
from sphinx.util.matching import patfilter

if TYPE_CHECKING:
    from docutils.nodes import Node
    from sphinx.application import Sphinx

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

#: `internal/` holds the project's working records, which are not part of the published documentation.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "internal/**"]

#: Google-style docstrings, as required.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
#: An ``Attributes:`` section becomes ``:ivar:`` fields. As ``.. attribute::`` entries it described each attribute a
#: second time beside autoapi's own entry, which is 1,275 "duplicate object description" warnings.
napoleon_use_ivar = True

suppress_warnings = [
    # A name a module re-exports from another (``inference_client`` re-exports ``urbanlens_ai.schema``) is one
    # autoapi cannot follow. Python resolves it and mypy checks it, so the warning says nothing about the code.
    "autoapi.python_import_resolution",
    # An annotation naming the builtin ``type`` matches every attribute called ``type`` in the reference, and the
    # Python domain warns that the target is ambiguous. Nothing here documents the builtin, so no link is right.
    "ref.python",
]

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


class PresentTocTree(TocTree):
    """
    A ``toctree`` that leaves out the pages this checkout does not have.

    The public mirror keeps only this file, ``index.rst`` and ``__init__.py`` of ``docs/``
    (see ``.forgejo/sync.toml``), so a plain ``toctree`` would warn once per missing page and
    a strict build would fail there. ``index.rst`` lists its pages with this directive instead:
    a full checkout builds every page, and the mirror builds the API reference alone. Entries
    are plain document names or, under ``:glob:``, patterns; an entry naming nothing here is
    dropped, and a toctree left with no entries renders nothing, caption included.
    """

    def run(self) -> list[Node]:
        """Drop the entries that name no document, then build the toctree from the rest."""
        current = self.env.current_document.docname
        found = self.env.found_docs - {current}
        kept = []
        for line in self.content:
            entry = line.strip()
            if not entry:
                continue
            name = docname_join(current, entry)
            present = patfilter(found, name) if "glob" in self.options and glob_re.match(entry) else name in found
            if present:
                kept.append(entry)
        if not kept:
            return []
        self.content = StringList(kept, items=[self.content.items[0]] * len(kept))
        return super().run()


_DOCUMENTED_DATA: set[str] = set()


def _skip_reassigned_data(_app: Sphinx, what: str, name: str, _obj: object, skip: bool, _options: list[str]) -> bool | None:
    """
    Document a module-level name once, however many times the module assigns it.

    autoapi lists one entry per assignment, so ``CORS_ALLOWED_ORIGINS = ...`` and a later
    ``CORS_ALLOWED_ORIGINS = [...]`` became two descriptions of one object.
    """
    if what != "data" or skip:
        return None
    if name in _DOCUMENTED_DATA:
        return True
    _DOCUMENTED_DATA.add(name)
    return None


def setup(app: Sphinx) -> None:
    """Register ``present-toctree`` for ``index.rst``, and keep reassigned names to one entry."""
    app.add_directive("present-toctree", PresentTocTree)
    app.connect("autoapi-skip-member", _skip_reassigned_data)
