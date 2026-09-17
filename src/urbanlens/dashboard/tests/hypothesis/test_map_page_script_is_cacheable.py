"""The map page's behaviour is a file the browser keeps, not 275 KB re-sent on every visit.

An inline ``<script>`` cannot be cached, cannot be minified by the bundler, and is invisible to the TypeScript
checks (P83). The map page's single block was 275 KB of a 589 KB response - 69 KB compressed, on every map view -
and everything in it is static apart from a few dozen URLs and flags, which now arrive in a config element.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.inline_scripts import (
    MAX_INLINE_SCRIPT_BYTES,
    inert_reads,
    inline_blocks,
    largest_inline,
    missing_config,
    rendered_config,
    template_syntax,
)
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.UrbanLens.settings.app import settings as app_settings

SCRIPT_FILE = Path(__file__).resolve().parents[3] / "dashboard/frontend/ts/entries/map-page.ts"

#: A name only the map program declares, so its presence inline means the program itself is still being re-sent.
_MAP_PROGRAM = b"_SERVER_CENTER_LAT"

_CONFIG_ID = "map-page-config"


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
        largest = largest_inline(self.body())
        self.assertLessEqual(
            largest,
            MAX_INLINE_SCRIPT_BYTES,
            f"the map page still inlines a {largest:,}-byte script, re-sent on every visit",
        )

    def test_the_map_program_itself_is_never_inline(self) -> None:
        """Size alone would not notice the program coming back in pieces."""
        inlined = [block for block in inline_blocks(self.body()) if _MAP_PROGRAM in block]
        self.assertEqual(inlined, [], "the map program is being re-sent inline rather than served as a file")

    def test_the_page_loads_the_script_from_a_file(self) -> None:
        self.assertIn(b"js/map-page.js", self.body(), "the map page does not reference the cacheable script")

    def test_the_config_carries_everything_the_script_reads(self) -> None:
        """The script is only correct if the server sends what it looks up: check read against rendered."""
        config = rendered_config(self.body(), _CONFIG_ID)
        self.assertIsNotNone(config, "the map page renders no #map-page-config element")
        self.assertEqual(
            missing_config(SCRIPT_FILE.read_text(), config or {}), [], "the script reads config the page does not send"
        )

    def test_the_script_holds_no_template_syntax(self) -> None:
        """A Django tag left in the file is inert JavaScript that ships to the browser as literal text."""
        self.assertEqual(template_syntax(SCRIPT_FILE.read_text()), [])

    def test_every_config_read_is_live_code(self) -> None:
        """A read inside a string literal is text, not a value, and fails silently.

        ``'history_v1_' + CFG.profileId`` keys one account's stored history; ``'history_v1_CFG.profileId'``
        keys everyone's to the same string, so on a shared browser one account would show another's.
        """
        self.assertEqual(inert_reads(SCRIPT_FILE.read_text()), [], "these config reads are quoted text, not values")
