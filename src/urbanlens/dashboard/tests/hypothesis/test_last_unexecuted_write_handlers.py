"""Write handlers no other test sent their verb to: a photo visit suggestion's accept or dismiss, a profile note's
edit, the default emergency contacts' replacement, a detail pin's delete and a wiki photo's reposition.

Each asserts the owner's write lands, that anyone else is refused and nothing changes, that anonymous is refused (a
login redirect on the dashboard, 401/403 on the external API, and 403 for a key without the write scope), and that a
malformed request is a 4xx rather than a 500 or a silent wrong write.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import json
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.safety.model import (
    EmergencyContactDefault,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.models.undo.model import UndoAction, UndoKind
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion, VisitSuggestionStatus
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.text_limits import MAX_PROFILE_BIO_LENGTH
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse

#: Valid JSON nested past the interpreter's recursion limit.
_DEEPLY_NESTED = "[" * 100_000 + "]" * 100_000


class _OwnerAndStranger(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile

    def assert_login_redirect(self, response: TestResponse) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class ExternalVisitSuggestionActionRouteTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE)
    read_scopes = (ApiKeyScope.PHOTOS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=42.5, longitude=-73.5)
        self.pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None)
        self.image = baker.make(Image, profile=self.owner, pin=self.pin, location=self.location)
        self.visited_at = timezone.now() - datetime.timedelta(days=3)
        self.suggestion = VisitSuggestion.objects.create(
            suggested_to=self.owner,
            origin_image=self.image,
            location=self.location,
            latitude=Decimal("42.5"),
            longitude=Decimal("-73.5"),
            visited_at=self.visited_at,
        )

    def _url(self, action: str, suggestion_id: int | None = None) -> str:
        return reverse(
            "external_api:suggestions.visits.action",
            kwargs={"suggestion_id": self.suggestion.pk if suggestion_id is None else suggestion_id, "action": action},
        )

    def _status(self) -> str:
        return VisitSuggestion.objects.values_list("status", flat=True).get(pk=self.suggestion.pk)

    def test_the_owner_accepts_and_the_visit_is_logged_with_the_photo_attached(self) -> None:
        response = self.send("post", self._url("accept"))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(self._status(), VisitSuggestionStatus.ACCEPTED)
        visit = PinVisit.objects.get(pin=self.pin)
        self.assertEqual((visit.visited_at, visit.source), (self.visited_at, VisitSource.PHOTO))
        self.assertEqual(Image.objects.values_list("visit_id", flat=True).get(pk=self.image.pk), visit.pk)

    def test_the_owner_dismisses_and_no_visit_is_logged(self) -> None:
        response = self.send("post", self._url("dismiss"))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(self._status(), VisitSuggestionStatus.REJECTED)
        self.assertFalse(PinVisit.objects.exists())

    def test_accepting_with_visit_tracking_off_is_403_and_the_suggestion_stays_pending(self) -> None:
        Profile.objects.filter(pk=self.owner.pk).update(track_pin_visits=False)

        self.assertEqual(self.send("post", self._url("accept")).status_code, 403)
        self.assertEqual(self._status(), VisitSuggestionStatus.PENDING)
        self.assertFalse(PinVisit.objects.exists())

    def test_a_stranger_gets_404_and_nothing_changes(self) -> None:
        for action in ("accept", "dismiss"):
            with self.subTest(action=action):
                self.assertEqual(self.send("post", self._url(action), auth=self.stranger_auth).status_code, 404)
        self.assertEqual(self._status(), VisitSuggestionStatus.PENDING)
        self.assertFalse(PinVisit.objects.exists())
        self.assertFalse(Pin.objects.filter(profile=self.stranger).exists())

    def test_an_answered_suggestion_is_404_and_logs_no_second_visit(self) -> None:
        self.send("post", self._url("accept"))

        for action in ("accept", "dismiss"):
            with self.subTest(action=action):
                self.assertEqual(self.send("post", self._url(action)).status_code, 404)
        self.assertEqual(self._status(), VisitSuggestionStatus.ACCEPTED)
        self.assertEqual(PinVisit.objects.count(), 1)

    def test_a_suggestion_not_raised_from_a_photo_is_not_answered_here(self) -> None:
        other = VisitSuggestion.objects.create(
            suggested_to=self.owner,
            from_my_activity=True,
            location=self.location,
            latitude=Decimal("42.5"),
            longitude=Decimal("-73.5"),
            visited_at=self.visited_at,
        )

        self.assertEqual(self.send("post", self._url("accept", other.pk)).status_code, 404)
        self.assertEqual(VisitSuggestion.objects.get(pk=other.pk).status, VisitSuggestionStatus.PENDING)
        self.assertFalse(PinVisit.objects.exists())

    def test_an_unknown_action_or_id_is_404_and_changes_nothing(self) -> None:
        for action in ("merge", "ACCEPT", "delete", "accept "):
            with self.subTest(action=action):
                self.assertEqual(self.send("post", self._url(action)).status_code, 404)
        for suggestion_id in (0, self.suggestion.pk + 1000, 2**31, 2**63):
            with self.subTest(suggestion_id=suggestion_id):
                self.assertEqual(self.send("post", self._url("accept", suggestion_id)).status_code, 404)
        self.assertEqual(self._status(), VisitSuggestionStatus.PENDING)
        self.assertFalse(PinVisit.objects.exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self._url("accept"))
        self.assertEqual(self._status(), VisitSuggestionStatus.PENDING)
        self.assertFalse(PinVisit.objects.exists())


class ExternalProfileNoteEditRouteTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.SOCIAL_READ, ApiKeyScope.SOCIAL_WRITE)
    read_scopes = (ApiKeyScope.SOCIAL_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.subject = baker.make(User).profile
        Profile.objects.filter(pk=self.subject.pk).update(slug="subject")
        Profile.objects.filter(pk=self.stranger.pk).update(slug="stranger")
        self.note = ProfileNote.objects.create(author=self.owner, subject=self.subject, content="Reliable lookout")
        self.sibling = ProfileNote.objects.create(author=self.owner, subject=self.subject, content="Owes me a torch")
        self.url = self._url("subject")

    def _url(self, profile_slug: str) -> str:
        return reverse(
            "external_api:profiles.notes.detail", kwargs={"profile_slug": profile_slug, "note_uuid": self.note.uuid}
        )

    def _contents(self) -> tuple[str, str]:
        return ProfileNote.objects.get(pk=self.note.pk).content, ProfileNote.objects.get(pk=self.sibling.pk).content

    def test_the_author_replaces_the_notes_content(self) -> None:
        response = self.send("patch", self.url, {"content": "Moved to Ohio"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["content"], "Moved to Ohio")
        self.assertEqual(self._contents(), ("Moved to Ohio", "Owes me a torch"))

    def test_the_author_may_blank_the_note(self) -> None:
        self.assertEqual(self.send("patch", self.url, {"content": ""}).status_code, 200)
        self.assertEqual(self._contents(), ("", "Owes me a torch"))

    def test_a_stranger_gets_404_and_the_note_is_unchanged(self) -> None:
        response = self.send("patch", self.url, {"content": "pwned"}, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._contents(), ("Reliable lookout", "Owes me a torch"))

    def test_the_subject_cannot_edit_a_note_kept_about_them(self) -> None:
        subject_auth = self.key(self.subject.user, self.scopes)

        self.assertEqual(self.send("patch", self.url, {"content": "Lies"}, auth=subject_auth).status_code, 404)
        self.assertEqual(self._contents(), ("Reliable lookout", "Owes me a torch"))

    def test_the_note_is_not_reachable_under_another_profile_or_an_unknown_one(self) -> None:
        for slug in ("stranger", "nobody-by-this-name", "00000000-0000-4000-8000-000000000000"):
            with self.subTest(slug=slug):
                self.assertEqual(self.send("patch", self._url(slug), {"content": "moved"}).status_code, 404)
        self.assertEqual(self._contents(), ("Reliable lookout", "Owes me a torch"))

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("patch", self.url, {"content": "x"})
        self.assertEqual(self._contents(), ("Reliable lookout", "Owes me a torch"))

    def test_a_malformed_or_overlong_body_is_400_and_changes_nothing(self) -> None:
        self.assert_malformed_bodies_are_4xx("patch", self.url)
        bad: tuple[object, ...] = (
            {},
            {"content": None},
            {"content": ["a"]},
            {"content": {"text": "a"}},
            {"content": "a\x00b"},
            {"content": "x" * (MAX_PROFILE_BIO_LENGTH + 1)},
            _DEEPLY_NESTED,
        )
        for body in bad:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self.send("patch", self.url, body).status_code, 400)
        self.assertEqual(self._contents(), ("Reliable lookout", "Owes me a torch"))


class ExternalSafetyContactDefaultsReplaceRouteTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.SAFETY_READ, ApiKeyScope.SAFETY_WRITE)
    read_scopes = (ApiKeyScope.SAFETY_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:safety.contacts")
        self.friend = baker.make(User).profile
        Friendship.objects.create(from_profile=self.owner, to_profile=self.friend, status=FriendshipStatus.ACCEPTED)
        EmergencyContactDefault.objects.create(owner=self.owner, email="old@example.com", label="Old", order=0)
        EmergencyContactDefault.objects.create(owner=self.stranger, email="theirs@example.com", order=0)

    @staticmethod
    def _defaults(profile: Profile) -> list[tuple[int | None, str | None, str]]:
        return [
            (row.contact_profile_id, row.email, row.label)
            for row in EmergencyContactDefault.objects.for_owner(profile).order_by("order")
        ]

    def test_the_owner_replaces_the_whole_list_in_order(self) -> None:
        body = {"contacts": [{"username": self.friend.username}, {"email": " Mo@Example.com ", "name": " Mo "}]}

        response = self.send("put", self.url, body)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content)["rejected"], [])
        self.assertEqual(
            self._defaults(self.owner), [(self.friend.pk, None, self.friend.username), (None, "mo@example.com", "Mo")]
        )
        self.assertEqual(self._defaults(self.stranger), [(None, "theirs@example.com", "")])

    def test_an_empty_list_clears_the_defaults(self) -> None:
        self.assertEqual(self.send("put", self.url, {"contacts": []}).status_code, 200)
        self.assertEqual(self._defaults(self.owner), [])

    def test_an_account_that_is_not_a_connection_is_rejected_and_never_saved(self) -> None:
        body = {"contacts": [{"username": self.stranger.username}, {"username": self.owner.username}]}

        response = self.send("put", self.url, body)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(json.loads(response.content)["rejected"]), 2)
        self.assertEqual(self._defaults(self.owner), [])

    def test_a_duplicate_or_opted_out_address_is_rejected(self) -> None:
        SafetyContactOptOut.objects.create(email="gone@example.com", scope=SafetyContactOptOutScope.GLOBAL)
        body = {"contacts": [{"email": "mo@example.com"}, {"email": "MO@example.com"}, {"email": "gone@example.com"}]}

        response = self.send("put", self.url, body)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(json.loads(response.content)["rejected"]), 2)
        self.assertEqual(self._defaults(self.owner), [(None, "mo@example.com", "")])

    def test_a_stranger_replaces_only_their_own_defaults_and_cannot_name_the_owners_friend(self) -> None:
        body = {"contacts": [{"email": "new@example.com"}, {"username": self.friend.username}]}

        response = self.send("put", self.url, body, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._defaults(self.stranger), [(None, "new@example.com", "")])
        self.assertEqual(self._defaults(self.owner), [(None, "old@example.com", "Old")])

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"contacts": []})
        self.assertEqual(self._defaults(self.owner), [(None, "old@example.com", "Old")])

    def test_a_malformed_body_is_400_and_the_saved_defaults_survive(self) -> None:
        self.assert_malformed_bodies_are_4xx("put", self.url)
        bad: tuple[object, ...] = (
            {},
            {"contacts": None},
            {"contacts": "mo@example.com"},
            {"contacts": [None]},
            {"contacts": [["mo@example.com"]]},
            {"contacts": [{}]},
            {"contacts": [{"username": "   "}]},
            {"contacts": [{"username": self.friend.username, "email": "mo@example.com"}]},
            {"contacts": [{"email": "not-an-address"}]},
            {"contacts": [{"email": "mo@example.com", "name": "x" * 151}]},
            {"contacts": [{"email": "mo@example.com"}, {"email": "broken"}]},
            _DEEPLY_NESTED,
        )
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self.send("put", self.url, body).status_code, 400)
        self.assertEqual(self._defaults(self.owner), [(None, "old@example.com", "Old")])


class DetailPinDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.root = baker.make(
            Pin,
            profile=self.owner,
            slug="old-mill",
            parent_pin=None,
            location=Location.objects.create(latitude=42.0, longitude=-73.0),
        )
        self.detail = baker.make(
            Pin,
            profile=self.owner,
            name="Boiler house",
            parent_pin=self.root,
            location=Location.objects.create(latitude=42.0001, longitude=-73.0001),
        )
        self.nested = baker.make(
            Pin,
            profile=self.owner,
            name="Valve",
            parent_pin=self.detail,
            location=Location.objects.create(latitude=42.0002, longitude=-73.0002),
        )
        self.url = self._url(self.root, self.detail)

    @staticmethod
    def _url(pin: Pin, detail: Pin) -> str:
        return reverse("pin.detail_pin.edit", kwargs={"pin_slug": pin.slug, "detail_pin_uuid": detail.uuid})

    def _surviving(self) -> set[int]:
        return set(
            Pin.objects.filter(pk__in=[self.root.pk, self.detail.pk, self.nested.pk]).values_list("pk", flat=True)
        )

    def test_the_owner_deletes_the_detail_pin_and_what_is_nested_under_it_and_can_undo_it(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response["HX-Trigger"])["showToast"]["level"], "success")
        self.assertEqual(self._surviving(), {self.root.pk})
        action = UndoAction.objects.get(profile=self.owner)
        self.assertEqual((action.model_label, action.kind), ("pin", UndoKind.DELETE))
        self.assertEqual({entry["old_pk"] for entry in action.payload}, {self.detail.pk, self.nested.pk})

    def test_a_stranger_gets_404_and_the_detail_pin_survives(self) -> None:
        own_pin = baker.make(Pin, profile=self.stranger, slug="their-pin", parent_pin=None)
        self.client.force_login(self.stranger_user)

        for url in (self.url, self._url(own_pin, self.detail)):
            with self.subTest(url=url):
                self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self._surviving(), {self.root.pk, self.detail.pk, self.nested.pk})
        self.assertFalse(UndoAction.objects.exists())

    def test_the_named_pin_itself_and_another_pins_detail_are_not_deleted_through_it(self) -> None:
        other_root = baker.make(Pin, profile=self.owner, slug="gasworks", parent_pin=None)
        self.client.force_login(self.owner_user)

        for url in (self._url(self.root, self.root), self._url(other_root, self.detail)):
            with self.subTest(url=url):
                self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertEqual(self._surviving(), {self.root.pk, self.detail.pk, self.nested.pk})

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertEqual(self._surviving(), {self.root.pk, self.detail.pk, self.nested.pk})

    def test_a_get_deletes_nothing(self) -> None:
        self.client.force_login(self.owner_user)

        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self._surviving(), {self.root.pk, self.detail.pk, self.nested.pk})


class WikiImageRepositionRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.location = Location.objects.create(latitude=42.5, longitude=-73.5)
        self.wiki = Wiki.objects.create(location=self.location)
        baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None)
        self.image = baker.make(
            Image,
            profile=self.owner,
            wiki=self.wiki,
            location=self.location,
            latitude=Decimal("42.5"),
            longitude=Decimal("-73.5"),
            map_hidden=False,
        )
        self.url = self._url(self.location, self.image)

    @staticmethod
    def _url(location: Location, image: Image) -> str:
        return reverse("location.wiki.gallery.image", kwargs={"location_slug": location.slug, "image_id": image.pk})

    def _post(self, body: object, url: str | None = None) -> TestResponse:
        data = body if isinstance(body, str | bytes) else json.dumps(body)
        return self.client.post(url or self.url, data=data, content_type="application/json")

    def _state(self, image: Image | None = None) -> tuple[float, float, bool]:
        latitude, longitude, hidden = Image.objects.values_list("latitude", "longitude", "map_hidden").get(
            pk=(image or self.image).pk
        )
        return float(latitude), float(longitude), hidden

    def test_the_uploader_moves_the_photo_and_it_shows_on_the_map_again(self) -> None:
        Image.objects.filter(pk=self.image.pk).update(map_hidden=True)
        self.client.force_login(self.owner_user)

        response = self._post({"latitude": 42.51, "longitude": -73.49})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(json.loads(response.content), {"latitude": 42.51, "longitude": -73.49, "map_hidden": False})
        self.assertEqual(self._state(), (42.51, -73.49, False))

    def test_the_uploader_hides_and_shows_the_photo_without_moving_it(self) -> None:
        self.client.force_login(self.owner_user)

        self.assertEqual(self._post({"map_hidden": True}).status_code, 200)
        self.assertEqual(self._state(), (42.5, -73.5, True))
        self.assertEqual(self._post({"map_hidden": False}).status_code, 200)
        self.assertEqual(self._state(), (42.5, -73.5, False))

    def test_another_viewer_of_the_wiki_gets_404_and_cannot_move_or_hide_the_photo(self) -> None:
        baker.make(Pin, profile=self.stranger, location=self.location, parent_pin=None)
        self.client.force_login(self.stranger_user)

        for body in ({"latitude": 0, "longitude": 0}, {"map_hidden": True}):
            with self.subTest(body=body):
                self.assertEqual(self._post(body).status_code, 404)
        self.assertEqual(self._state(), (42.5, -73.5, False))

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._post({"map_hidden": True}).status_code, 404)
        self.assertEqual(self._state(), (42.5, -73.5, False))

    def test_a_photo_on_another_wiki_is_not_reachable_through_this_one(self) -> None:
        elsewhere = Location.objects.create(latitude=41.0, longitude=-74.0)
        baker.make(Pin, profile=self.owner, location=elsewhere, parent_pin=None)
        other_image = baker.make(
            Image,
            profile=self.owner,
            wiki=Wiki.objects.create(location=elsewhere),
            location=elsewhere,
            latitude=Decimal("41.0"),
            longitude=Decimal("-74.0"),
        )
        self.client.force_login(self.owner_user)

        response = self._post({"map_hidden": True}, url=self._url(self.location, other_image))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._state(other_image), (41.0, -74.0, False))

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post({"map_hidden": True}))
        self.assertEqual(self._state(), (42.5, -73.5, False))

    def test_a_malformed_body_is_400_and_moves_nothing(self) -> None:
        self.client.force_login(self.owner_user)
        bad: tuple[object, ...] = (
            "[]",
            "null",
            '"text"',
            "{",
            "[1,",
            b"\x80\xff",
            "{}",
            {"latitude": 1},
            {"latitude": "north", "longitude": 0},
            {"latitude": 91, "longitude": 0},
            {"latitude": 0, "longitude": 181},
            {"latitude": "nan", "longitude": 0},
            {"latitude": 0, "longitude": "Infinity"},
            {"latitude": None, "longitude": None},
            {"latitude": True, "longitude": 0},
            _DEEPLY_NESTED,
        )
        for body in bad:
            with self.subTest(body=str(body)[:40]):
                self.assertEqual(self._post(body).status_code, 400)
        self.assertEqual(self._state(), (42.5, -73.5, False))

    def test_a_map_hidden_that_is_not_a_boolean_is_400_and_does_not_hide_the_photo(self) -> None:
        self.client.force_login(self.owner_user)

        for value in ("false", "0", "no", [False], {"hidden": False}):
            with self.subTest(value=value):
                self.assertEqual(self._post({"map_hidden": value}).status_code, 400)
        self.assertEqual(self._state(), (42.5, -73.5, False))
