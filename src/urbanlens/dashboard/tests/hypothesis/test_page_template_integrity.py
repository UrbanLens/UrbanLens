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
                    self.assertIn("integrity=", tag, "A cross-origin script is loaded without a subresource integrity hash.")
                    self.assertIn("crossorigin=", tag, "integrity is only enforced when the request is made with CORS; add crossorigin=\"anonymous\".")
