"""Tests for mention parsing and rendering in comment text.

Pure-function tests (extract_location_uuids, is_visible_to) use unittest.TestCase.
DB-backed tests (viewer_pinned_uuids, filter_visible_comments) use django.test.TestCase.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from urbanlens.core.tests.testcase import TestCase
from hypothesis import given, settings
from hypothesis import strategies as st

from urbanlens.dashboard.services.mentions import (
    extract_location_uuids,
    filter_visible_comments,
    is_visible_to,
    render_comment_text,
    viewer_pinned_uuids,
)


# -- Strategies -----------------------------------------------------------------

_uuid_st = st.uuids()
_hyp = settings(max_examples=100, deadline=None)
_hyp_light = settings(max_examples=50, deadline=None)


def _loc_mention(display: str, uid: uuid.UUID) -> str:
    """Produce a well-formed location mention string."""
    return f"@[{display}](loc:{uid})"


# -- extract_location_uuids ----------------------------------------------------

class ExtractLocationUuidsTests(TestCase):
    """extract_location_uuids parses @[...](loc:UUID) tokens."""

    def test_empty_string_returns_empty_list(self) -> None:
        self.assertEqual(extract_location_uuids(""), [])

    def test_no_mentions_returns_empty_list(self) -> None:
        self.assertEqual(extract_location_uuids("Hello world"), [])

    def test_single_mention_returns_one_uuid(self) -> None:
        uid = uuid.uuid4()
        text = _loc_mention("Abandoned Factory", uid)
        result = extract_location_uuids(text)
        self.assertEqual(result, [uid])

    def test_two_mentions_returns_both_uuids(self) -> None:
        uid1, uid2 = uuid.uuid4(), uuid.uuid4()
        text = f"See {_loc_mention('A', uid1)} and {_loc_mention('B', uid2)}"
        result = extract_location_uuids(text)
        self.assertEqual(result, [uid1, uid2])

    def test_act_mention_is_not_extracted(self) -> None:
        self.assertEqual(extract_location_uuids("@act:3"), [])

    def test_malformed_mention_is_ignored(self) -> None:
        self.assertEqual(extract_location_uuids("@[Name](loc:not-a-uuid)"), [])

    def test_mention_without_brackets_is_ignored(self) -> None:
        self.assertEqual(extract_location_uuids("@Name"), [])

    def test_surrounding_text_is_ignored(self) -> None:
        uid = uuid.uuid4()
        text = f"prefix {_loc_mention('X', uid)} suffix"
        self.assertEqual(extract_location_uuids(text), [uid])

    @given(uid=_uuid_st)
    @_hyp
    def test_single_mention_round_trips(self, uid: uuid.UUID) -> None:
        text = _loc_mention("Place", uid)
        result = extract_location_uuids(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], uid)

    @given(uids=st.lists(_uuid_st, min_size=0, max_size=5))
    @_hyp_light
    def test_count_matches_number_of_mentions(self, uids: list[uuid.UUID]) -> None:
        text = " ".join(_loc_mention(f"Place{i}", u) for i, u in enumerate(uids))
        result = extract_location_uuids(text)
        self.assertEqual(len(result), len(uids))


# -- is_visible_to -------------------------------------------------------------

class IsVisibleToTests(TestCase):
    """is_visible_to hides comments when any mentioned location is not pinned."""

    def test_no_mentions_is_always_visible(self) -> None:
        self.assertTrue(is_visible_to("Hello!", set()))

    def test_mentioned_uuid_in_pinned_set_is_visible(self) -> None:
        uid = uuid.uuid4()
        text = _loc_mention("X", uid)
        self.assertTrue(is_visible_to(text, {uid}))

    def test_mentioned_uuid_not_in_pinned_set_is_hidden(self) -> None:
        uid = uuid.uuid4()
        text = _loc_mention("X", uid)
        self.assertFalse(is_visible_to(text, set()))

    def test_all_mentions_must_be_pinned(self) -> None:
        uid1, uid2 = uuid.uuid4(), uuid.uuid4()
        text = f"{_loc_mention('A', uid1)} {_loc_mention('B', uid2)}"
        self.assertFalse(is_visible_to(text, {uid1}))
        self.assertTrue(is_visible_to(text, {uid1, uid2}))

    def test_extra_uuids_in_pinned_set_dont_affect_result(self) -> None:
        uid = uuid.uuid4()
        extra = uuid.uuid4()
        text = _loc_mention("X", uid)
        self.assertTrue(is_visible_to(text, {uid, extra}))

    @given(uid=_uuid_st, extra_uuids=st.frozensets(_uuid_st, max_size=5))
    @_hyp
    def test_visible_iff_mentioned_uuid_in_pinned(
        self, uid: uuid.UUID, extra_uuids: frozenset[uuid.UUID]
    ) -> None:
        text = _loc_mention("Place", uid)
        pinned_with = extra_uuids | {uid}
        pinned_without = extra_uuids - {uid}
        self.assertTrue(is_visible_to(text, set(pinned_with)))
        self.assertFalse(is_visible_to(text, set(pinned_without)))


# -- render_comment_text -------------------------------------------------------

class RenderCommentTextTests(TestCase):
    """render_comment_text returns None for hidden comments and HTML for visible ones."""

    def _render(self, text: str, pinned: set | None = None, activity_index: dict | None = None):
        return render_comment_text(text, pinned or set(), activity_index)

    def test_returns_none_when_mention_not_pinned(self) -> None:
        uid = uuid.uuid4()
        text = _loc_mention("Spot", uid)
        self.assertIsNone(self._render(text, set()))

    def test_plain_text_is_returned_escaped(self) -> None:
        result = self._render("Hello <world>")
        self.assertIsNotNone(result)
        self.assertIn("&lt;world&gt;", str(result))

    def test_mention_renders_as_anchor(self) -> None:
        uid = uuid.uuid4()
        text = _loc_mention("Coolspot", uid)
        result = self._render(text, {uid})
        self.assertIsNotNone(result)
        self.assertIn("Coolspot", str(result))
        self.assertIn("<a ", str(result))
        self.assertIn("mention--location", str(result))

    def test_text_before_and_after_mention_is_preserved(self) -> None:
        uid = uuid.uuid4()
        text = f"before {_loc_mention('X', uid)} after"
        result = self._render(text, {uid})
        self.assertIsNotNone(result)
        html = str(result)
        self.assertIn("before ", html)
        self.assertIn(" after", html)

    def test_unknown_act_mention_is_escaped_not_rendered(self) -> None:
        # @act:99 with no activity_index_map → falls through to escaped literal
        result = self._render("@act:99", set(), {})
        self.assertIsNotNone(result)
        self.assertIn("@act:99", str(result))

    def test_act_mention_resolved_from_index(self) -> None:
        activity = MagicMock()
        activity.effective_title = "Activity Alpha"
        activity.id = 7
        activity.location = None
        result = self._render("@act:1", set(), {1: activity})
        self.assertIsNotNone(result)
        html = str(result)
        self.assertIn("Activity Alpha", html)
        self.assertIn("mention--activity", html)

    def test_act_mention_with_location_renders_link(self) -> None:
        # reverse is imported inside the function body so must be patched at its source.
        loc = MagicMock()
        loc.uuid = uuid.uuid4()
        activity = MagicMock()
        activity.effective_title = "Location Activity"
        activity.id = 42
        activity.location = loc
        with patch("django.urls.reverse", return_value="/wiki/test/"):
            result = self._render("@act:2", set(), {2: activity})
        self.assertIsNotNone(result)
        html = str(result)
        self.assertIn("Location Activity", html)
        self.assertIn("/wiki/test/", html)

    def test_empty_text_returns_empty_string_not_none(self) -> None:
        result = self._render("")
        self.assertIsNotNone(result)
        self.assertEqual(str(result), "")


# -- viewer_pinned_uuids and filter_visible_comments ---------------------------

try:
    from django.contrib.auth.models import User as _User
    from django.test import TestCase as DjangoTestCase
    from model_bakery import baker

    from urbanlens.dashboard.models.location.model import Location as _Location
    from urbanlens.dashboard.models.profile.model import Profile as _Profile

    class ViewerPinnedUuidsTests(DjangoTestCase):
        """viewer_pinned_uuids queries the Pin model for a profile's pinned locations."""

        def test_returns_empty_set_for_profile_with_no_pins(self) -> None:
            profile: _Profile = baker.make(_User).profile
            result = viewer_pinned_uuids(profile)
            self.assertIsInstance(result, set)
            self.assertEqual(len(result), 0)

        def test_returns_uuid_for_pinned_location(self) -> None:
            user: _User = baker.make(_User)
            location: _Location = baker.make(_Location, latitude=40.0, longitude=-74.0)
            baker.make("dashboard.Pin", profile=user.profile, location=location)
            result = viewer_pinned_uuids(user.profile)
            self.assertIn(location.uuid, result)

        def test_does_not_include_other_users_pins(self) -> None:
            user1: _User = baker.make(_User)
            user2: _User = baker.make(_User)
            location: _Location = baker.make(_Location, latitude=41.0, longitude=-75.0)
            baker.make("dashboard.Pin", profile=user2.profile, location=location)
            result = viewer_pinned_uuids(user1.profile)
            self.assertNotIn(location.uuid, result)

        def test_pin_without_location_is_excluded(self) -> None:
            user: _User = baker.make(_User)
            baker.make("dashboard.Pin", profile=user.profile, location=None)
            result = viewer_pinned_uuids(user.profile)
            self.assertEqual(len(result), 0)

    class FilterVisibleCommentsTests(DjangoTestCase):
        """filter_visible_comments returns only comments visible to the profile."""

        def _make_comment(self, text: str) -> MagicMock:
            comment = MagicMock()
            comment.text = text
            return comment

        def test_empty_list_returns_empty(self) -> None:
            profile: _Profile = baker.make(_User).profile
            self.assertEqual(filter_visible_comments([], profile), [])

        def test_comment_without_mentions_is_always_visible(self) -> None:
            profile: _Profile = baker.make(_User).profile
            comment = self._make_comment("No mentions here")
            result = filter_visible_comments([comment], profile)
            self.assertEqual(result, [comment])

        def test_comment_with_pinned_location_is_visible(self) -> None:
            user: _User = baker.make(_User)
            location: _Location = baker.make(_Location, latitude=42.0, longitude=-76.0)
            baker.make("dashboard.Pin", profile=user.profile, location=location)
            comment = self._make_comment(_loc_mention("Place", location.uuid))
            result = filter_visible_comments([comment], user.profile)
            self.assertEqual(result, [comment])

        def test_comment_with_unpinned_location_is_hidden(self) -> None:
            user: _User = baker.make(_User)
            uid = uuid.uuid4()
            comment = self._make_comment(_loc_mention("Secret", uid))
            result = filter_visible_comments([comment], user.profile)
            self.assertEqual(result, [])

except ImportError:
    pass
