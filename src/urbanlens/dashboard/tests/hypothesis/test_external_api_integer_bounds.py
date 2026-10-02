"""External API integers past their database column are refused with a 4xx and nothing written.

Django adapts an ``IntegerField`` write as psycopg's ``Int4``, whose binary dumper keeps only the low 32 bits: 2**31
is stored as -2**31 and 2**32 + 1 as 1, without an error. Past 2**63 the dumper raises ``OverflowError`` instead, and
a wrapped value that lands below a ``Positive*`` column's check constraint raises ``IntegrityError``. An exact or
greater-than lookup on an out-of-range id matches nothing, so an id that only reaches one is safe; each such route is
pinned here too.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import os

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DataError, transaction
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model
import pytest

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.safety.model import (
    MAX_AUTO_DELETE_AFTER_DAYS,
    SafetyCheckin,
    SafetyCheckinStatus,
    SafetyPreference,
)
from urbanlens.dashboard.models.spotguessr.model import GameSession
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.core.numbers import DB_INTEGER_MAX, DB_INTEGER_MIN, DB_SMALLINT_MAX
from urbanlens.dashboard.tasks import delete_expired_safety_checkins
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase

AccessToken = get_access_token_model()

#: Past ``integer``: the first two wrap on a write, the third fails it, the last wraps to the column's maximum.
_PAST_INTEGER = (2**31, 2**32 + 1, 2**63, DB_INTEGER_MIN - 1)
#: Past ``smallint``, the same way.
_PAST_SMALLINT = (2**15, 2**16 + 1, 2**31, 2**63)
#: Past ``bigint``, which no row id can be.
_PAST_BIGINT = (2**63, 2**64 + 1)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class IntegerColumnWriteTests(TestCase):
    """Why the bounds sit on the parse: the database layer stores an out-of-range write rather than refusing it."""

    @pytest.mark.xfail(strict=True, reason="psycopg's binary Int4 dumper keeps the low 32 bits instead of raising")
    def test_a_write_past_the_integer_column_is_refused_rather_than_wrapped(self) -> None:
        label = baker.make(Label, name="Wrap", kind=KIND_TAG, order=0)

        with self.assertRaises((DataError, OverflowError)), transaction.atomic():
            Label.objects.filter(pk=label.pk).update(order=2**32 + 1)


class LabelBulkEditOrderTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.LABELS_READ, ApiKeyScope.LABELS_WRITE)
    read_scopes = (ApiKeyScope.LABELS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.label = baker.make(Label, profile=self.owner, name="Bounds", kind=KIND_TAG, order=7)
        self.url = reverse("external_api:labels.bulk.edit")

    def _order(self) -> int:
        return Label.objects.get(pk=self.label.pk).order

    def test_an_order_past_the_integer_column_is_400_and_changes_nothing(self) -> None:
        for order in _PAST_INTEGER:
            with self.subTest(order=order):
                response = self.send("post", self.url, {"uuids": [str(self.label.uuid)], "order": order})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self._order(), 7)

    def test_the_column_extremes_are_stored_exactly(self) -> None:
        for order in (DB_INTEGER_MAX, DB_INTEGER_MIN):
            with self.subTest(order=order):
                response = self.send("post", self.url, {"uuids": [str(self.label.uuid)], "order": order})

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(self._order(), order)


class MessageSendIntegerTests(ExternalApiRouteCase):
    """Messaging scopes are OAuth2-only, so the owner sends with a first-party token rather than a key."""

    def setUp(self) -> None:
        super().setUp()
        token = AccessToken.objects.create(
            user=self.owner_user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}",
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}
        Profile.objects.filter(pk__in=[self.owner.pk, self.stranger.pk]).update(
            direct_message_visibility=VisibilityChoice.ANYONE
        )
        self.peer_slug = self.stranger.ensure_slug()
        self.url = reverse("external_api:messages.thread", kwargs={"peer_slug": self.peer_slug})

    def _sent(self) -> list[int]:
        return list(DirectMessage.objects.filter(sender=self.owner).values_list("key_version", flat=True))

    @staticmethod
    def _encrypted(key_version: int) -> dict[str, object]:
        return {"ciphertext": "c2VhbGVk", "nonce": "bm9uY2U=", "key_version": key_version}

    def test_a_key_version_past_the_column_is_400_and_sends_nothing(self) -> None:
        for key_version in _PAST_INTEGER:
            with self.subTest(key_version=key_version):
                response = self.send("post", self.url, self._encrypted(key_version))

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self._sent(), [])

    def test_the_largest_key_version_the_column_holds_is_stored_exactly(self) -> None:
        response = self.send("post", self.url, self._encrypted(DB_INTEGER_MAX))

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self._sent(), [DB_INTEGER_MAX])

    def test_the_dashboard_send_refuses_a_key_version_past_the_column_too(self) -> None:
        self.client.force_login(self.owner_user)
        for key_version in (2**31, 2**32 + 1, 2**63):
            with self.subTest(key_version=key_version):
                response = self.client.post(
                    reverse("messages.send", args=[self.peer_slug]),
                    {"ciphertext": "c2VhbGVk", "nonce": "bm9uY2U=", "key_version": str(key_version)},
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self._sent(), [])

    def test_an_id_past_bigint_is_400_and_sends_nothing(self) -> None:
        for body in (
            *({"body": "hi", "reply_to_id": value} for value in _PAST_BIGINT),
            *({"body": "hi", "image_ids": [value]} for value in _PAST_BIGINT),
        ):
            with self.subTest(body=body):
                response = self.send("post", self.url, body)

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self._sent(), [])


class SafetyAutoDeleteWindowTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.SAFETY_READ, ApiKeyScope.SAFETY_WRITE)
    read_scopes = (ApiKeyScope.SAFETY_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.preference = baker.make(SafetyPreference, profile=self.owner, auto_delete_after_days=30)
        self.url = reverse("external_api:safety.settings")

    def _window(self) -> int | None:
        return SafetyPreference.objects.get(pk=self.preference.pk).auto_delete_after_days

    def _resolved_checkin(self, profile: Profile, *, days_ago: int) -> SafetyCheckin:
        then = timezone.now() - timedelta(days=days_ago)
        checkin = baker.make(
            SafetyCheckin, profile=profile, status=SafetyCheckinStatus.CHECKED_IN, checkin_by=then, resolved_at=then
        )
        SafetyCheckin.objects.filter(pk=checkin.pk).update(created=then)
        return checkin

    def test_a_window_past_the_column_is_400_and_keeps_the_saved_one(self) -> None:
        for days in _PAST_INTEGER:
            with self.subTest(days=days):
                response = self.send("patch", self.url, {"auto_delete_after_days": days})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(self._window(), 30)

    def test_a_window_the_purge_cannot_add_to_a_date_is_refused_and_the_purge_still_runs(self) -> None:
        self._resolved_checkin(self.owner, days_ago=1)
        baker.make(SafetyPreference, profile=self.stranger, auto_delete_after_days=1)
        due = self._resolved_checkin(self.stranger, days_ago=5)

        for days in (MAX_AUTO_DELETE_AFTER_DAYS + 1, 2 * 10**8, DB_INTEGER_MAX):
            with self.subTest(days=days):
                self.assertEqual(self.send("patch", self.url, {"auto_delete_after_days": days}).status_code, 400)
        delete_expired_safety_checkins()

        self.assertEqual(self._window(), 30)
        self.assertFalse(SafetyCheckin.objects.filter(pk=due.pk).exists())

    def test_the_longest_window_is_stored(self) -> None:
        response = self.send("patch", self.url, {"auto_delete_after_days": MAX_AUTO_DELETE_AFTER_DAYS})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._window(), MAX_AUTO_DELETE_AFTER_DAYS)

    def test_the_dashboard_form_stores_the_longest_window_for_anything_past_it(self) -> None:
        self.client.force_login(self.owner_user)
        for days in (MAX_AUTO_DELETE_AFTER_DAYS + 1, 2**32, 2**63):
            with self.subTest(days=days):
                self.client.post(reverse("safety.settings"), {"auto_delete_after_days": str(days)})

                self.assertEqual(self._window(), MAX_AUTO_DELETE_AFTER_DAYS)


class CustomFieldOrderTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.CUSTOM_FIELDS_READ, ApiKeyScope.CUSTOM_FIELDS_WRITE)
    read_scopes = (ApiKeyScope.CUSTOM_FIELDS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.field = baker.make(CustomField, profile=self.owner, entity_type=CustomFieldEntity.PIN, name="Era", order=3)
        self.url = reverse("external_api:custom_fields")

    def test_creating_with_an_order_past_the_smallint_column_is_400_and_stores_nothing(self) -> None:
        for order in _PAST_SMALLINT:
            with self.subTest(order=order):
                response = self.send(
                    "post", self.url, {"entity_type": "pin", "name": f"Condition {order}", "order": order}
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(
                    list(CustomField.objects.filter(profile=self.owner).values_list("name", flat=True)), ["Era"]
                )

    def test_editing_to_an_order_past_the_smallint_column_is_400_and_keeps_the_order(self) -> None:
        url = reverse("external_api:custom_fields.detail", args=[self.field.pk])
        for order in _PAST_SMALLINT:
            with self.subTest(order=order):
                self.assertEqual(self.send("patch", url, {"order": order}).status_code, 400)
                self.assertEqual(CustomField.objects.get(pk=self.field.pk).order, 3)

    def test_the_largest_smallint_is_stored_exactly(self) -> None:
        response = self.send("post", self.url, {"entity_type": "pin", "name": "Condition", "order": DB_SMALLINT_MAX})

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(CustomField.objects.get(profile=self.owner, name="Condition").order, DB_SMALLINT_MAX)


class AccountSettingsDownscaleTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.SETTINGS_READ, ApiKeyScope.SETTINGS_WRITE)
    read_scopes = (ApiKeyScope.SETTINGS_READ,)

    def test_a_downscale_cap_past_the_column_is_400_and_stores_nothing(self) -> None:
        url = reverse("external_api:settings")
        for field in ("image_downscale_max_dimension", "video_downscale_max_height"):
            for value in (*_PAST_INTEGER, 0):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.send("patch", url, {field: value}).status_code, 400)
                    self.assertIsNone(getattr(Profile.objects.get(pk=self.owner.pk), field))


class PhotoUploadVisitTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.PHOTOS_READ, ApiKeyScope.PHOTOS_WRITE)
    read_scopes = (ApiKeyScope.PHOTOS_READ,)

    def test_a_visit_id_past_bigint_is_400_and_uploads_nothing(self) -> None:
        for visit in _PAST_BIGINT:
            with self.subTest(visit=visit):
                response = self.client.post(
                    reverse("external_api:photos"),
                    {"file": SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png"), "visit": visit},
                    HTTP_AUTHORIZATION=self.auth["HTTP_AUTHORIZATION"],
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(Image.objects.filter(profile=self.owner).exists())
                self.assertFalse(PinVisit.objects.exists())


class TripCommentParentTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE)
    read_scopes = (ApiKeyScope.TRIPS_READ,)

    def test_a_parent_id_past_bigint_is_4xx_and_posts_nothing(self) -> None:
        trip = Trip.objects.create(creator=self.owner, name="Detroit")
        TripMembership.objects.create(trip=trip, profile=self.owner, status=TripMembership.STATUS_JOINED, rsvp="yes")
        url = reverse("external_api:trips.comments", args=[trip.slug])
        for parent_id in _PAST_BIGINT:
            with self.subTest(parent_id=parent_id):
                status = self.send("post", url, {"text": "Bring a torch", "parent_id": parent_id}).status_code

                self.assertGreaterEqual(status, 400)
                self.assertLess(status, 500)
                self.assertFalse(TripComment.objects.filter(trip=trip).exists())


class SpotGuessrLabelTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.GAMES_READ, ApiKeyScope.GAMES_WRITE)
    read_scopes = (ApiKeyScope.GAMES_READ,)

    def test_a_label_id_past_bigint_is_400_and_starts_nothing(self) -> None:
        url = reverse("external_api:games.spotguessr.sessions")
        for label_id in _PAST_BIGINT:
            with self.subTest(label_id=label_id):
                self.assertEqual(self.send("post", url, {"label_id": label_id}).status_code, 400)
                self.assertFalse(GameSession.objects.exists())


class WikiIntegerTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.WIKI_READ, ApiKeyScope.WIKI_WRITE)
    read_scopes = (ApiKeyScope.WIKI_READ,)

    def setUp(self) -> None:
        super().setUp()
        location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=location, name="Old Mill", description="Brick.")
        baker.make(Pin, profile=self.owner, location=location)
        self.slug = location.ensure_slug()

    def test_an_edit_based_on_a_revision_past_bigint_is_400_and_changes_nothing(self) -> None:
        url = reverse("external_api:wikis.detail", args=[self.slug])
        for base in _PAST_BIGINT:
            with self.subTest(base_revision_id=base):
                response = self.send("patch", url, {"description": "Defaced", "base_revision_id": base})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(Wiki.objects.get(pk=self.wiki.pk).description, "Brick.")
                self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())

    def test_an_article_based_on_a_revision_past_bigint_is_400_and_saves_nothing(self) -> None:
        url = reverse("external_api:wikis.article", args=[self.slug])
        for base in _PAST_BIGINT:
            with self.subTest(base_revision_id=base):
                response = self.send("put", url, {"content": "Built 1890.", "base_revision_id": base})

                self.assertEqual(response.status_code, 400, response.content)
                self.assertFalse(ArticleRevision.objects.exists())

    def test_a_reply_to_a_comment_id_past_bigint_is_4xx_and_posts_nothing(self) -> None:
        url = reverse("external_api:wikis.comments", args=[self.slug])
        for parent_id in _PAST_BIGINT:
            with self.subTest(parent_id=parent_id):
                status = self.send("post", url, {"text": "Gate is open", "parent_id": parent_id}).status_code

                self.assertGreaterEqual(status, 400)
                self.assertLess(status, 500)
                self.assertFalse(Comment.objects.filter(wiki=self.wiki).exists())
