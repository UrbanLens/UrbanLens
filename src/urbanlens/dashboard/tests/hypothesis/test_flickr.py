"""Tests for the Flickr photo-import integration.

Covers:
- oauth.py - request-token/access-token exchange (OAuth1Session mocked).
- FlickrGateway - per-request OAuth1 signing, geo search (no local distance
  filtering needed - Flickr filters server-side), error mapping.
- Settings connect/callback/disconnect views.
- PinFlickrSearchView - radius passthrough, "during my visits"/"all photos"
  mode branching, and already-imported flagging.
- import_flickr_photos task - new-photo happy path and checksum dedupe.

All HTTP/OAuth calls are mocked; no real network access occurs.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import hashlib
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.flickr.model import FlickrAccount
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.flickr import oauth as flickr_oauth
from urbanlens.dashboard.services.apis.flickr.gateway import FlickrGateway, FlickrPhoto
from urbanlens.dashboard.services.gateway import GatewayRequestError


def _mock_response(*, ok: bool = True, status_code: int = 200, json_data=None, content: bytes = b"", headers: dict | None = None):
    resp = mock.MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = content
    resp.headers = headers or {}
    resp.text = ""
    return resp


def _account(**kwargs) -> FlickrAccount:
    defaults = {"oauth_token": "token", "oauth_token_secret": "secret", "flickr_user_id": "12345@N00", "flickr_username": "tester"}
    defaults.update(kwargs)
    user = baker.make(User)
    return FlickrAccount(profile=user.profile, **defaults)


@mock.patch.object(flickr_oauth, "settings")
class FlickrOAuthFlowTests(TestCase):
    """start_authorization/finish_authorization wrap OAuth1Session's handshake."""

    def _configure_settings(self, mock_settings) -> None:
        mock_settings.flickr_api_key = "key"
        mock_settings.flickr_api_secret = "secret"

    def test_start_authorization_returns_pending_state(self, mock_settings) -> None:
        self._configure_settings(mock_settings)
        with mock.patch.object(flickr_oauth, "OAuth1Session") as session_cls:
            session_cls.return_value.fetch_request_token.return_value = {"oauth_token": "req-token", "oauth_token_secret": "req-secret"}
            session_cls.return_value.authorization_url.return_value = "https://www.flickr.com/services/oauth/authorize?oauth_token=req-token"

            pending = flickr_oauth.start_authorization("https://example.com/callback")

        self.assertEqual(pending.oauth_token, "req-token")
        self.assertEqual(pending.oauth_token_secret, "req-secret")
        self.assertIn("authorize", pending.authorization_url)

    def test_finish_authorization_returns_access_grant(self, mock_settings) -> None:
        self._configure_settings(mock_settings)
        with mock.patch.object(flickr_oauth, "OAuth1Session") as session_cls:
            session_cls.return_value.fetch_access_token.return_value = {
                "oauth_token": "final-token",
                "oauth_token_secret": "final-secret",
                "user_nsid": "12345@N00",
                "username": "tester",
            }
            grant = flickr_oauth.finish_authorization(oauth_token="req-token", oauth_token_secret="req-secret", oauth_verifier="verifier")

        self.assertEqual(grant.oauth_token, "final-token")
        self.assertEqual(grant.user_nsid, "12345@N00")

    def test_missing_consumer_credentials_raises(self, mock_settings) -> None:
        mock_settings.flickr_api_key = None
        mock_settings.flickr_api_secret = None
        with self.assertRaises(flickr_oauth.FlickrNotConfiguredError):
            flickr_oauth.start_authorization("https://example.com/callback")


# -- FlickrGateway ----------------------------------------------------------------


class FlickrGatewayTests(TestCase):
    """FlickrGateway signs requests and maps errors, and filters no distance locally."""

    def _gateway(self) -> FlickrGateway:
        gw = FlickrGateway(account=_account(), session=mock.MagicMock())
        return gw

    def test_search_near_parses_photos(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(
            json_data={
                "stat": "ok",
                "photos": {
                    "photo": [
                        {"id": "1", "url_s": "https://example.com/1_s.jpg", "url_o": "https://example.com/1_o.jpg", "latitude": "40.0", "longitude": "-74.0"},
                        {"id": "2", "url_s": "https://example.com/2_s.jpg", "latitude": "0", "longitude": "0"},
                    ],
                },
            },
        )
        with mock.patch("urbanlens.dashboard.services.apis.flickr.gateway._consumer_credentials", return_value=("key", "secret")):
            photos = gw.search_near(40.0, -74.0, 0.5)

        self.assertEqual(photos[0], FlickrPhoto(id="1", thumbnail_url="https://example.com/1_s.jpg", original_url="https://example.com/1_o.jpg", lat=40.0, lon=-74.0))
        # latitude/longitude of "0"/"0" mean "no geo" per Flickr's convention.
        self.assertIsNone(photos[1].lat)

    def test_flickr_error_status_raises(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(json_data={"stat": "fail", "message": "Invalid auth token"})
        with mock.patch("urbanlens.dashboard.services.apis.flickr.gateway._consumer_credentials", return_value=("key", "secret")), self.assertRaises(GatewayRequestError):
            gw.search_near(40.0, -74.0, 0.5)

    def test_http_error_status_raises(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(ok=False, status_code=500)
        with mock.patch("urbanlens.dashboard.services.apis.flickr.gateway._consumer_credentials", return_value=("key", "secret")), self.assertRaises(GatewayRequestError):
            gw.search_near(40.0, -74.0, 0.5)

    def test_get_original_uses_fallback_url_without_extra_call(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(content=b"jpeg-bytes", headers={"Content-Type": "image/jpeg"})
        content, filename, content_type = gw.get_original("1", fallback_url="https://example.com/1_o.jpg")
        self.assertEqual(content, b"jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")
        gw.session.get.assert_called_once()

    def test_search_by_dates_issues_one_call_per_date_and_dedupes(self) -> None:
        gw = self._gateway()
        gw.session.get.side_effect = [
            _mock_response(json_data={"stat": "ok", "photos": {"photo": [{"id": "1", "url_s": "https://example.com/1_s.jpg"}]}}),
            _mock_response(
                json_data={
                    "stat": "ok",
                    "photos": {"photo": [{"id": "1", "url_s": "https://example.com/1_s.jpg"}, {"id": "2", "url_s": "https://example.com/2_s.jpg"}]},
                },
            ),
        ]
        with mock.patch("urbanlens.dashboard.services.apis.flickr.gateway._consumer_credentials", return_value=("key", "secret")):
            results = gw.search_by_dates([datetime.date(2024, 1, 1), datetime.date(2024, 1, 2)])
        self.assertEqual(gw.session.get.call_count, 2)
        self.assertEqual({photo.id for photo in results}, {"1", "2"})

    def test_list_recent_sorts_by_date_taken_desc(self) -> None:
        gw = self._gateway()
        gw.session.get.return_value = _mock_response(json_data={"stat": "ok", "photos": {"photo": [{"id": "1", "url_s": "https://example.com/1_s.jpg"}]}})
        with mock.patch("urbanlens.dashboard.services.apis.flickr.gateway._consumer_credentials", return_value=("key", "secret")):
            results = gw.list_recent(limit=50)
        self.assertEqual([photo.id for photo in results], ["1"])
        _args, kwargs = gw.session.get.call_args
        self.assertEqual(kwargs["params"]["sort"], "date-taken-desc")
        self.assertEqual(kwargs["params"]["per_page"], "50")


# -- Settings: connect / disconnect -------------------------------------------


class FlickrSettingsViewTests(TestCase):
    """Connect/disconnect manage the stashed request-token cache and the FlickrAccount row."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_disconnect_removes_the_account(self) -> None:
        FlickrAccount.objects.create(profile=self.user.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00")
        self.client.post(reverse("settings.flickr.disconnect"))
        self.assertFalse(FlickrAccount.objects.filter(profile=self.user.profile).exists())

    def test_callback_with_bad_state_does_not_create_account(self) -> None:
        response = self.client.get(reverse("settings.flickr.callback"), {"oauth_token": "unknown", "oauth_verifier": "v"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FlickrAccount.objects.filter(profile=self.user.profile).exists())

    def test_callback_completes_with_stashed_request_token(self) -> None:
        cache.set("ul_flickr_request_token_req-token", {"secret": "req-secret", "pid": self.user.profile.id}, 600)
        with mock.patch(
            "urbanlens.dashboard.controllers.flickr.finish_authorization",
            return_value=flickr_oauth.FlickrAccessGrant(oauth_token="final", oauth_token_secret="final-secret", user_nsid="1@N00", username="tester"),
        ):
            response = self.client.get(reverse("settings.flickr.callback"), {"oauth_token": "req-token", "oauth_verifier": "v"})
        self.assertEqual(response.status_code, 302)
        account = FlickrAccount.objects.get(profile=self.user.profile)
        self.assertEqual(account.oauth_token, "final")
        self.assertEqual(account.flickr_user_id, "1@N00")


# -- Pin detail: search -------------------------------------------------------------


class PinFlickrSearchViewTests(TestCase):
    """Search passes the radius straight to Flickr's server-side geo filter."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = FlickrAccount.objects.create(profile=self.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00")

    def test_search_returns_photos_and_flags_already_imported(self) -> None:
        photo = FlickrPhoto(id="42", thumbnail_url="https://example.com/42_s.jpg", original_url="https://example.com/42_o.jpg", lat=40.0, lon=-74.0)
        baker.make(Image, pin=self.pin, profile=self.profile, source_url=self.account.photo_web_url("42"))
        with mock.patch.object(FlickrGateway, "search_near", return_value=[photo]):
            response = self.client.get(reverse("pin.flickr.search", args=[self.pin.slug]))
        self.assertTrue(response.context["assets"][0]["already_imported"])

    def test_no_account_renders_connect_prompt_without_calling_gateway(self) -> None:
        self.account.delete()
        with mock.patch.object(FlickrGateway, "search_near") as search_near:
            response = self.client.get(reverse("pin.flickr.search", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("account"))
        search_near.assert_not_called()

    def test_visits_mode_with_no_recorded_visits_skips_the_gateway(self) -> None:
        with mock.patch.object(FlickrGateway, "search_by_dates") as search_by_dates:
            response = self.client.get(reverse("pin.flickr.search", args=[self.pin.slug]), {"mode": "visits"})
        self.assertEqual(response.context["assets"], [])
        self.assertIn("No recorded visits", response.context["empty_message"])
        search_by_dates.assert_not_called()

    def test_visits_mode_searches_by_recorded_visit_dates(self) -> None:
        baker.make(PinVisit, pin=self.pin, visited_at=timezone.make_aware(datetime.datetime(2024, 1, 5)))
        photo = FlickrPhoto(id="v1", thumbnail_url="https://example.com/v1_s.jpg", original_url=None, lat=None, lon=None)
        with mock.patch.object(FlickrGateway, "search_by_dates", return_value=[photo]) as search_by_dates:
            response = self.client.get(reverse("pin.flickr.search", args=[self.pin.slug]), {"mode": "visits"})
        asset_ids = [a["id"] for a in response.context["assets"]]
        self.assertEqual(asset_ids, ["v1"])
        (dates_arg,), _kwargs = search_by_dates.call_args
        self.assertEqual(list(dates_arg), [datetime.date(2024, 1, 5)])

    def test_all_mode_calls_list_recent(self) -> None:
        photo = FlickrPhoto(id="r1", thumbnail_url="https://example.com/r1_s.jpg", original_url=None, lat=None, lon=None)
        with mock.patch.object(FlickrGateway, "list_recent", return_value=[photo]) as list_recent:
            response = self.client.get(reverse("pin.flickr.search", args=[self.pin.slug]), {"mode": "all"})
        asset_ids = [a["id"] for a in response.context["assets"]]
        self.assertEqual(asset_ids, ["r1"])
        list_recent.assert_called_once()


# -- Celery task: import_flickr_photos ----------------------------------------------


class ImportFlickrPhotosTaskTests(TestCase):
    """import_flickr_photos downloads, dedupes, and logs a visit per new photo."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = FlickrAccount.objects.create(profile=self.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00")

    def _run(self, photo_ids, downloads):
        def fake_get_original(self_gw, photo_id):
            return downloads[photo_id]

        with (
            mock.patch.object(FlickrGateway, "get_original", fake_get_original),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            return tasks.import_flickr_photos(self.pin.pk, self.profile.pk, photo_ids)

    def test_imports_a_new_photo_and_logs_a_visit(self) -> None:
        counts = self._run(["42"], {"42": (b"jpeg-bytes", "photo.jpg", "image/jpeg")})
        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertEqual(image.source_url, self.account.photo_web_url("42"))
        self.assertTrue(PinVisit.objects.filter(pin=self.pin, source=VisitSource.PHOTO).exists())

    def test_skips_photo_already_imported_by_checksum(self) -> None:
        content = b"already-here"
        checksum = hashlib.sha256(content).hexdigest()
        baker.make(Image, pin=self.pin, profile=self.profile, checksum=checksum)

        counts = self._run(["dup"], {"dup": (content, "photo.jpg", "image/jpeg")})

        self.assertEqual(counts, {"imported": 0, "skipped": 1, "failed": 0})

    def test_missing_account_is_a_noop(self) -> None:
        self.account.delete()
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_flickr_photos(self.pin.pk, self.profile.pk, ["42"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})


class GetFlickrAccountTests(TestCase):
    """FlickrAccountManager.get_for_profile() heals accounts left with undecryptable tokens.

    Unlike the equivalent Immich/GoogleCalendar/GooglePhotos lookups, nothing
    here ever caught InvalidToken before this - every page or task touching a
    Flickr connection after a field_encryption_key rotation would 500 outright
    instead of treating it as "never connected" and offering reconnection.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.account = FlickrAccount.objects.create(profile=self.profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00")

    def _corrupt_stored_oauth_token(self) -> None:
        """Write a ciphertext-shaped value directly to the DB that Fernet cannot decrypt."""
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE dashboard_flickr_accounts SET oauth_token = %s WHERE id = %s",
                ["not-a-valid-fernet-token", self.account.id],
            )

    def test_returns_account_when_decryptable(self) -> None:
        self.assertEqual(FlickrAccount.objects.get_for_profile(self.profile), self.account)

    def test_undecryptable_account_is_healed_to_none(self) -> None:
        self._corrupt_stored_oauth_token()
        self.assertIsNone(FlickrAccount.objects.get_for_profile(self.profile))
        self.assertFalse(FlickrAccount.objects.filter(profile=self.profile).exists())

    def test_settings_page_does_not_500_with_undecryptable_account(self) -> None:
        self._corrupt_stored_oauth_token()
        self.client.force_login(self.user)
        response = self.client.get(reverse("settings.flickr"))
        self.assertEqual(response.status_code, 200)


class FlickrAccountDeleteForProfileTests(TestCase):
    def test_removes_the_profiles_account(self) -> None:
        user = baker.make(User)
        profile = user.profile
        FlickrAccount.objects.create(profile=profile, oauth_token="t", oauth_token_secret="s", flickr_user_id="1@N00")

        FlickrAccount.objects.delete_for_profile(profile)

        self.assertFalse(FlickrAccount.objects.filter(profile=profile).exists())

    def test_noop_when_the_profile_has_no_account(self) -> None:
        user = baker.make(User)
        profile = user.profile
        FlickrAccount.objects.delete_for_profile(profile)  # must not raise
        self.assertFalse(FlickrAccount.objects.filter(profile=profile).exists())
