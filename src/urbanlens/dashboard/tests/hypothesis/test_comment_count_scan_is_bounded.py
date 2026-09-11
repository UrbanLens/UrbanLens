"""The comment-count badge must not read every comment on the thread.

The badge has to agree with the thread rather than report a raw ``.count()``,
because a raw total announces that something was hidden and roughly where -
the existence oracle ``services.comments.comments`` is built to deny. Agreeing
meant applying the ``@loc`` gate in Python, which meant pulling the full text
of every comment carrying a mention, on every pin and wiki page load, with no
ceiling (N21 H18).

Anyone who can comment can therefore set what every later viewer of that page
pays. The count stays exact while the thread is a normal size and becomes a
floor - displayed with a ``+`` - above the ceiling, which is safe in the
direction that matters: a badge lower than the thread reveals nothing, while a
badge higher than the thread is the oracle itself.
"""

from __future__ import annotations

from unittest import mock
import uuid as uuid_module

from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.comments.comments import visible_comment_count
from urbanlens.dashboard.services.notifications import mentions

SETTING_NAME = "COMMENT_COUNT_SCAN_LIMIT"


class _ThreadCase(TestCase):
    """A pin whose comments the viewer can read, and a place they have pinned."""

    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.viewer = Profile.objects.get(user=baker.make("auth.User"))
        self.author = Profile.objects.get(user=baker.make("auth.User"))
        self.pinned_location = baker.make(Location)
        baker.make(Pin, profile=self.viewer, location=self.pinned_location)
        self.pin = baker.make(Pin, profile=self.viewer)

    def _comment(self, text: str) -> Comment:
        return baker.make(Comment, pin=self.pin, profile=self.author, text=text)

    def _mentioning(self, count: int, *, location_uuid: uuid_module.UUID | None = None) -> None:
        target = location_uuid or self.pinned_location.uuid
        for index in range(count):
            self._comment(f"visiting @[the mill {index}](loc:{target})")

    def _count(self):
        return visible_comment_count(self.pin.comments.all(), self.viewer)


class TheScanIsBoundedTests(_ThreadCase):
    """What one commenter can make every later viewer pay."""

    @override_settings(**{SETTING_NAME: 5})
    def test_the_visibility_gate_runs_no_more_than_the_ceiling_allows(self) -> None:
        self._mentioning(30)

        with mock.patch.object(mentions, "is_visible_to", wraps=mentions.is_visible_to) as gate:
            self._count()

        self.assertLessEqual(
            gate.call_count,
            5,
            f"the badge evaluated {gate.call_count} comments against a ceiling of 5, so a thread's "
            "length still sets what every viewer of the page pays",
        )

    @override_settings(**{SETTING_NAME: 5})
    def test_a_capped_count_says_so(self) -> None:
        self._mentioning(30)

        result = self._count()

        self.assertTrue(result.capped)
        self.assertTrue(str(result).endswith("+"), f"a capped badge rendered as {str(result)!r}")

    @override_settings(**{SETTING_NAME: 5})
    def test_a_capped_count_never_claims_more_than_it_verified(self) -> None:
        """A badge above the thread is the oracle; a badge below it reveals nothing."""
        self._mentioning(30)

        result = self._count()

        self.assertLessEqual(int(result), 30)


class TheCountIsStillRightTests(_ThreadCase):
    """The half that stops the tests above passing against a badge that stopped counting."""

    def test_the_setting_exists(self) -> None:
        """An override_settings of a name nothing reads configures nothing."""
        from django.conf import settings

        self.assertTrue(hasattr(settings, SETTING_NAME), f"nothing reads {SETTING_NAME}")

    @override_settings(**{SETTING_NAME: 100})
    def test_an_ordinary_thread_counts_exactly_and_is_not_marked_capped(self) -> None:
        self._comment("no mention here")
        self._comment("nor here")
        self._mentioning(3)

        result = self._count()

        self.assertEqual(int(result), 5)
        self.assertFalse(result.capped)

    @override_settings(**{SETTING_NAME: 100})
    def test_a_mention_of_a_place_the_viewer_has_not_pinned_is_still_dropped(self) -> None:
        self._comment("plain")
        self._mentioning(2, location_uuid=baker.make(Location).uuid)

        result = self._count()

        self.assertEqual(int(result), 1, "the @loc gate stopped hiding comments the viewer cannot see")

    @override_settings(**{SETTING_NAME: 100})
    def test_the_badge_is_falsy_when_nothing_is_visible(self) -> None:
        """Every template guards the badge with `{% if %}` before rendering it."""
        self._mentioning(2, location_uuid=baker.make(Location).uuid)

        self.assertFalse(self._count())
