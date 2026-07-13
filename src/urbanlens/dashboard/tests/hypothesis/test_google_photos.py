"""Tests for the Google Photos Picker integration.

Covers:
- GooglePhotosGateway - session create/get, media item listing (pagination),
  duration-string parsing, download with the ``=d``/``=w..-h..`` suffix.
- Settings connect/callback/disconnect (OAuth2 state validation, mirrors
  GoogleCalendarCallbackView's tests).
- Pin-detail session create/status views - the session/poll/list flow, since
  there's no coordinate filter to test here (every picked item is a candidate).
- import_google_photos task - new-item happy path and checksum dedupe.

All HTTP/OAuth calls are mocked; no real network access occurs.
"""

from __future__ import annotations

import hashlib
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.google_photos.model import GooglePhotosAccount
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.photos.google import GooglePhotosGateway, PickedMediaItem, PickerSession, media_item_web_url


def _mock_response(*, ok: bool = True, status_code: int = 200, json_data=None, content: bytes = b""):
    resp = mock.MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = content
    resp.text = ""
    return resp


def _account(**kwargs) -> GooglePhotosAccount:
    import datetime

    from django.utils import timezone

    defaults = {"access_token": "access", "refresh_token": "refresh", "token_expiry": timezone.now() + datetime.timedelta(hours=1)}
    defaults.update(kwargs)
    user = baker.make(User)
    return GooglePhotosAccount(profile=user.profile, **defaults)


class GooglePhotosGatewayTests(TestCase):
    """GooglePhotosGateway wraps session create/get/list/download."""

    def _gateway(self) -> GooglePhotosGateway:
        return GooglePhotosGateway(account=_account(), session=mock.MagicMock())

    def test_create_session_parses_polling_config(self) -> None:
        gw = self._gateway()
        gw.session.post.return_value = _mock_response(
            json_data={"id": "sess1", "pickerUri": "https://photos.google.com/picker/sess1", "pollingConfig": {"pollInterval": "5s", "timeoutIn": "300s"}, "mediaItemsSet": False},
        )
        result = gw.create_session()
        self.assertEqual(result, PickerSession(id="sess1", picker_uri="https://photos.google.com/picker/sess1", media_items_set=False, poll_interval_s=5, timeout_s=300))

    def test_create_session_raises_on_error(self) -> None:
        from urbanlens.dashboard.services.gateway import GatewayRequestError

        gw = self._gateway()
        gw.session.post.return_value = _mock_response(ok=False, status_code=500)
        with self.assertRaises(GatewayRequestError):
            gw.create_session()

    def test_list_session_media_items_follows_pagination(self) -> None:
        gw = self._gateway()
        gw.session.get.side_effect = [
            _mock_response(json_data={"mediaItems": [{"id": "a", "mediaFile": {"baseUrl": "https://x/a", "mimeType": "image/jpeg", "filename": "a.jpg"}}], "nextPageToken": "p2"}),
            _mock_response(json_data={"mediaItems": [{"id": "b", "mediaFile": {"baseUrl": "https://x/b", "mimeType": "image/jpeg", "filename": "b.jpg"}}]}),
        ]
        items = gw.list_session_media_items("sess1")
        self.assertEqual([i.id for i in items], ["a", "b"])
        self.assertEqual(gw.session.get.call_count, 2)

    def test_download_media_item_uses_original_suffix(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(content=b"bytes")
        gw.download_media_item("https://x/a", original=True)
        called_url = gw.session.get.call_args[0][0]
        self.assertTrue(called_url.endswith("=d"))

    def test_download_media_item_uses_preview_suffix(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(content=b"bytes")
        gw.download_media_item("https://x/a", original=False)
        called_url = gw.session.get.call_args[0][0]
        self.assertIn("=w", called_url)


# -- Settings: connect / disconnect -------------------------------------------


class GooglePhotosSettingsViewTests(TestCase):
    """Callback rejects tampered/missing state without storing tokens; disconnect removes the row."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_bad_state_redirects_without_creating_account(self) -> None:
        response = self.client.get(reverse("settings.google_photos.callback"), {"state": "forged", "code": "abc"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GooglePhotosAccount.objects.filter(profile=self.profile).exists())

    def test_provider_error_redirects_without_creating_account(self) -> None:
        response = self.client.get(reverse("settings.google_photos.callback"), {"error": "access_denied"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(GooglePhotosAccount.objects.filter(profile=self.profile).exists())

    def test_disconnect_removes_the_account(self) -> None:
        GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")
        self.client.post(reverse("settings.google_photos.disconnect"))
        self.assertFalse(GooglePhotosAccount.objects.filter(profile=self.profile).exists())


# -- Pin detail: session / status ----------------------------------------------


class PinGooglePhotosSessionTests(TestCase):
    """Session create/status views drive the poll-until-picked flow; every item is a candidate."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location")
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")

    def test_session_create_returns_waiting_state(self) -> None:
        picker_session = PickerSession(id="sess1", picker_uri="https://photos.google.com/picker/sess1", media_items_set=False, poll_interval_s=5, timeout_s=300)
        with mock.patch.object(GooglePhotosGateway, "create_session", return_value=picker_session):
            response = self.client.post(reverse("pin.google_photos.session.create", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["session_id"], "sess1")

    def test_session_status_still_waiting(self) -> None:
        cache.set("ul_gphotos_session_owner_sess1", self.profile.id, 600)
        picker_session = PickerSession(id="sess1", picker_uri="https://photos.google.com/picker/sess1", media_items_set=False, poll_interval_s=5, timeout_s=300)
        with mock.patch.object(GooglePhotosGateway, "get_session", return_value=picker_session):
            response = self.client.get(reverse("pin.google_photos.session.status", args=[self.pin.slug, "sess1"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Waiting")

    def test_session_status_ready_lists_all_picked_items_as_candidates(self) -> None:
        cache.set("ul_gphotos_session_owner_sess1", self.profile.id, 600)
        picker_session = PickerSession(id="sess1", picker_uri="https://photos.google.com/picker/sess1", media_items_set=True, poll_interval_s=5, timeout_s=300)
        item = PickedMediaItem(id="item1", base_url="https://x/item1", mime_type="image/jpeg", filename="item1.jpg", create_time=None)
        with (
            mock.patch.object(GooglePhotosGateway, "get_session", return_value=picker_session),
            mock.patch.object(GooglePhotosGateway, "list_session_media_items", return_value=[item]),
        ):
            response = self.client.get(reverse("pin.google_photos.session.status", args=[self.pin.slug, "sess1"]))
        self.assertEqual(response.context["assets"], [{"id": "item1", "already_imported": False}])

    def test_status_for_unowned_session_is_404(self) -> None:
        # Never registered as owned by this profile.
        response = self.client.get(reverse("pin.google_photos.session.status", args=[self.pin.slug, "someone-elses-session"]))
        self.assertEqual(response.status_code, 404)


# -- Celery task: import_google_photos ----------------------------------------------


class ImportGooglePhotosTaskTests(TestCase):
    """import_google_photos resolves items from the session cache and imports them."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location")
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")

    def _seed_cache(self, session_id: str, items: dict) -> None:
        from urbanlens.dashboard.services.apis.photos.google import session_items_cache_key

        cache.set(session_items_cache_key(session_id), items, 3600)

    def test_imports_a_new_item_and_logs_a_visit(self) -> None:
        self._seed_cache("sess1", {"item1": {"base_url": "https://x/item1", "mime_type": "image/jpeg", "filename": "item1.jpg"}})
        with (
            mock.patch.object(GooglePhotosGateway, "download_media_item", return_value=b"jpeg-bytes"),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_google_photos(self.pin.pk, self.profile.pk, "sess1", ["item1"])

        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertEqual(image.source_url, media_item_web_url("item1"))
        self.assertTrue(PinVisit.objects.filter(pin=self.pin, source=VisitSource.PHOTO).exists())

    def test_skips_item_already_imported_by_checksum(self) -> None:
        content = b"already-here"
        checksum = hashlib.sha256(content).hexdigest()
        baker.make(Image, pin=self.pin, profile=self.profile, checksum=checksum)
        self._seed_cache("sess1", {"dup": {"base_url": "https://x/dup", "mime_type": "image/jpeg", "filename": "dup.jpg"}})

        with (
            mock.patch.object(GooglePhotosGateway, "download_media_item", return_value=content),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_google_photos(self.pin.pk, self.profile.pk, "sess1", ["dup"])

        self.assertEqual(counts, {"imported": 0, "skipped": 1, "failed": 0})

    def test_item_missing_from_cache_and_relist_fails_counts_as_failed(self) -> None:
        from urbanlens.dashboard.services.gateway import GatewayRequestError

        with (
            mock.patch.object(GooglePhotosGateway, "list_session_media_items", side_effect=GatewayRequestError("boom")),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            counts = tasks.import_google_photos(self.pin.pk, self.profile.pk, "unknown-session", ["missing-item"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 1})

    def test_missing_account_is_a_noop(self) -> None:
        self.account.delete()
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_google_photos(self.pin.pk, self.profile.pk, "sess1", ["item1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})


# -- get_photos_account: self-heal on undecryptable tokens ---------------------


class GetPhotosAccountTests(TestCase):
    """get_photos_account heals accounts left with undecryptable tokens.

    Regression test for a production 500: rotating field_encryption_key
    without migrating old rows makes EncryptedTextField.from_db_value raise
    InvalidToken, which crashed every page that touched the Google Photos
    connection (e.g. GET /dashboard/settings/google-photos/).
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.account = GooglePhotosAccount.objects.create(profile=self.profile, access_token="a", refresh_token="r")

    def _corrupt_stored_access_token(self) -> None:
        """Write a ciphertext-shaped value directly to the DB that Fernet cannot decrypt."""
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_google_photos_accounts SET access_token = %s WHERE id = %s",
                ["not-a-valid-fernet-token", self.account.id],
            )

    def test_returns_account_when_decryptable(self) -> None:
        from urbanlens.dashboard.models.google_photos.model import get_photos_account

        self.assertEqual(get_photos_account(self.profile), self.account)

    def test_undecryptable_account_is_healed_to_none(self) -> None:
        from urbanlens.dashboard.models.google_photos.model import get_photos_account

        self._corrupt_stored_access_token()
        self.assertIsNone(get_photos_account(self.profile))
        self.assertFalse(GooglePhotosAccount.objects.filter(profile=self.profile).exists())

    def test_settings_view_does_not_500_on_undecryptable_account(self) -> None:
        self._corrupt_stored_access_token()
        self.client.force_login(self.user)
        response = self.client.get(reverse("settings.google_photos"))
        self.assertEqual(response.status_code, 200)
