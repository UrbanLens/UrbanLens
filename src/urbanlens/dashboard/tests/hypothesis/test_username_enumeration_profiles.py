"""A profile the viewer may not see answers every slug-addressed route exactly as a slug nobody holds."""

from __future__ import annotations

from datetime import timedelta
import os
import re
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import DirectMessageConsumer
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.e2ee import MessagingKeyBundle
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat
from urbanlens.dashboard.services.social.friendship import block_profile

NOBODY = "nobody_holds_this"
_CSRF_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")
_ALL_SCOPES = " ".join(scope.value for scope in ApiKeyScope)
_METHODS = ("get", "post", "put", "patch", "delete")
_FORM = {"content": "x", "rating": "3", "nickname": "n", "body": "hi", "value": "v", "before": "1", "q": "hi"}

#: (url name, extra kwargs) for every web route addressed by a profile slug.
WEB_ROUTES = [
    ("profile.view_user", {}),
    ("profile.common_pins", {}),
    ("achievement.profile_panel", {}),
    ("achievement.list", {}),
    ("profile.note", {}),
    ("profile.note.delete", {"note_id": 1}),
    ("profile.note.edit", {"note_id": 1}),
    ("profile.label_toggle", {"label_id": 1}),
    ("profile.trust", {}),
    ("profile.nickname", {}),
    ("profile.custom_field_value", {"field_id": 1}),
    ("messages.conversation", {}),
    ("messages.send", {}),
    ("messages.read", {}),
    ("messages.mute", {}),
    ("messages.older", {}),
    ("messages.conversation_search", {}),
    ("messages.react", {"message_id": 1}),
    ("messages.delete", {"message_id": 1}),
    ("messages.image_permission", {}),
    ("messages.share.pin", {}),
    ("messages.share.trip", {}),
    ("messages.share.friend", {}),
    ("messages.share.pin.respond", {"message_id": 1}),
    ("messages.share.friend.respond", {"message_id": 1}),
    ("messages.mention.add_pin", {"mention_id": 1}),
    ("e2ee.partner_key", {}),
    ("e2ee.conversation_key", {}),
]

#: (url name, slug kwarg, extra kwargs) for every external API route addressed by a profile slug.
API_ROUTES = [
    ("profiles.detail", "profile_slug", {}),
    ("profiles.notes", "profile_slug", {}),
    ("profiles.annotations", "profile_slug", {}),
    ("profiles.nickname", "profile_slug", {}),
    ("profiles.trust", "profile_slug", {}),
    ("profiles.social_links", "profile_slug", {}),
    ("profiles.avatar", "profile_slug", {}),
    ("messages.thread", "peer_slug", {}),
    ("messages.read", "peer_slug", {}),
    ("messages.mute", "peer_slug", {}),
    ("messages.react", "peer_slug", {"message_id": 1}),
    ("messages.detail", "peer_slug", {"message_id": 1}),
]


def _profile(username: str, *, active: bool = True, **fields) -> Profile:
    user = baker.make(User, username=username, is_active=active)
    Profile.objects.filter(user=user).update(**fields)
    profile = Profile.objects.get(user=user)
    profile.ensure_slug()
    return profile


def _scrub(content: bytes, slug: str) -> str:
    return _CSRF_TOKEN_RE.sub("CSRF", content.decode()).replace(slug, "SLUG")


class _HiddenProfilesTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is promoted to site admin
        open_to_all = {
            "profile_visibility": VisibilityChoice.ANYONE,
            "direct_message_visibility": VisibilityChoice.ANYONE,
        }
        self.viewer = _profile("the_viewer", **open_to_all)
        blocker = _profile("the_blocker", **open_to_all)
        block_profile(blocker, self.viewer)
        #: Each is a real account this viewer may neither see nor message.
        self.hidden = {
            "not visible to the viewer": _profile(
                "friends_only",
                profile_visibility=VisibilityChoice.FRIENDS,
                direct_message_visibility=VisibilityChoice.FRIENDS,
            ).slug,
            "has blocked the viewer": blocker.slug,
            "inactive account": _profile("pending_signup", active=False, **open_to_all).slug,
        }
        self.visible = _profile("open_book", **open_to_all)
        for profile in Profile.objects.filter(slug__in=self.hidden.values()):
            baker.make(MessagingKeyBundle, profile=profile)


class WebRoutesHideProfilesTests(_HiddenProfilesTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.viewer.user)

    def _answer(self, method: str, name: str, slug: str, extra: dict) -> tuple[int, str]:
        url = reverse(name, kwargs={"profile_slug": slug, **extra})
        response = getattr(self.client, method)(url, _FORM if method == "post" else None)
        return response.status_code, _scrub(response.content, slug)

    def test_every_route_answers_a_hidden_profile_as_it_answers_no_profile(self) -> None:
        for name, extra in WEB_ROUTES:
            for method in _METHODS:
                expected = self._answer(method, name, NOBODY, extra)
                for case, slug in self.hidden.items():
                    with self.subTest(route=name, method=method, case=case):
                        self.assertEqual(self._answer(method, name, slug, extra), expected)

    def test_a_visible_profile_and_the_one_you_blocked_still_open(self) -> None:
        blocked = _profile("blocked_by_viewer", profile_visibility=VisibilityChoice.ANYONE)
        block_profile(self.viewer, blocked)
        for profile in (self.visible, blocked):
            with self.subTest(profile=profile.username):
                response = self.client.get(reverse("profile.view_user", kwargs={"profile_slug": profile.slug}))
                self.assertEqual(response.status_code, 200)

    def test_a_messageable_profile_opens_a_conversation(self) -> None:
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.visible.slug}))

        self.assertEqual(response.status_code, 200)

    def test_group_creation_refuses_a_hidden_member_as_it_refuses_no_one(self) -> None:
        def answer(slug: str) -> tuple[int, str]:
            response = self.client.post(reverse("messages.group.create"), {"name": "g", "member_slugs": [slug]})
            return response.status_code, _scrub(response.content, slug)

        expected = answer(NOBODY)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(answer(slug), expected)

    def test_adding_a_group_member_refuses_a_hidden_member_as_it_refuses_no_one(self) -> None:
        group = create_group_chat(self.viewer, "g", [self.visible])

        def answer(slug: str) -> tuple[int, str]:
            url = reverse("messages.group.members.add", kwargs={"group_uuid": group.uuid})
            response = self.client.post(url, {"member_slugs": [slug]})
            return response.status_code, _scrub(response.content, slug)

        expected = answer(NOBODY)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(answer(slug), expected)


class WebSocketSendHidesProfilesTests(_HiddenProfilesTestCase):
    """The consumer maps each exception type to one error frame, so equal types mean equal frames."""

    def _refusal(self, slug: str) -> type[Exception] | None:
        create = DirectMessageConsumer.__dict__["_create_message"].func
        try:
            create(SimpleNamespace(profile_id=self.viewer.pk), slug, "hi", "", "", 0, [], None, None)
        except Exception as exc:  # noqa: BLE001  # the type is what is compared
            return type(exc)
        return None

    def test_sending_to_a_hidden_profile_fails_as_sending_to_no_one(self) -> None:
        expected = self._refusal(NOBODY)
        self.assertIs(expected, ValueError)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertIs(self._refusal(slug), expected)


class FriendshipActionsHideProfilesTests(_HiddenProfilesTestCase):
    """The profile-id friendship buttons must not turn an id into a username, or a hidden id into a yes."""

    ACTIONS = (
        "friend.request",
        "friend.accept",
        "friend.reject",
        "friend.ignore",
        "friend.remove",
        "friend.block",
        "friend.unblock",
        "friend.mute",
        "friend.unmute",
    )

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.viewer.user)
        self.hidden_ids = {case: Profile.objects.get(slug=slug).pk for case, slug in self.hidden.items()}

    def _answer(self, name: str, profile_id: int, *, htmx: bool) -> tuple[int, str, str]:
        headers = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        response = self.client.post(reverse(name, kwargs={"profile_id": profile_id}), {"message": "hi"}, **headers)
        return response.status_code, response.content.decode(), response.get("Location", "")

    def test_every_action_answers_a_hidden_profile_as_it_answers_no_profile(self) -> None:
        for name in self.ACTIONS:
            for htmx in (False, True):
                expected = self._answer(name, 10**9, htmx=htmx)
                for case, profile_id in self.hidden_ids.items():
                    with self.subTest(action=name, htmx=htmx, case=case):
                        self.assertEqual(self._answer(name, profile_id, htmx=htmx), expected)

    def test_friend_lists_answer_a_hidden_profile_as_they_answer_no_profile(self) -> None:
        def answer(name: str, profile_id: int) -> tuple[int, str, str]:
            response = self.client.get(reverse(name, kwargs={"profile_id": profile_id}))
            return response.status_code, response.content.decode(), response.get("Location", "")

        for name in ("friend.list", "friend.page", "friend.page_widget"):
            expected = answer(name, 10**9)
            for case, profile_id in self.hidden_ids.items():
                with self.subTest(route=name, case=case):
                    self.assertEqual(answer(name, profile_id), expected)

    def test_a_visible_profile_can_still_be_blocked_and_requested(self) -> None:
        for name in ("friend.request", "friend.block"):
            with self.subTest(action=name):
                status, _, location = self._answer(name, self.visible.pk, htmx=False)
                self.assertEqual((status, location), (302, reverse("profile.view_user", args=[self.visible.slug])))
            self.client.post(reverse("friend.unblock", kwargs={"profile_id": self.visible.pk}))


class ExternalApiRoutesHideProfilesTests(_HiddenProfilesTestCase):
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

    def _call(self, method: str, url: str, payload: dict) -> tuple[int, str]:
        response = getattr(self.client, method)(url, payload, content_type="application/json", **self.auth)
        return response.status_code, response.content.decode()

    def test_every_route_answers_a_hidden_profile_as_it_answers_no_profile(self) -> None:
        for name, kwarg, extra in API_ROUTES:
            for method in _METHODS:

                def answer(slug: str, name=name, kwarg=kwarg, extra=extra, method=method) -> tuple[int, str]:
                    url = reverse(f"external_api:{name}", kwargs={kwarg: slug, **extra})
                    status, body = self._call(method, url, {"body": "hi", "content": "x", "rating": 3})
                    return status, body.replace(slug, "SLUG")

                expected = answer(NOBODY)
                for case, slug in self.hidden.items():
                    with self.subTest(route=name, method=method, case=case):
                        self.assertEqual(answer(slug), expected)

    def test_group_creation_refuses_a_hidden_member_as_it_refuses_no_one(self) -> None:
        def answer(slug: str) -> tuple[int, str]:
            status, body = self._call(
                "post", reverse("external_api:messages.groups"), {"name": "g", "member_slugs": [slug]}
            )
            return status, body.replace(slug, "SLUG")

        expected = answer(NOBODY)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(answer(slug), expected)

    def test_adding_a_group_member_refuses_a_hidden_member_as_it_refuses_no_one(self) -> None:
        group = create_group_chat(self.viewer, "g", [self.visible])

        def answer(slug: str) -> tuple[int, str]:
            url = reverse("external_api:messages.groups.members", kwargs={"group_uuid": group.uuid})
            status, body = self._call("post", url, {"member_slugs": [slug]})
            return status, body.replace(slug, "SLUG")

        expected = answer(NOBODY)
        for case, slug in self.hidden.items():
            with self.subTest(case=case):
                self.assertEqual(answer(slug), expected)
