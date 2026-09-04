"""Structural guarantees about the two templates every page is built from.

Both properties here were found missing by the Playwright suite
(`tests/integration/`, see docs/PROBLEMS.md 2026-08-23). That suite runs by hand
against a deployment, so it can go months between runs; these assertions are the
cheap half, and they run on every commit.

Deliberately reading the template *source* rather than rendering it. Neither
property depends on context, rendering `themes/base.html` needs a request with an
authenticated user and a profile, and - more to the point - the useful form of
the second assertion is "no unpinned tag exists anywhere in this file", which is
a statement about the file, not about one rendering of it.
"""

from __future__ import annotations

from pathlib import Path
import re

from django.conf import settings
from django.test import SimpleTestCase

#: The two templates every user-facing page extends.
_THEME_TEMPLATES = ("dashboard/themes/base.html", "dashboard/themes/auth_base.html")

#: A `<script>` served from another origin. Same-origin tags (`{% static %}`)
#: are excluded: SRI protects against a third party changing a file we do not
#: control, and there is no third party in a static tag.
_REMOTE_SCRIPT = re.compile(r"<script\b[^>]*\bsrc=[\"']https://[^\"']+[^>]*>", re.IGNORECASE)

_OPENING_HTML_TAG = re.compile(r"<html\b[^>]*>", re.IGNORECASE)

#: ``{% extends 'name' %}`` with a literal template name. A variable target
#: (``{% extends base_template %}``) is unresolvable from source and skipped.
_EXTENDS = re.compile(r"{%\s*extends\s+[\"']([^\"']+)[\"']\s*%}")

_BLOCK = re.compile(r"{%\s*block\s+([A-Za-z0-9_]+)")

#: Templates root, for the whole-tree scan below. Taken from the loader's own
#: DIRS rather than built from BASE_DIR, which points at the settings package.
_TEMPLATES_ROOT = Path(settings.TEMPLATES[0]["DIRS"][0])


def _template_source(name: str) -> str:
    """Read one template's raw text.

    Args:
        name: Template name as it would be given to ``render``.

    Returns:
        The file's contents.
    """
    for directory in settings.TEMPLATES[0]["DIRS"]:
        candidate = Path(directory) / name
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    # APP_DIRS templates: dashboard/templates/<name>.
    candidate = Path(settings.BASE_DIR) / "dashboard" / "templates" / name
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    raise AssertionError(f"Could not locate template {name!r} on disk.")


class PageLanguageTests(SimpleTestCase):
    """WCAG 3.1.1: a page has to say what language it is in."""

    def test_every_theme_declares_a_language(self) -> None:
        """``<html>`` must carry ``lang``.

        Without it a screen reader guesses which language to pronounce the page
        in, and guesses wrong for anyone whose system language differs. axe
        reported ``html-has-lang`` at *serious* on all ten scanned pages, which
        was ten of the integration suite's thirteen failures - one attribute,
        repeated everywhere.
        """
        for name in _THEME_TEMPLATES:
            with self.subTest(template=name):
                opening = _OPENING_HTML_TAG.search(_template_source(name))
                self.assertIsNotNone(opening, f"{name} has no <html> tag.")
                self.assertIn("lang=", opening.group(0), f"{name}'s <html> tag declares no lang attribute.")


class BlockNameTests(SimpleTestCase):
    """A child block whose name no ancestor defines is silently discarded.

    Django treats an unmatched ``{% block %}`` in a child template as dead
    content rather than an error, so the page renders and simply never shows
    what the block was for. Twelve pages declared ``{% block title %}`` against
    ``themes/base.html``, whose ``<title>`` block is ``page_title`` - every one
    of them showed the site default instead of its own name, for as long as they
    have existed.
    """

    def _ancestor_blocks(self, template: Path) -> set[str] | None:
        """Collect block names defined anywhere up this template's extends chain.

        Args:
            template: Path to the child template.

        Returns:
            The set of block names an ancestor defines, or ``None`` when the
            chain cannot be resolved from source (a variable ``extends``
            target, or a parent that is not on disk).
        """
        names: set[str] = set()
        current = template
        seen: set[Path] = set()
        while True:
            extends = _EXTENDS.search(current.read_text(encoding="utf-8"))
            if not extends:
                return names
            parent = _TEMPLATES_ROOT / extends.group(1)
            if not parent.exists() or parent in seen:
                return None
            seen.add(parent)
            names.update(_BLOCK.findall(parent.read_text(encoding="utf-8")))
            current = parent

    @staticmethod
    def _top_level_blocks(source: str) -> list[str]:
        """Block names declared outside any other block in this template.

        Only these can be dropped. A block *nested* inside one the ancestor does
        define renders as part of it, and introducing a new name there is how a
        template offers an override point to its own children - which is what
        ``errors/404.html`` does with ``error_title`` for ``pin_not_found.html``.

        Args:
            source: The template's raw text.

        Returns:
            The names declared at depth zero, in document order.
        """
        names: list[str] = []
        depth = 0
        for match in re.finditer(r"{%\s*(block\s+([A-Za-z0-9_]+)|endblock)", source):
            if match.group(1).startswith("endblock"):
                depth -= 1
                continue
            if depth == 0:
                names.append(match.group(2))
            depth += 1
        return names

    def test_every_declared_block_is_defined_by_an_ancestor(self) -> None:
        checked = 0
        for template in sorted(_TEMPLATES_ROOT.rglob("*.html")):
            source = template.read_text(encoding="utf-8")
            if not _EXTENDS.search(source):
                continue
            defined = self._ancestor_blocks(template)
            if defined is None:
                continue
            checked += 1
            for name in self._top_level_blocks(source):
                with self.subTest(template=str(template.relative_to(_TEMPLATES_ROOT)), block=name):
                    self.assertIn(
                        name,
                        defined,
                        f"{template.name} declares {{% block {name} %}}, which no template it extends defines - Django drops it silently.",
                    )
        # Guard against the scan passing because it matched nothing. 89 templates
        # extend another as of this writing; the floor only has to be high enough
        # that a broken path cannot slip through.
        self.assertGreater(
            checked, 70, "The template scan resolved almost no extends chains; it is not testing what it claims."
        )


class SubresourceIntegrityTests(SimpleTestCase):
    """Third-party scripts must be pinned to a hash."""

    def test_every_cross_origin_script_is_pinned(self) -> None:
        """A remote ``<script>`` without ``integrity`` is total control of the app.

        HTMX was loaded from unpkg with no hash while the jQuery and toastr tags
        either side of it both had one, which is the tell that it was an
        oversight rather than a decision. HTMX drives essentially every
        interaction here, so whoever controls that CDN response controls the
        application for every visitor.

        Asserted over the whole file rather than against one known URL, so the
        next unpinned tag fails too.

        Stylesheets are out of scope on purpose: Google Fonts serves a different
        stylesheet per user agent and cannot be hashed at all, so a blanket rule
        over ``<link>`` would have to carry an exception list that quietly grows.
        """
        for name in _THEME_TEMPLATES:
            source = _template_source(name)
            for tag in _REMOTE_SCRIPT.findall(source):
                with self.subTest(template=name, tag=tag[:80]):
                    self.assertIn(
                        "integrity=", tag, "A cross-origin script is loaded without a subresource integrity hash."
                    )
                    self.assertIn(
                        "crossorigin=",
                        tag,
                        'integrity is only enforced when the request is made with CORS; add crossorigin="anonymous".',
                    )
