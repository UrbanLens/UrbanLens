"""P280: a profile a request body names costs the same, and answers the same, whether it is hidden or missing.

P269 made every route whose URL names a profile ask who may see it in the query that finds it. A slug, username or id
sent in a form or JSON body was still found first and refused afterwards, so a timed request, or in places the answer
itself, told the sender that the account exists. An id is a sequential integer, so walking ids told the sender which
accounts are hidden from it, and in the group and game routes which of them exist at all.
"""

from __future__ import annotations

from datetime import timedelta
import os
import re

from django.contrib.auth.models import User
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.oauth import first_party_application
from urbanlens.dashboard.models.consensus.model import (
    ConsensusSession,
    ConsensusSessionParticipant,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.spotguessr.model import (
    GameSession,
    GameSessionParticipant,
    GameSessionParticipantStatus,
    GameSessionStatus,
)
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.trivia.model import (
    TriviaSession,
    TriviaSessionParticipant,
    TriviaSessionParticipantStatus,
    TriviaSessionStatus,
)
from urbanlens.dashboard.services.auth.username import normalize_username_key
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat
from urbanlens.dashboard.services.trips.trip_crud import create_trip
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import disable_throttling
from urbanlens.dashboard.tests.hypothesis.test_username_enumeration_profiles import (
    _ALL_SCOPES,
    _CSRF_TOKEN_RE,
    NOBODY,
    _HiddenProfilesTestCase,
)

_SAVEPOINT = re.compile(r'"?s\d+_x\d+"?')
_TIMESTAMP = re.compile(r"'\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-]\d{2}:?\d{2})?'(?:::timestamptz)?")
_WARM_UP = "warm_up_name"
_INTEGER = re.compile(r"\b\d+\b")


def _same_form(text: str, probe: str) -> str:
    for form in sorted({probe, probe.replace("_", "-"), normalize_username_key(probe)} - {""}, key=len, reverse=True):
        text = text.replace(form, "<probe>")
    return text


def _shape(queries: list[dict], probe: str) -> list[str]:
    """The statements a request ran, with the probe, savepoint names and clock readings made comparable."""
    shapes = []
    for query in queries:
        if "dashboard_api_key_usage_log" in query["sql"]:
            continue
        shapes.append(_TIMESTAMP.sub("<now>", _SAVEPOINT.sub("<savepoint>", _same_form(query["sql"], probe))))
    return shapes


class _BodySideChannelCase(_HiddenProfilesTestCase):
    """The requester, ``the_viewer``, and three accounts it may neither see nor message."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.viewer.user)
        self.usernames = {case: Profile.objects.get(slug=slug).user.username for case, slug in self.hidden.items()}

    def _measure(self, send, probe: str) -> tuple[int, str, list[str]]:
        reset_queries()
        with CaptureQueriesContext(connection) as queries:
            status, body = send(probe)
        return status, _same_form(_CSRF_TOKEN_RE.sub("CSRF", body), probe), _shape(queries.captured_queries, probe)

    def assert_indistinguishable(self, send, *, by: str = "slug") -> None:
        """``send(probe)`` answers and costs the same for each hidden account as for a name nobody holds."""
        if by == "pk":
            self.assert_id_indistinguishable(send)
            return
        probes = self.usernames if by == "username" else self.hidden
        self._measure(send, _WARM_UP)
        expected = self._measure(send, NOBODY)
        for case, probe in probes.items():
            with self.subTest(case=case):
                self.assertEqual(self._measure(send, probe), expected)

    def _measure_id(self, send, probe: int) -> tuple[int, str, list[str]]:
        """As :meth:`_measure`, with every integer in the SQL made alike, since a pk is no distinctive string."""
        reset_queries()
        with CaptureQueriesContext(connection) as queries:
            status, body = send(str(probe))
        body = re.sub(rf"\b{probe}\b", "<probe>", _CSRF_TOKEN_RE.sub("CSRF", body))
        shapes = [_INTEGER.sub("<n>", shape) for shape in _shape(queries.captured_queries, "")]
        return status, body, shapes

    def assert_id_indistinguishable(self, send) -> None:
        """``send(pk)`` answers and costs the same for each hidden account's id as for an id nobody holds."""
        never_used = Profile.objects.order_by("-pk").values_list("pk", flat=True).first() + 10_000
        self._measure_id(send, never_used + 1)
        expected = self._measure_id(send, never_used)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(self._measure_id(send, Profile.objects.get(slug=slug).pk), expected)

    def _web(self, url: str, payload: dict) -> tuple[int, str]:
        """The status, and the body with the toast an HTMX answer carries in its header."""
        response = self.client.post(url, payload)
        return response.status_code, response.get("HX-Trigger", "") + response.content.decode()


class WebBodiesTests(_BodySideChannelCase):
    def test_creating_a_group(self) -> None:
        url = reverse("messages.group.create")
        self.assert_indistinguishable(lambda probe: self._web(url, {"name": "g", "member_slugs": [probe]}))

    def test_creating_a_group_with_someone_already_in_as_many_groups_as_they_may_join(self) -> None:
        """A hidden account at its group limit answered "already in as many groups", where no one answered 403."""
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(max_group_chats_per_user=1)
        for slug in self.hidden.values():
            group = baker.make(GroupChat, creator=self.visible)
            GroupChatMembership.objects.create(group=group, profile=Profile.objects.get(slug=slug))
        url = reverse("messages.group.create")

        self.assert_indistinguishable(lambda probe: self._web(url, {"name": "g", "member_slugs": [probe]}))

    def test_adding_a_group_member(self) -> None:
        group = create_group_chat(self.viewer, "g", [self.visible])
        url = reverse("messages.group.members.add", kwargs={"group_uuid": group.uuid})

        self.assert_indistinguishable(lambda probe: self._web(url, {"member_slugs": [probe]}))

    def test_sharing_a_vault_photo_with_a_friend(self) -> None:
        image = baker.make(Image, profile=self.viewer, pin=None, wiki=None)
        url = reverse("vault.photos.action", args=[image.pk, "share"])

        self.assert_indistinguishable(lambda probe: self._web(url, {"friend_slug": probe}))

    def test_recommending_a_friend_in_a_conversation(self) -> None:
        url = reverse("messages.share.friend", kwargs={"profile_slug": self.visible.slug})

        self.assert_indistinguishable(lambda probe: self._web(url, {"recommended_slug": probe}))

    def test_inviting_a_safety_partner(self) -> None:
        checkin = baker.make(
            "dashboard.SafetyCheckin",
            profile=self.viewer,
            title="Hike",
            checkin_by=timezone.now() + timedelta(hours=2),
            grace_period=timedelta(hours=1),
        )
        url = reverse("safety.checkin.partners", kwargs={"checkin_slug": checkin.slug})

        self.assert_indistinguishable(lambda probe: self._web(url, {"username": probe}), by="username")

    def test_inviting_a_trip_member(self) -> None:
        trip, _ = create_trip(self.viewer, name="Mill run")
        url = reverse("trips.members", kwargs={"trip_slug": trip.slug})

        self.assert_indistinguishable(lambda probe: self._web(url, {"username": probe}), by="username")


class WebBodiesStillWorkTests(_BodySideChannelCase):
    """Anti-vacuity: each request still reaches an account it may."""

    def test_a_group_takes_a_member_who_accepts_messages(self) -> None:
        response = self.client.post(
            reverse("messages.group.create"), {"name": "g", "member_slugs": [self.visible.slug]}
        )

        self.assertEqual(response.status_code, 201, response.content)

    def test_a_trip_takes_a_member_by_username(self) -> None:
        trip, _ = create_trip(self.viewer, name="Mill run")

        self.client.post(reverse("trips.members", kwargs={"trip_slug": trip.slug}), {"username": "open_book"})

        self.assertTrue(Trip.objects.filter(pk=trip.pk, memberships__profile=self.visible).exists())

    def test_a_friend_can_be_recommended(self) -> None:
        friend = baker.make(User, username="old_pal").profile
        Friendship.objects.create(from_profile=self.viewer, to_profile=friend, status=FriendshipStatus.ACCEPTED)
        url = reverse("messages.share.friend", kwargs={"profile_slug": self.visible.slug})

        response = self.client.post(url, {"recommended_slug": friend.slug})

        self.assertEqual(response.status_code, 200, response.content)


class ExternalApiBodiesTests(_BodySideChannelCase):
    def setUp(self) -> None:
        super().setUp()
        token = get_access_token_model().objects.create(
            user=self.viewer.user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=_ALL_SCOPES,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}
        disable_throttling(self)

    def _api(self, method: str, url: str, payload: dict) -> tuple[int, str]:
        response = getattr(self.client, method)(url, payload, content_type="application/json", **self.auth)
        return response.status_code, response.content.decode()

    def test_creating_a_group(self) -> None:
        url = reverse("external_api:messages.groups")
        self.assert_indistinguishable(lambda probe: self._api("post", url, {"name": "g", "member_slugs": [probe]}))

    def test_adding_a_group_member(self) -> None:
        group = create_group_chat(self.viewer, "g", [self.visible])
        url = reverse("external_api:messages.groups.members", kwargs={"group_uuid": group.uuid})

        self.assert_indistinguishable(lambda probe: self._api("post", url, {"member_slugs": [probe]}))

    def test_removing_a_group_member(self) -> None:
        """A name nobody held answered "Unknown profile slug(s)", any other non-member "They aren't a member"."""
        group = create_group_chat(self.viewer, "g", [self.visible])
        url = reverse("external_api:messages.groups.members", kwargs={"group_uuid": group.uuid})

        self.assert_indistinguishable(lambda probe: self._api("delete", url, {"member_slugs": [probe]}))

    def test_recommending_a_friend_in_a_message(self) -> None:
        url = reverse("external_api:messages.thread", kwargs={"peer_slug": self.visible.slug})

        self.assert_indistinguishable(
            lambda probe: self._api("post", url, {"body": "hi", "shared_profile_slug": probe})
        )

    def test_inviting_a_trip_member(self) -> None:
        trip, _ = create_trip(self.viewer, name="Mill run")
        url = reverse("external_api:trips.members", kwargs={"trip_slug": trip.slug})

        self.assert_indistinguishable(lambda probe: self._api("post", url, {"username": probe}), by="username")

    def test_inviting_a_safety_partner(self) -> None:
        checkin = baker.make(
            "dashboard.SafetyCheckin",
            profile=self.viewer,
            title="Hike",
            checkin_by=timezone.now() + timedelta(hours=2),
            grace_period=timedelta(hours=1),
        )
        url = reverse("external_api:safety.checkins.partners", kwargs={"checkin_slug": checkin.slug})

        self.assert_indistinguishable(lambda probe: self._api("post", url, {"username": probe}), by="username")


class WebIdBodiesTests(_BodySideChannelCase):
    """Routes whose body names a profile by id. None of the hidden accounts is a friend, member or player here."""

    def setUp(self) -> None:
        super().setUp()
        grant_alpha_features(self.viewer.user)

    def test_sharing_a_pin(self) -> None:
        pin = baker.make(Pin, profile=self.viewer, location=baker.make(Location, latitude="40.1", longitude="-75.1"))
        url = reverse("pin.share.send", kwargs={"pin_slug": pin.slug})

        self.assert_indistinguishable(lambda probe: self._web(url, {"profile_id": probe}), by="pk")

    def test_sharing_a_markup_map(self) -> None:
        markup_map = baker.make(MarkupMap, profile=self.viewer)
        url = reverse("markup_map.share.send", kwargs={"map_uuid": markup_map.uuid})

        self.assert_indistinguishable(lambda probe: self._web(url, {"profile_id": probe}), by="pk")

    def test_removing_a_group_member(self) -> None:
        """An id nobody held answered 404, any other non-member "They aren't a member of this group"."""
        group = create_group_chat(self.viewer, "g", [self.visible])
        url = reverse("messages.group.members.remove", kwargs={"group_uuid": group.uuid})

        self.assert_indistinguishable(lambda probe: self._web(url, {"profile_id": probe}), by="pk")

    def test_kicking_from_a_spotguessr_session(self) -> None:
        """An id nobody held answered "profile_id is required", any other account "not part of this session"."""
        session = baker.make(GameSession, host_profile=self.viewer, status=GameSessionStatus.LOBBY)
        GameSessionParticipant.objects.create(
            session=session, profile=self.viewer, status=GameSessionParticipantStatus.JOINED
        )
        url = reverse("spotguessr.kick", kwargs={"session_id": session.pk})

        self.assert_indistinguishable(lambda probe: self._web(url, {"profile_id": probe}), by="pk")

    def test_kicking_from_a_trivia_session(self) -> None:
        session = baker.make(TriviaSession, host_profile=self.viewer, status=TriviaSessionStatus.LOBBY)
        TriviaSessionParticipant.objects.create(
            session=session, profile=self.viewer, status=TriviaSessionParticipantStatus.JOINED
        )
        url = reverse("trivia.kick", kwargs={"session_id": session.pk})

        self.assert_indistinguishable(lambda probe: self._web(url, {"profile_id": probe}), by="pk")

    def test_inviting_to_a_game_session(self) -> None:
        sessions = {
            "spotguessr.invite": (GameSession, GameSessionParticipant, GameSessionStatus.LOBBY),
            "trivia.invite": (TriviaSession, TriviaSessionParticipant, TriviaSessionStatus.LOBBY),
            "consensus.invite": (ConsensusSession, ConsensusSessionParticipant, ConsensusSessionStatus.LOBBY),
        }
        for name, (session_model, participant_model, lobby) in sessions.items():
            session = baker.make(session_model, host_profile=self.viewer, status=lobby)
            participant_model.objects.create(session=session, profile=self.viewer, status="joined")
            url = reverse(name, kwargs={"session_id": session.pk})
            with self.subTest(route=name):
                self.assert_indistinguishable(lambda probe, url=url: self._web(url, {"profile_id": probe}), by="pk")

    def test_a_player_who_is_not_the_host_learns_nothing_of_the_host_s_friends(self) -> None:
        """Resolving the invitee among the host's friends must not run ahead of the host check."""
        friend = baker.make(User, username="hosts_pal").profile
        host = baker.make(User, username="the_host").profile
        Friendship.objects.create(from_profile=host, to_profile=friend, status=FriendshipStatus.ACCEPTED)
        session = baker.make(GameSession, host_profile=host, status=GameSessionStatus.LOBBY)
        for profile in (host, self.viewer):
            GameSessionParticipant.objects.create(
                session=session, profile=profile, status=GameSessionParticipantStatus.JOINED
            )
        url = reverse("spotguessr.invite", kwargs={"session_id": session.pk})

        self.assertEqual(self._web(url, {"profile_id": friend.pk}), self._web(url, {"profile_id": self.visible.pk}))
