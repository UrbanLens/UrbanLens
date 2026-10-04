"""P269: a profile-addressed URL must cost the same for an account the requester cannot see as for no account at all.

Usernames are guessable and profile slugs come from them, so a hidden account that answered 404 after more queries
than a missing one told anyone timing the two that the username is registered.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
import re
from types import SimpleNamespace
from unittest import mock
import uuid

from django.contrib.auth.models import User
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import DirectMessageConsumer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.direct_messages.temporary_access import DirectMessageTemporaryAccess
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.messaging.direct_messages import (
    broadcast_typing_indicator,
    conversation_reachable,
    is_thread_open,
)

NEVER_USED = "nobody-ever-took-this-name"

_SAVEPOINT = re.compile(r'"?s\d+_x\d+"?')
_TIMESTAMP = re.compile(r"'\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:[+-]\d{2}:?\d{2})?'(?:::timestamptz)?")
_EXTRA_ARGS = {"int": 1, "str": "x", "slug": "x", "uuid": "00000000-0000-4000-8000-000000000000"}
_API_NAMESPACES = {"external_api"}


def _shape(queries: list[dict], probe: str) -> list[str]:
    """The statements a request ran, with the probed slug or uuid, savepoint names and clock readings made comparable.

    An API key's usage log is pruned by row id now and then, whatever the request named, so it is left out.
    """
    shapes = []
    for query in queries:
        if "dashboard_api_key_usage_log" in query["sql"]:
            continue
        sql = query["sql"].replace(probe, "<probe>").replace(probe.replace("-", ""), "<probe>")
        shapes.append(_TIMESTAMP.sub("<now>", _SAVEPOINT.sub("<savepoint>", sql)))
    return shapes


def _profile_slug_routes() -> list[tuple[str, str, dict[str, object]]]:
    """Every named route addressed by a profile's slug, namespaced where it is: its name, the slug's argument, and
    stand-ins for its other arguments."""
    found: dict[str, tuple[str, dict[str, object]]] = {}

    def walk(patterns, prefix: str, namespace: str) -> None:
        for entry in patterns:
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, route, f"{namespace}{entry.namespace}:" if entry.namespace else namespace)
            elif (
                isinstance(entry, URLPattern)
                and entry.name
                and (match := re.search(r"<\w+:(profile_slug|peer_slug)>", route))
            ):
                argument = match.group(1)
                extra = {
                    name: _EXTRA_ARGS[kind] for kind, name in re.findall(r"<(\w+):(\w+)>", route) if name != argument
                }
                found[namespace + entry.name] = (argument, extra)

    walk(get_resolver().url_patterns, "", "")
    return sorted((name, argument, extra) for name, (argument, extra) in found.items())


def _is_api(route_name: str) -> bool:
    return route_name.split(":", maxsplit=1)[0] in _API_NAMESPACES


def _make_profile(username: str, **fields: object) -> Profile:
    user = baker.make(User, username=username)
    Profile.objects.filter(user=user).update(**fields)
    return Profile.objects.select_related("user").get(user=user)


class _HiddenAccountFixture(TestCase):
    """Three accounts the requester may not see, by setting, by their block, and by deactivation."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User, username="requester")
        self.profile = self.user.profile
        hidden = {"profile_visibility": VisibilityChoice.NO_ONE, "direct_message_visibility": VisibilityChoice.NO_ONE}
        self.by_setting = _make_profile("quiet-explorer", **hidden)
        self.by_block = _make_profile("blocking-explorer")
        Friendship.objects.create(from_profile=self.by_block, to_profile=self.profile, status=FriendshipStatus.BLOCKED)
        self.deactivated = _make_profile("departed-explorer")
        User.objects.filter(pk=self.deactivated.user_id).update(is_active=False)
        self.client.force_login(self.user)

    def _probe(self, method: str, url_for, probe: str, **extra) -> tuple[int, list[str]]:
        url = url_for(probe)
        reset_queries()
        with CaptureQueriesContext(connection) as queries:
            response = getattr(self.client, method)(url, **extra)
        return response.status_code, _shape(queries.captured_queries, probe)

    def _assert_indistinguishable(self, method: str, url_for, **extra) -> None:
        self._probe(method, url_for, "warm-up-name", **extra)
        missing = self._probe(method, url_for, NEVER_USED, **extra)
        for label, hidden in (
            ("setting", self.by_setting),
            ("block", self.by_block),
            ("deactivated", self.deactivated),
        ):
            with self.subTest(hidden_by=label):
                self.assertEqual(self._probe(method, url_for, hidden.slug, **extra), missing)


class WebRouteSideChannelTests(_HiddenAccountFixture):
    def _web_routes(self) -> list[tuple[str, str, dict[str, object]]]:
        return [route for route in _profile_slug_routes() if not _is_api(route[0])]

    def test_the_route_list_is_not_empty(self) -> None:
        """Guards the sweep below against passing over nothing."""
        names = [name for name, _argument, _extra in self._web_routes()]

        self.assertIn("profile.view_user", names)
        self.assertIn("messages.conversation", names)
        self.assertIn("e2ee.partner_key", names)
        self.assertGreater(len(names), 25)

    def test_every_profile_route_costs_the_same_for_a_hidden_account_as_for_none(self) -> None:
        for name, argument, extra in self._web_routes():
            for method in ("get", "post"):
                with self.subTest(route=name, method=method):
                    self._assert_indistinguishable(
                        method,
                        lambda probe, name=name, argument=argument, extra=extra: reverse(
                            name, kwargs={argument: probe, **extra}
                        ),
                    )

    def test_a_hidden_account_s_profile_is_still_a_404(self) -> None:
        for hidden in (self.by_setting, self.by_block, self.deactivated):
            with self.subTest(username=hidden.username):
                self.assertEqual(self.client.get(reverse("profile.view_user", args=[hidden.slug])).status_code, 404)

    def test_a_visible_account_s_profile_still_opens(self) -> None:
        visible = _make_profile("open-explorer", profile_visibility=VisibilityChoice.ANYONE)

        self.assertEqual(self.client.get(reverse("profile.view_user", args=[visible.slug])).status_code, 200)


class ExternalApiProfileSideChannelTests(_HiddenAccountFixture):
    def setUp(self) -> None:
        super().setUp()
        _key, raw_key = generate_api_key(self.user, "Side channel client")
        scopes = [scope.value for scope in ApiKeyScope]
        ApiKey.objects.filter(user=self.user).update(scopes=scopes)
        self.client.logout()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}

    def _api_routes(self) -> list[tuple[str, str, dict[str, object]]]:
        return [route for route in _profile_slug_routes() if _is_api(route[0])]

    def test_the_route_list_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(self._api_routes()), 8)

    def test_every_profile_route_costs_the_same_for_a_hidden_account_as_for_none(self) -> None:
        for name, argument, extra in self._api_routes():
            for method in ("get", "post", "patch"):
                with self.subTest(route=name, method=method):
                    self._assert_indistinguishable(
                        method,
                        lambda probe, name=name, argument=argument, extra=extra: reverse(
                            name, kwargs={argument: probe, **extra}
                        ),
                        **self.auth,
                    )

    def test_a_hidden_account_s_uuid_costs_the_same_as_an_unknown_uuid(self) -> None:
        def url_for(probe: str) -> str:
            return reverse(self._detail_route(), kwargs={"profile_slug": probe})

        self._probe("get", url_for, str(uuid.uuid4()), **self.auth)
        unknown = self._probe("get", url_for, str(uuid.uuid4()), **self.auth)
        self.assertEqual(self._probe("get", url_for, str(self.by_setting.uuid), **self.auth)[0], unknown[0])
        self.assertEqual(
            self._probe("get", url_for, str(self.by_setting.uuid), **self.auth)[1],
            self._probe("get", url_for, str(uuid.uuid4()), **self.auth)[1],
        )

    def _detail_route(self) -> str:
        [name] = [name for name, _argument, _extra in self._api_routes() if name.endswith("profiles.detail")]
        return name


class DirectMessageSocketSideChannelTests(_HiddenAccountFixture):
    """The direct-message socket names its partner by slug too, so each frame must cost the same for a hidden account."""

    def setUp(self) -> None:
        super().setUp()
        self.socket = SimpleNamespace(profile_id=self.profile.pk)
        self.friend = _make_profile("friendly-explorer")
        Friendship.objects.create(from_profile=self.profile, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)

    def _send(self, slug: str) -> None:
        DirectMessageConsumer.__dict__["_create_message"].func(self.socket, slug, "hi", "", "", 0, [], None, None)

    def _open(self, slug: str) -> None:
        DirectMessageConsumer.__dict__["_mark_thread_open"].func(self.socket, slug)

    def _type(self, slug: str) -> None:
        broadcast_typing_indicator(self.profile.pk, slug)

    def _frame_shape(self, frame, probe: str) -> list[str]:
        reset_queries()
        with CaptureQueriesContext(connection) as queries, contextlib.suppress(ValueError, PermissionError):
            frame(probe)
        return _shape(queries.captured_queries, probe)

    def _assert_frame_indistinguishable(self, frame) -> None:
        self._frame_shape(frame, "warm-up-name")
        missing = self._frame_shape(frame, NEVER_USED)
        for label, hidden in (
            ("setting", self.by_setting),
            ("block", self.by_block),
            ("deactivated", self.deactivated),
        ):
            with self.subTest(hidden_by=label):
                self.assertEqual(self._frame_shape(frame, hidden.slug), missing)

    def test_sending_to_a_hidden_account_costs_the_same_as_sending_to_none(self) -> None:
        self._assert_frame_indistinguishable(self._send)

    def test_typing_to_a_hidden_account_costs_the_same_as_typing_to_none(self) -> None:
        with mock.patch("urbanlens.dashboard.services.messaging.direct_messages.send_group_message"):
            self._assert_frame_indistinguishable(self._type)

    def test_opening_a_hidden_account_s_thread_costs_the_same_as_opening_none(self) -> None:
        self._assert_frame_indistinguishable(self._open)

    def test_a_hidden_account_s_thread_is_never_marked_open(self) -> None:
        for hidden in (self.by_setting, self.by_block, self.deactivated):
            self._open(hidden.slug)
            self.assertFalse(is_thread_open(self.profile.pk, hidden.pk))

    def test_a_friend_s_thread_is_marked_open(self) -> None:
        self._open(self.friend.slug)

        self.assertTrue(is_thread_open(self.profile.pk, self.friend.pk))

    def test_a_friend_sees_the_typing_indicator(self) -> None:
        with mock.patch("urbanlens.dashboard.services.messaging.direct_messages.send_group_message") as relay:
            self._type(self.friend.slug)

        relay.assert_called_once()

    def test_a_friend_receives_a_message(self) -> None:
        with mock.patch("urbanlens.dashboard.services.messaging.direct_messages.send_group_message"):
            self._send(self.friend.slug)

        self.assertTrue(DirectMessage.objects.filter(sender=self.profile, recipient=self.friend).exists())


class _RelationshipGrid(TestCase):
    """Pairs of accounts in every relationship the visibility rules distinguish."""

    RELATIONSHIPS = (
        "none",
        "friends",
        "request_from_subject",
        "request_from_viewer",
        "common_pin",
        "common_friend",
        "common_trip",
        "subject_blocked_viewer",
        "viewer_blocked_subject",
        "temporary_access",
        "temporary_access_expired",
        "subject_inactive",
        "subject_messaged_viewer",
        "viewer_messaged_subject",
        "subject_community_off",
        "viewer_community_off",
    )

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self._serial = 0

    def _pair(self, relationship: str) -> tuple[Profile, Profile]:
        """A viewer and a subject in *relationship*; the caller sets the subject's visibility setting."""
        self._serial += 1
        viewer = _make_profile(f"viewer{self._serial}")
        subject = _make_profile(f"subject{self._serial}")
        accepted, blocked = FriendshipStatus.ACCEPTED, FriendshipStatus.BLOCKED
        if relationship == "friends":
            Friendship.objects.create(from_profile=viewer, to_profile=subject, status=accepted)
        elif relationship == "request_from_subject":
            Friendship.objects.create(from_profile=subject, to_profile=viewer, status=FriendshipStatus.REQUESTED)
        elif relationship == "request_from_viewer":
            Friendship.objects.create(from_profile=viewer, to_profile=subject, status=FriendshipStatus.REQUESTED)
        elif relationship == "common_pin":
            location = baker.make(Location, latitude=f"{40 + self._serial / 1000:.6f}", longitude="-74.000000")
            baker.make(Pin, profile=viewer, location=location)
            baker.make(Pin, profile=subject, location=location)
        elif relationship == "common_friend":
            mutual = _make_profile(f"mutual{self._serial}")
            Friendship.objects.create(from_profile=viewer, to_profile=mutual, status=accepted)
            Friendship.objects.create(from_profile=mutual, to_profile=subject, status=accepted)
        elif relationship == "common_trip":
            trip = baker.make(Trip, creator=viewer)
            TripMembership.objects.create(trip=trip, profile=viewer, status=TripMembership.STATUS_JOINED)
            TripMembership.objects.create(trip=trip, profile=subject, status=TripMembership.STATUS_JOINED)
        elif relationship == "subject_blocked_viewer":
            Friendship.objects.create(from_profile=subject, to_profile=viewer, status=blocked)
        elif relationship == "viewer_blocked_subject":
            Friendship.objects.create(from_profile=viewer, to_profile=subject, status=blocked)
        elif relationship.startswith("temporary_access"):
            expires = timezone.now() + (timedelta(days=-1) if relationship.endswith("expired") else timedelta(days=1))
            DirectMessageTemporaryAccess.objects.create(profile=subject, granted_to=viewer, expires_at=expires)
        elif relationship == "subject_inactive":
            User.objects.filter(pk=subject.user_id).update(is_active=False)
        elif relationship == "subject_messaged_viewer":
            baker.make(DirectMessage, sender=subject, recipient=viewer)
        elif relationship == "viewer_messaged_subject":
            baker.make(DirectMessage, sender=viewer, recipient=subject)
        elif relationship == "subject_community_off":
            Profile.objects.filter(pk=subject.pk).update(community_enabled=False)
        elif relationship == "viewer_community_off":
            Profile.objects.filter(pk=viewer.pk).update(community_enabled=False)
        return (
            Profile.objects.select_related("user").get(pk=viewer.pk),
            Profile.objects.select_related("user").get(pk=subject.pk),
        )


class ViewableProfileQueryTests(_RelationshipGrid):
    """The one-query lookup must answer exactly as :meth:`Profile.can_view_profile` does, in every case."""

    def test_the_query_agrees_with_can_view_profile_everywhere(self) -> None:
        for relationship in self.RELATIONSHIPS:
            viewer, subject = self._pair(relationship)
            for setting in VisibilityChoice.values:
                Profile.objects.filter(pk=subject.pk).update(profile_visibility=setting)
                subject.profile_visibility = setting
                with self.subTest(relationship=relationship, setting=setting):
                    self.assertEqual(
                        Profile.visible_by_slug(subject.slug, viewer) is not None,
                        subject.can_view_profile(viewer),
                    )

    def test_an_anonymous_viewer_sees_only_active_accounts_open_to_anyone(self) -> None:
        for active in (True, False):
            subject = _make_profile(f"anonymous-subject-{active}".lower())
            User.objects.filter(pk=subject.user_id).update(is_active=active)
            subject = Profile.objects.select_related("user").get(pk=subject.pk)
            for setting in VisibilityChoice.values:
                Profile.objects.filter(pk=subject.pk).update(profile_visibility=setting)
                subject.profile_visibility = setting
                with self.subTest(setting=setting, active=active):
                    self.assertEqual(
                        Profile.visible_by_slug(subject.slug, None) is not None, subject.can_view_profile(None)
                    )

    def test_an_account_always_sees_its_own_profile(self) -> None:
        viewer = _make_profile("self-viewer", profile_visibility=VisibilityChoice.NO_ONE)
        User.objects.filter(pk=viewer.user_id).update(is_active=False)

        self.assertEqual(Profile.visible_by_slug(viewer.slug, viewer), viewer)


class ReachablePartnerQueryTests(_RelationshipGrid):
    """The one-query partner lookup must answer exactly as :func:`conversation_reachable` does, in every case."""

    def test_the_query_agrees_with_conversation_reachable_everywhere(self) -> None:
        for relationship in self.RELATIONSHIPS:
            viewer, subject = self._pair(relationship)
            for setting in VisibilityChoice.values:
                Profile.objects.filter(pk=subject.pk).update(direct_message_visibility=setting)
                subject.direct_message_visibility = setting
                with self.subTest(relationship=relationship, setting=setting):
                    self.assertEqual(
                        Profile.reachable_partner_by_slug(subject.slug, viewer) is not None,
                        conversation_reachable(viewer, subject),
                    )

    def test_an_account_is_never_its_own_partner(self) -> None:
        viewer = _make_profile("lonely-viewer", direct_message_visibility=VisibilityChoice.ANYONE)

        self.assertIsNone(Profile.reachable_partner_by_slug(viewer.slug, viewer))
