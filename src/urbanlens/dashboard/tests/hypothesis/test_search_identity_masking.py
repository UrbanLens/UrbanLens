"""Search results must not name people the rest of the app masks.

Every surface that displays someone else's name resolves it first: the messages page
and the DM export through ``display_identity_for``, trip comments and pin/wiki
comments through ``resolve_visible_identities`` (whose ``is_masked``/``display_name``
the comment template branches on).

Global search built its titles and subtitles from ``.username`` directly, so a search
box returned names the page rendering the very same rows would have masked.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments import Comment
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import CommentSearchProvider, DirectMessageSearchProvider


def _profile(visibility: str = VisibilityChoice.ANYONE) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(profile_visibility=visibility, direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


def _rendered(results) -> str:
    """Everything a result puts on screen, as one string."""
    return " ".join(f"{r.title} {r.subtitle or ''}" for r in results)


class DirectMessageSearchMaskingTests(TestCase):
    """The conversation partner is masked in search exactly as on the messages page."""

    def setUp(self):
        super().setUp()
        self.searcher = _profile()
        self.hidden_partner = _profile(VisibilityChoice.NO_ONE)
        self.open_partner = _profile()

        DirectMessage.objects.create(sender=self.hidden_partner, recipient=self.searcher, body="meet at the quarry")
        DirectMessage.objects.create(sender=self.open_partner, recipient=self.searcher, body="quarry photos attached")

    def _search(self) -> str:
        results = DirectMessageSearchProvider().search(self.searcher, parse_query("quarry"), 20)
        self.assertTrue(results, "the fixture messages should match the query")
        return _rendered(results)

    def test_the_partner_is_masked_on_the_messages_page(self):
        # Premise: search is inconsistent with the page, not with a rule nobody applies.
        self.assertFalse(self.hidden_partner.can_view_profile(self.searcher))

    def test_a_hidden_partners_username_is_not_in_the_results(self):
        self.assertNotIn(self.hidden_partner.username, self._search())

    def test_a_visible_partners_username_still_is(self):
        self.assertIn(self.open_partner.username, self._search())


class CommentSearchMaskingTests(TestCase):
    """Comment authors are masked in search exactly as in the comment list."""

    def setUp(self):
        super().setUp()
        self.searcher = _profile()
        self.hidden_author = _profile(VisibilityChoice.NO_ONE)
        self.open_author = _profile()

        location = Location.objects.create(latitude=45.1, longitude=-70.3)
        self.pin = Pin.objects.create(profile=self.searcher, location=location, name="Quarry")
        Comment.objects.create(pin=self.pin, profile=self.hidden_author, text="quarry access is fenced")
        Comment.objects.create(pin=self.pin, profile=self.open_author, text="quarry gate is open")

        self.trip = Trip.objects.create(name="Quarry trip", creator=self.searcher)
        self.trip.profiles.add(self.searcher, self.hidden_author, self.open_author)
        TripComment.objects.create(trip=self.trip, author=self.hidden_author, text="quarry route planned")
        TripComment.objects.create(trip=self.trip, author=self.open_author, text="quarry meetup time")

    def _search(self) -> str:
        results = CommentSearchProvider().search(self.searcher, parse_query("quarry"), 20)
        self.assertTrue(results, "the fixture comments should match the query")
        return _rendered(results)

    def test_a_hidden_authors_username_is_not_in_the_results(self):
        self.assertNotIn(self.hidden_author.username, self._search())

    def test_a_visible_authors_username_still_is(self):
        self.assertIn(self.open_author.username, self._search())

    def test_the_results_are_still_returned(self):
        # Masking a name must not drop the row - the searcher is entitled to find
        # the comment, just not to learn who wrote it.
        results = CommentSearchProvider().search(self.searcher, parse_query("quarry"), 20)
        self.assertGreaterEqual(len(results), 4)
