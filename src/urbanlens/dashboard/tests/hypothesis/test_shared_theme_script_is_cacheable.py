"""The comment-map composer is a file the browser keeps, not 52 KB inlined into every page on the site.

``themes/base.html`` carried a 51,976-byte ``<script>`` defining the comment map and image-attachment composers.
Every page extends that theme, so every page paid for it, whether or not it could open a composer at all - and
being inline, no visit could reuse the previous one's copy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory
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
from urbanlens.UrbanLens.settings.app import settings as app_settings

SCRIPT_FILE = Path(__file__).resolve().parents[3] / "dashboard/frontend/static/js/comment-map.js"

#: A name only the composer declares, so finding it inline means the program is still being re-sent.
_COMPOSER_PROGRAM = b"_openCommentMapComposer"

_CONFIG_ID = "comment-map-config"
_CONTEXT_VAR = "comment_map_config"


class SharedThemeScriptIsCacheableTests(TestCase):
    """What every page inherits from the theme is a script tag, not a program."""

    def setUp(self) -> None:
        super().setUp()
        # The dev toolbar is admin chrome that a visitor is never sent, and tests render it by default.
        toolbar = patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=False)
        toolbar.start()
        self.addCleanup(toolbar.stop)
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def body(self) -> bytes:
        """An ordinary page, to see what the theme gives every one of them.

        Returns:
            The rendered home page."""
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        return bytes(response.content)

    def test_no_inline_block_carries_a_program(self) -> None:
        largest = largest_inline(self.body())
        self.assertLessEqual(
            largest,
            MAX_INLINE_SCRIPT_BYTES,
            f"every page still inlines a {largest:,}-byte script, re-sent on every visit",
        )

    def test_the_composer_itself_is_never_inline(self) -> None:
        """Size alone would not notice the program coming back in pieces."""
        inlined = [block for block in inline_blocks(self.body()) if _COMPOSER_PROGRAM in block]
        self.assertEqual(inlined, [], "the composer is being re-sent inline rather than served as a file")

    def test_the_page_loads_the_script_from_a_file(self) -> None:
        self.assertIn(b"js/comment-map.js", self.body(), "the theme does not reference the cacheable script")

    def test_the_config_carries_everything_the_script_reads(self) -> None:
        config = rendered_config(self.body(), _CONFIG_ID)
        self.assertIsNotNone(config, "the page renders no config element for the composer")
        self.assertEqual(
            missing_config(SCRIPT_FILE.read_text(), config or {}), [], "the script reads config the page does not send"
        )

    def test_the_script_holds_no_template_syntax(self) -> None:
        """A Django tag left in the file is inert JavaScript that ships to the browser as literal text."""
        self.assertEqual(template_syntax(SCRIPT_FILE.read_text()), [])

    def test_every_config_read_is_live_code(self) -> None:
        self.assertEqual(inert_reads(SCRIPT_FILE.read_text()), [], "these config reads are quoted text, not values")

    def test_the_config_survives_a_signed_out_request(self) -> None:
        """``logged_out.html`` extends the theme, and an anonymous request has no profile to read a uuid from.

        The template shrugged that off; a processor reaching for ``request.user.profile`` would not.
        """
        from urbanlens.dashboard.context_processors import add_comment_map_config

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        self.assertEqual(add_comment_map_config(request)[_CONTEXT_VAR]()["profileUuid"], "")

    def test_signing_out_does_not_error(self) -> None:
        """The end-to-end path that processor has to survive."""
        self.assertLess(self.client.post(reverse("logout"), follow=True).status_code, 500)
