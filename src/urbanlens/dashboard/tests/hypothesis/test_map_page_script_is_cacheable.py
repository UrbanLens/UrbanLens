"""The map page's behaviour is a file the browser keeps, not 275 KB re-sent on every visit.

An inline ``<script>`` cannot be cached, cannot be minified by the bundler, and is invisible to the TypeScript
checks (P83). The map page's single block was 275 KB of a 589 KB response - 69 KB compressed, on every map view -
and everything in it is static apart from a few dozen URLs and flags, which now arrive in a config element.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.UrbanLens.settings.app import settings as app_settings

#: What one inline block may still hold. Small values (a JSON island, a few lines of wiring) are fine; a program is
#: not. The block this replaced was 275,245 bytes.
MAX_INLINE_SCRIPT_BYTES = 20_000

SCRIPT_FILE = Path(__file__).resolve().parents[3] / "dashboard/frontend/static/js/map-page.js"

#: The shared theme inlines a comment-map composer into every page on the site (``themes/base.html``). That is the
#: same defect at a wider scale and is tracked on its own; excluded here so this test speaks only for the map page.
_SHARED_THEME_BLOCK = b"_makeRefMarker"

#: A name only the map program declares, so its presence inline means the program itself is still being re-sent.
_MAP_PROGRAM = b"_SERVER_CENTER_LAT"

_INLINE = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)
_CONFIG = re.compile(rb'<script[^>]*id="map-page-config"[^>]*>(.*?)</script>', re.DOTALL)
#: Every ``CFG.urls["x"]`` / ``CFG.assets["x"]`` / ``CFG.plainKey`` the script reads.
_INDEXED_READ = re.compile(r'CFG\.(urls|assets)\["([^"]+)"\]')
_PLAIN_READ = re.compile(r"CFG\.([a-zA-Z][a-zA-Z0-9_]*)\b(?!\[)")
#: Any config read at all, indexed or plain.
_ANY_READ = re.compile(r'CFG\.(?:urls|assets)\["[^"]+"\]|CFG\.[a-zA-Z][a-zA-Z0-9_]*')


def _open_delimiter(source: str, position: int) -> str | None:
    """The string delimiter open at *position*, judged on its own line.

    Args:
        source: The script text.
        position: An offset into it.

    Returns:
        The open quote character, or None when *position* is not inside a string."""
    index = source.rfind("\n", 0, position) + 1
    stack: str | None = None
    while index < position:
        character = source[index]
        if character == "\\":
            index += 2
            continue
        if character in "\"'`":
            stack = character if stack is None else (None if stack == character else stack)
        index += 1
    return stack


class MapPageScriptIsCacheableTests(TestCase):
    """The page carries a config and a script tag; the program itself is a static file."""

    def setUp(self) -> None:
        super().setUp()
        # The dev toolbar is admin chrome that a visitor is never sent, and tests render it by default.
        toolbar = patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=False)
        toolbar.start()
        self.addCleanup(toolbar.stop)
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude="41.5", longitude="-71.5"))
        self.client.force_login(self.user)

    def body(self) -> bytes:
        response = self.client.get(reverse("map.view"))
        self.assertEqual(response.status_code, 200)
        return bytes(response.content)

    def test_no_inline_block_carries_a_program(self) -> None:
        blocks = [block for block in _INLINE.findall(self.body()) if _SHARED_THEME_BLOCK not in block]
        largest = max((len(block) for block in blocks), default=0)
        self.assertLessEqual(
            largest,
            MAX_INLINE_SCRIPT_BYTES,
            f"the map page still inlines a {largest:,}-byte script, re-sent on every visit",
        )

    def test_the_map_program_itself_is_never_inline(self) -> None:
        """Size alone would not notice the program coming back in pieces."""
        inlined = [block for block in _INLINE.findall(self.body()) if _MAP_PROGRAM in block]
        self.assertEqual(inlined, [], "the map program is being re-sent inline rather than served as a file")

    def test_the_page_loads_the_script_from_a_file(self) -> None:
        self.assertIn(b"js/map-page.js", self.body(), "the map page does not reference the cacheable script")

    def test_the_config_carries_everything_the_script_reads(self) -> None:
        """The script is only correct if the server sends what it looks up: check read against rendered."""
        source = SCRIPT_FILE.read_text()
        match = _CONFIG.search(self.body())
        self.assertIsNotNone(match, "the map page renders no #map-page-config element")
        config = json.loads(match.group(1).decode()) if match else {}

        missing = sorted(
            f"{group}.{key}" for group, key in set(_INDEXED_READ.findall(source)) if key not in config.get(group, {})
        ) + sorted(
            key for key in set(_PLAIN_READ.findall(source)) if key not in {"urls", "assets"} and key not in config
        )
        self.assertEqual(missing, [], "the script reads config the page does not send")

    def test_the_script_holds_no_template_syntax(self) -> None:
        """A Django tag left in the file is inert JavaScript that ships to the browser as literal text."""
        source = SCRIPT_FILE.read_text()
        code = re.sub(r"//[^\n]*", "", source)  # a comment may still mention one
        self.assertNotIn("{%", code)
        self.assertNotIn("{{", code)

    def test_every_config_read_is_live_code(self) -> None:
        """A read inside a string literal is text, not a value, and fails silently.

        ``'history_v1_' + CFG.profileId`` keys one account's stored history; ``'history_v1_CFG.profileId'``
        keys everyone's to the same string, so on a shared browser one account would show another's.
        """
        source = SCRIPT_FILE.read_text()
        inert = []
        for match in re.finditer(_ANY_READ, source):
            delimiter = _open_delimiter(source, match.start())
            if delimiter is None or (delimiter == "`" and source[match.start() - 2 : match.start()] == "${"):
                continue
            inert.append(source[source.rfind("\n", 0, match.start()) + 1 : source.find("\n", match.end())].strip())
        self.assertEqual(inert, [], "these config reads are quoted text, not values")
