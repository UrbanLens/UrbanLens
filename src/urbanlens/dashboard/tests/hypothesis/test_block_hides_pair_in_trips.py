"""Two people who blocked each other share a trip without seeing each other on it (I7, rulings 1b and 2b).

Adding either to a trip the other is on is allowed. From the moment of the block, neither sees the other's
comments, reactions or activity attribution, nor the other in any roster. What was posted before stays, and the
trip itself - its itinerary, its settings - works as it did.
"""

from __future__ import annotations

from datetime import timedelta
import json
import os
import tempfile

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment, TripMembership
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import CommentSearchProvider, TripSearchProvider
from urbanlens.dashboard.services.import_export.export import _export_trips
from urbanlens.dashboard.services.media.access import authorize_comment_image
from urbanlens.dashboard.services.social.friendship import block_profile
from urbanlens.dashboard.services.trips.trip_activities import build_activity_rows, create_activity
from urbanlens.dashboard.services.trips.trip_comments import (
    add_comment,
    build_comment_tree,
    set_comment_reaction,
    trip_comment_is_visible,
    visible_comment_count,
    visible_comment_queryset,
)
from urbanlens.dashboard.services.trips.trip_crud import create_trip
from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError
from urbanlens.dashboard.services.trips.trip_membership import add_member_by_username, list_members


def _profile(name: str) -> Profile:
    user = baker.make(User, username=f"{name}{os.urandom(3).hex()}")
    # Everyone visible to everyone, so an absence below is the block's doing and not a privacy setting.
    Profile.objects.filter(user=user).update(
        profile_visibility=VisibilityChoice.ANYONE,
        comment_visibility=VisibilityChoice.ANYONE,
        trip_pin_location_visibility=VisibilityChoice.ANYONE,
    )
    profile = Profile.objects.select_related("user").get(user=user)
    profile.ensure_slug()
    return profile


def _age(model, pk: int, *, minutes: int) -> None:
    model.objects.filter(pk=pk).update(created=timezone.now() - timedelta(minutes=minutes))


def _token(user: User) -> dict:
    from oauth2_provider.models import get_access_token_model

    from urbanlens.core.tests.oauth import first_party_application

    token = get_access_token_model().objects.create(
        user=user,
        application=first_party_application(),
        token=f"tok-{os.urandom(8).hex()}",
        expires=timezone.now() + timedelta(hours=1),
        scope=f"{ApiKeyScope.TRIPS_READ.value} {ApiKeyScope.TRIPS_WRITE.value}",
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}


class AddingIsAllowedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.carol = _profile("carol")
        self.trip, _ = create_trip(self.alice, name="Mill run")
        Trip.objects.filter(pk=self.trip.pk).update(allow_add_members=Trip.PERM_EVERYONE)
        self.trip.refresh_from_db()
        TripMembership.objects.create(trip=self.trip, profile=self.carol, status=TripMembership.STATUS_JOINED)
        block_profile(self.alice, self.bob)

    def test_a_member_can_add_someone_the_creator_blocked(self) -> None:
        membership, created = add_member_by_username(self.trip, self.carol, self.bob.user.username)

        self.assertTrue(created)
        self.assertEqual(membership.profile_id, self.bob.pk)


class _BlockedPairOnATrip(TestCase):
    """Alice's trip holds Bob and Carol. Bob posts, then Alice blocks Bob, then everyone posts again."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.alice = _profile("alice")
        self.bob = _profile("bob")
        self.carol = _profile("carol")
        self.trip, _ = create_trip(self.alice, name="Mill run")
        Trip.objects.filter(pk=self.trip.pk).update(allow_add_members=Trip.PERM_EVERYONE)
        self.trip.refresh_from_db()
        for member in (self.bob, self.carol):
            TripMembership.objects.create(trip=self.trip, profile=member, status=TripMembership.STATUS_JOINED)

        self.bob_before = add_comment(self.trip, self.bob, text="bob before the block")
        _age(TripComment, self.bob_before.pk, minutes=30)
        self.bob_early_stop = create_activity(self.trip, self.bob, title="bob early stop")
        _age(TripActivity, self.bob_early_stop.pk, minutes=30)

        block_profile(self.alice, self.bob)

        self.bob_after = add_comment(self.trip, self.bob, text="bob after the block")
        self.carol_after = add_comment(self.trip, self.carol, text="carol after the block")
        self.bob_late_stop = create_activity(self.trip, self.bob, title="bob late stop")


class RosterTests(_BlockedPairOnATrip):
    def test_neither_is_in_the_others_roster(self) -> None:
        self.assertEqual({m.profile_id for m in list_members(self.trip, self.alice)}, {self.alice.pk, self.carol.pk})
        self.assertEqual({m.profile_id for m in list_members(self.trip, self.bob)}, {self.bob.pk, self.carol.pk})

    def test_everyone_else_sees_both(self) -> None:
        self.assertEqual(
            {m.profile_id for m in list_members(self.trip, self.carol)}, {self.alice.pk, self.bob.pk, self.carol.pk}
        )

    def test_the_members_panel_leaves_the_other_out(self) -> None:
        self.client.force_login(self.alice.user)

        response = self.client.get(reverse("trips.members", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="trip-member-item"', count=2)
        self.assertNotContains(response, self.bob.user.username)

    def test_the_trip_list_leaves_the_other_out(self) -> None:
        self.client.force_login(self.alice.user)

        response = self.client.get(reverse("trips.list"), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2 members")
        self.assertNotContains(response, self.bob.user.username)

    def test_the_api_roster_and_detail_leave_the_other_out(self) -> None:
        auth = _token(self.alice.user)

        members = self.client.get(reverse("external_api:trips.members", kwargs={"trip_slug": self.trip.slug}), **auth)
        detail = self.client.get(reverse("external_api:trips.detail", kwargs={"trip_slug": self.trip.slug}), **auth)

        self.assertEqual(members.status_code, 200, members.content[:300])
        self.assertEqual(len(members.json()["results"]), 2)
        self.assertNotIn(self.bob.user.username, members.content.decode())
        self.assertEqual(len(detail.json()["members"]), 2)
        self.assertNotIn(self.bob.user.username, detail.content.decode())

    def test_neither_is_named_as_the_creator_of_a_trip_the_other_views(self) -> None:
        """Bob's own trip, which Alice is on, names Bob to Alice no more than Alice's names Alice to Bob."""
        bobs_trip, _ = create_trip(self.bob, name="Bob's run")
        TripMembership.objects.create(trip=bobs_trip, profile=self.alice, status=TripMembership.STATUS_JOINED)

        def creator(viewer: Profile, trip: Trip) -> dict | None:
            url = reverse("external_api:trips.detail", kwargs={"trip_slug": trip.slug})
            response = self.client.get(url, **_token(viewer.user))
            self.assertEqual(response.status_code, 200, response.content[:300])
            return response.json()["creator"]

        self.assertIsNone(creator(self.alice, bobs_trip))
        self.assertIsNone(creator(self.bob, self.trip))
        self.assertEqual((creator(self.carol, self.trip) or {}).get("slug"), self.alice.slug)

    def test_the_data_export_roster_leaves_the_other_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _export_trips(self.alice, temp_dir)
            with open(os.path.join(temp_dir, "trips.json"), encoding="utf-8") as fh:
                [row] = json.load(fh)

        self.assertEqual(len(row["member_uuids"]), 2)
        self.assertNotIn(str(self.bob.uuid), row["member_uuids"])
        self.assertNotIn(self.bob.user.username, row["members"])

    def test_the_api_trip_list_counts_leave_the_hidden_out(self) -> None:
        rows = self.client.get(reverse("external_api:trips"), **_token(self.alice.user)).json()["results"]
        carols = self.client.get(reverse("external_api:trips"), **_token(self.carol.user)).json()["results"]

        self.assertEqual(rows[0]["member_count"], 2)
        self.assertEqual(rows[0]["comment_count"], 2)
        self.assertEqual(carols[0]["member_count"], 3)
        self.assertEqual(carols[0]["comment_count"], 3)


class CommentTests(_BlockedPairOnATrip):
    def _texts(self, viewer: Profile) -> set[str]:
        texts: set[str] = set()
        for row in build_comment_tree(self.trip, viewer):
            texts.add(row["comment"].text)
            texts.update(reply["comment"].text for reply in row["replies"])
        return texts

    def test_the_blocker_does_not_see_what_the_blocked_person_posted_afterwards(self) -> None:
        self.assertNotIn(
            self.bob_after.pk, set(visible_comment_queryset(self.trip, self.alice).values_list("pk", flat=True))
        )
        self.assertNotIn("bob after the block", self._texts(self.alice))
        self.assertFalse(trip_comment_is_visible(self.bob_after, self.alice))

    def test_what_was_posted_before_the_block_stays(self) -> None:
        self.assertIn("bob before the block", self._texts(self.alice))

    def test_the_count_matches_what_is_shown(self) -> None:
        self.assertEqual(visible_comment_count(self.trip, self.alice), 2)
        self.assertEqual(visible_comment_count(self.trip, self.carol), 3)

    def test_the_blocked_persons_count_matches_what_is_shown(self) -> None:
        alice_before = add_comment(self.trip, self.alice, text="alice before the block")
        _age(TripComment, alice_before.pk, minutes=40)

        self.assertEqual(visible_comment_count(self.trip, self.bob), len(self._texts(self.bob)))

    def test_the_blocked_person_does_not_see_the_blocker_either(self) -> None:
        add_comment(self.trip, self.alice, text="alice after the block")

        self.assertNotIn("alice after the block", self._texts(self.bob))
        self.assertIn("alice after the block", self._texts(self.carol))

    def test_a_hidden_reply_under_a_visible_comment_is_left_out(self) -> None:
        add_comment(self.trip, self.bob, text="bob replies to carol", parent_id=self.carol_after.pk)

        self.assertNotIn("bob replies to carol", self._texts(self.alice))
        self.assertIn("bob replies to carol", self._texts(self.carol))

    def test_the_panel_and_the_api_leave_it_out(self) -> None:
        self.client.force_login(self.alice.user)
        panel = self.client.get(reverse("trips.comments", kwargs={"trip_slug": self.trip.slug}))
        api = self.client.get(
            reverse("external_api:trips.comments", kwargs={"trip_slug": self.trip.slug}), **_token(self.alice.user)
        )

        for response in (panel, api):
            self.assertEqual(response.status_code, 200, response.content[:300])
            self.assertNotIn("bob after the block", response.content.decode())
            self.assertIn("carol after the block", response.content.decode())

    def test_nobody_can_reply_to_a_comment_they_cannot_see(self) -> None:
        with self.assertRaises(TripNotFoundError):
            add_comment(self.trip, self.alice, text="alice replies", parent_id=self.bob_after.pk)

    def test_a_reply_across_the_block_does_not_notify(self) -> None:
        NotificationLog.objects.all().delete()

        add_comment(self.trip, self.alice, text="alice replies to bob's old comment", parent_id=self.bob_before.pk)
        add_comment(self.trip, self.bob, text="bob replies to carol", parent_id=self.carol_after.pk)

        self.assertFalse(
            NotificationLog.objects.filter(profile=self.bob, notification_type=NotificationType.COMMENT_REPLY).exists()
        )
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.carol, notification_type=NotificationType.COMMENT_REPLY
            ).exists()
        )

    def test_nobody_can_react_to_a_comment_they_cannot_see(self) -> None:
        with self.assertRaises(TripNotFoundError):
            set_comment_reaction(self.bob_after, self.alice, "👍", reacted=True)

    def test_a_reaction_across_the_block_does_not_notify(self) -> None:
        NotificationLog.objects.all().delete()

        set_comment_reaction(self.bob_before, self.alice, "👍", reacted=True)

        self.assertFalse(
            NotificationLog.objects.filter(profile=self.bob, notification_type=NotificationType.COMMENT_LIKED).exists()
        )

    def test_a_reaction_made_after_the_block_is_left_out_of_the_others_tally(self) -> None:
        set_comment_reaction(self.carol_after, self.bob, "👍", reacted=True)

        alices = {row["comment"].pk: row["reactions"] for row in build_comment_tree(self.trip, self.alice)}
        carols = {row["comment"].pk: row["reactions"] for row in build_comment_tree(self.trip, self.carol)}

        self.assertEqual(alices[self.carol_after.pk], {})
        self.assertEqual(carols[self.carol_after.pk]["👍"]["count"], 1)

    def test_the_web_reaction_row_leaves_the_others_reaction_out(self) -> None:
        Reaction.objects.create(profile=self.bob, trip_comment=self.carol_after, emoji="🔥")
        self.client.force_login(self.alice.user)

        response = self.client.post(
            reverse("trips.comment.react", kwargs={"trip_slug": self.trip.slug, "comment_id": self.carol_after.pk}),
            {"emoji": "👍"},
        )

        self.assertEqual(response.status_code, 200)
        # One pill (Alice's own), where Bob's would have made two; the picker lists every emoji regardless.
        self.assertContains(response, 'class="reaction-count"', count=1)

    def test_search_does_not_return_it(self) -> None:
        results = CommentSearchProvider().search(self.alice, parse_query("block"), 20)

        snippets = " ".join(result.snippet or "" for result in results)
        self.assertNotIn("bob after", snippets)
        self.assertIn("carol after", snippets)

    def test_trip_search_does_not_match_on_it(self) -> None:
        """Matching the trip on a word only the hidden comment holds would tell the other what it says."""
        add_comment(self.trip, self.bob, text="bob mentions a quokka")
        add_comment(self.trip, self.carol, text="carol mentions a wombat")

        self.assertEqual(TripSearchProvider().search(self.alice, parse_query("quokka"), 10), [])
        self.assertEqual(len(TripSearchProvider().search(self.carol, parse_query("quokka"), 10)), 1)
        self.assertEqual(len(TripSearchProvider().search(self.alice, parse_query("wombat"), 10)), 1)

    def test_its_image_is_not_served_to_the_other(self) -> None:
        TripComment.objects.filter(pk=self.bob_after.pk).update(image="comment_images/bob-after.jpg")
        TripComment.objects.filter(pk=self.carol_after.pk).update(image="comment_images/carol-after.jpg")

        self.assertFalse(authorize_comment_image(self.alice, "comment_images/bob-after.jpg"))
        self.assertTrue(authorize_comment_image(self.carol, "comment_images/bob-after.jpg"))
        self.assertTrue(authorize_comment_image(self.alice, "comment_images/carol-after.jpg"))


class ActivityTests(_BlockedPairOnATrip):
    def _rows(self, viewer: Profile) -> dict[int, dict]:
        return {row["activity"].pk: row for row in build_activity_rows(self.trip, viewer, include_legs=False)}

    def test_the_itinerary_itself_is_unchanged(self) -> None:
        self.assertEqual(set(self._rows(self.alice)), set(self._rows(self.carol)))
        self.assertIn(self.bob_late_stop.pk, self._rows(self.alice))

    def test_who_added_a_stop_after_the_block_is_not_shown(self) -> None:
        rows = self._rows(self.alice)

        self.assertIsNone(rows[self.bob_late_stop.pk]["added_by"])
        self.assertEqual(rows[self.bob_early_stop.pk]["added_by"].pk, self.bob.pk)
        self.assertEqual(self._rows(self.carol)[self.bob_late_stop.pk]["added_by"].pk, self.bob.pk)

    def test_the_api_and_panel_leave_the_attribution_out(self) -> None:
        api = self.client.get(
            reverse("external_api:trips.activities", kwargs={"trip_slug": self.trip.slug}), **_token(self.alice.user)
        )
        by_id = {row["id"]: row for row in api.json()["results"]}
        self.client.force_login(self.alice.user)
        panel = self.client.get(reverse("trips.activities", kwargs={"trip_slug": self.trip.slug}))

        self.assertIsNone(by_id[self.bob_late_stop.pk]["added_by"])
        self.assertIsNotNone(by_id[self.bob_early_stop.pk]["added_by"])
        self.assertEqual(panel.status_code, 200)
        self.assertContains(panel, "Added by", count=1)  # Bob's early stop only.
