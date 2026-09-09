"""P89: `MarkupJsonView`'s `?children=1` wiki path must apply concealment.

The single-wiki path (``_resolve_owner``'s wiki branch) narrows through
``visible_rows`` before returning. The ``?children=1`` aggregation branch,
94 lines further down the same ``get()``, rebuilt ``items`` from a raw
``PinMarkup.objects.filter(parent_wiki__in=subtree)`` that never touched
``visible_rows`` at all - so a concealed viewer asking for the descendant
subtree got every wiki's markup back unfiltered, not just their own and
their friends'. See ``docs/PROBLEMS.md`` P89.

Not a live leak while ``concealment_active()`` stays hardcoded False (as it
is today) - every test below that exercises the bug forces it on, matching
the pattern already used in ``test_wiki_concealment.py`` /
``test_concealed_render.py`` / ``test_concealment_own_contribution_round4.py``.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_CONCEALED = mock.patch("urbanlens.dashboard.services.wiki.concealment.concealment_active", return_value=True)


class MarkupJsonChildrenConcealmentTests(TestCase):
    """`?children=1` on a wiki owner must filter the subtree the same as a single wiki does."""

    def setUp(self) -> None:
        super().setUp()
        self.root_location = baker.make(Location)
        self.root = baker.make(Wiki, location=self.root_location, name="Root")
        self.child = baker.make(Wiki, parent_wiki=self.root, location=baker.make(Location), name="Child")

        self.viewer_user = baker.make(User)
        # A pin at the root location is what grants wiki access at all; it says
        # nothing about whether the viewer has earned the community's detail.
        baker.make(Pin, profile=self.viewer_user.profile, location=self.root_location)

        self.stranger = baker.make(User).profile
        self.stranger_item = baker.make(
            PinMarkup,
            parent_wiki=self.child,
            profile=self.stranger,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            label="stranger's entrance route",
        )

        self.client.force_login(self.viewer_user)

    def _get_children(self):
        return self.client.get(reverse("location.wiki.markup.json", args=[self.root_location.slug]), {"children": "1"})

    def test_a_concealed_viewer_does_not_see_a_strangers_descendant_wiki_markup(self) -> None:
        """The bypass itself: children=1 must not skip visible_rows."""
        with _CONCEALED:
            response = self._get_children()

        self.assertEqual(response.status_code, 200)
        uuids = {item["uuid"] for item in response.json()["markup_items"]}
        self.assertNotIn(str(self.stranger_item.uuid), uuids)

    def test_an_unconcealed_viewer_still_sees_it(self) -> None:
        """Anti-vacuity: concealment-disabled (today's real default) must still show it."""
        response = self._get_children()

        self.assertEqual(response.status_code, 200)
        uuids = {item["uuid"] for item in response.json()["markup_items"]}
        self.assertIn(str(self.stranger_item.uuid), uuids)

    def test_the_viewers_own_item_on_a_descendant_wiki_still_shows_when_concealed(self) -> None:
        """Anti-vacuity the other way: the fix must be visible_rows, not a blanket hide.

        Own contributions survive concealment everywhere else in this file's
        siblings; children=1 must not become the one path that forgets that.
        """
        own_item = baker.make(
            PinMarkup,
            parent_wiki=self.child,
            profile=self.viewer_user.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            label="my own route",
        )

        with _CONCEALED:
            response = self._get_children()

        uuids = {item["uuid"] for item in response.json()["markup_items"]}
        self.assertIn(str(own_item.uuid), uuids)
        self.assertNotIn(str(self.stranger_item.uuid), uuids)

    def test_a_friends_item_on_a_descendant_wiki_still_shows_when_concealed(self) -> None:
        """Friends talk offline - the friend clause has to survive this path too."""
        friend = baker.make(User).profile
        baker.make(
            Friendship, from_profile=self.viewer_user.profile, to_profile=friend, status=FriendshipStatus.ACCEPTED
        )
        friend_item = baker.make(
            PinMarkup,
            parent_wiki=self.child,
            profile=friend,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            label="friend's route",
        )

        with _CONCEALED:
            response = self._get_children()

        uuids = {item["uuid"] for item in response.json()["markup_items"]}
        self.assertIn(str(friend_item.uuid), uuids)
        self.assertNotIn(str(self.stranger_item.uuid), uuids)
