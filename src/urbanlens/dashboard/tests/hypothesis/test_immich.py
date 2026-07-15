"""Tests for the Immich photo-import integration.

Covers:
- EncryptedTextField - values round-trip through encrypt/decrypt, and the raw
  DB-stored value is not the plaintext (property-based).
- ImmichGateway - auth header, map-marker parsing, GatewayRequestError on
  failure. All HTTP calls are mocked; no real network access occurs.
- ImmichSettingsView / ImmichDisconnectView - connect only persists a
  credential that ping() verifies; disconnect removes it.
- PinImmichSearchView - distance filtering, "during my visits"/"all photos"
  mode branching, and already-imported flagging.
- import_immich_photos task - creates Image + PinVisit for a new asset,
  skips a duplicate checksum, skips an over-quota asset, without failing the
  rest of the batch, and (when called with visit_id_by_asset, as
  accept_pin_suggestion does) attaches to that visit instead of creating a
  redundant one of its own.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
import hashlib
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.fields import EncryptedTextField
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.immich.gateway import GatewayRequestError, ImmichGateway, MapMarker, SearchAsset

_db_settings = settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def _mock_response(*, ok: bool = True, status_code: int = 200, json_data=None, content: bytes = b"", headers: dict | None = None):
    resp = mock.MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = content
    resp.headers = headers or {}
    resp.text = ""
    return resp


# -- EncryptedTextField ---------------------------------------------------------


class EncryptedTextFieldTests(TestCase):
    """Values survive a get_prep_value/from_db_value round trip and are not stored as plaintext."""

    @given(value=st.text(min_size=1, max_size=200))
    @_db_settings
    def test_round_trips_through_encrypt_decrypt(self, value: str) -> None:
        field = EncryptedTextField()
        stored = field.get_prep_value(value)
        self.assertEqual(field.from_db_value(stored, None, None), value)

    @given(value=st.text(min_size=1, max_size=200))
    @_db_settings
    def test_stored_value_is_not_plaintext(self, value: str) -> None:
        field = EncryptedTextField()
        stored = field.get_prep_value(value)
        self.assertNotEqual(stored, value)

    def test_blank_values_pass_through_unchanged(self) -> None:
        field = EncryptedTextField()
        self.assertIsNone(field.get_prep_value(None))
        self.assertEqual(field.get_prep_value(""), "")
        self.assertIsNone(field.from_db_value(None, None, None))

    def test_immich_account_api_key_is_encrypted_at_rest(self) -> None:
        from django.db import connection

        user = baker.make(User)
        account = ImmichAccount.objects.create(profile=user.profile, server_url="https://photos.example.com", api_key="s3cret-key")

        # Bypass the ORM's from_db_value conversion to see the actual stored bytes.
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT api_key FROM {ImmichAccount._meta.db_table} WHERE id = %s", [account.pk])
            (raw_value,) = cursor.fetchone()
        self.assertNotEqual(raw_value, "s3cret-key")

        account.refresh_from_db()
        self.assertEqual(account.api_key, "s3cret-key")

    def test_key_is_derived_from_djangos_stable_secret_key_not_appsettings(self) -> None:
        """Regression test: the Fernet key must come from Django's SECRET_KEY.

        AppSettings.secret_key (a separate pydantic field, env var UL_SECRET_KEY)
        has no wired env var in any deployment of this app and silently falls
        back to a fresh random value in every process - using it here meant
        every gunicorn worker/Celery worker/manage.py run derived a *different*
        key, so anything encrypted by one process was undecryptable by any
        other (see the ImmichAccountManagerTests below for the user-facing
        fallout). Django's SECRET_KEY (DJANGO_SECRET_KEY) is the one secret
        every deployment actually configures consistently.
        """
        import base64
        import hashlib

        from cryptography.fernet import Fernet
        from django.conf import settings as django_settings

        token = EncryptedTextField().get_prep_value("probe-value")
        derived = hashlib.sha256(django_settings.SECRET_KEY.encode()).digest()
        direct_fernet = Fernet(base64.urlsafe_b64encode(derived))
        self.assertEqual(direct_fernet.decrypt(token.encode()).decode(), "probe-value")


# -- ImmichGateway ----------------------------------------------------------------


def _account(**kwargs) -> ImmichAccount:
    defaults = {"server_url": "https://photos.example.com", "api_key": "test-key"}
    defaults.update(kwargs)
    user = baker.make(User)
    return ImmichAccount(profile=user.profile, **defaults)


class ImmichGatewayTests(TestCase):
    """ImmichGateway sends the API key header and maps failures to GatewayRequestError."""

    def test_ping_true_on_success(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(json_data={"res": "pong"})
        self.assertTrue(gw.ping())

    def test_ping_false_on_failure(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(ok=False, status_code=401)
        self.assertFalse(gw.ping())

    def test_requests_include_api_key_header(self) -> None:
        gw = ImmichGateway(account=_account(api_key="my-secret"), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(json_data={"res": "pong"})
        gw.ping()
        _args, kwargs = gw.session.get.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "my-secret")

    def test_get_map_markers_parses_geolocated_assets_only(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(
            json_data=[
                {"id": "a1", "lat": 40.0, "lon": -74.0, "city": "Newark"},
                {"id": "a2", "lat": None, "lon": None},
            ],
        )
        markers = gw.get_map_markers()
        self.assertEqual(markers, [MapMarker(id="a1", lat=40.0, lon=-74.0, city="Newark")])

    def test_get_map_markers_raises_on_error_status(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(ok=False, status_code=500)
        with self.assertRaises(GatewayRequestError):
            gw.get_map_markers()

    def test_get_asset_original_returns_bytes_filename_and_content_type(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.get.return_value = _mock_response(content=b"jpegdata", headers={"Content-Type": "image/jpeg", "Content-Disposition": 'attachment; filename="photo.jpg"'})
        content, filename, content_type = gw.get_asset_original("a1")
        self.assertEqual(content, b"jpegdata")
        self.assertEqual(filename, "photo.jpg")
        self.assertEqual(content_type, "image/jpeg")

    def test_search_by_dates_issues_one_call_per_date_and_dedupes(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.side_effect = [
            _mock_response(json_data={"assets": {"items": [{"id": "a1", "fileCreatedAt": "2024-01-01T12:00:00+00:00"}]}}),
            _mock_response(
                json_data={
                    "assets": {
                        "items": [
                            {"id": "a1", "fileCreatedAt": "2024-01-01T12:00:00+00:00"},
                            {"id": "a2", "fileCreatedAt": "2024-01-02T09:00:00+00:00"},
                        ],
                    },
                },
            ),
        ]
        results = gw.search_by_dates([datetime.date(2024, 1, 1), datetime.date(2024, 1, 2)])
        self.assertEqual(gw.session.post.call_count, 2)
        self.assertEqual({asset.id for asset in results}, {"a1", "a2"})

    def test_list_recent_sends_no_date_filter(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={"assets": {"items": [{"id": "a1"}]}})
        results = gw.list_recent(limit=10)
        self.assertEqual([asset.id for asset in results], ["a1"])
        _args, kwargs = gw.session.post.call_args
        self.assertEqual(kwargs["json"], {"size": 10, "withExif": True})

    def test_search_metadata_parses_gps_and_city_from_exif_info(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(
            json_data={"assets": {"items": [{"id": "a1", "exifInfo": {"latitude": 40.5, "longitude": -74.5, "city": "Newark", "dateTimeOriginal": "2024-01-01T12:00:00+00:00"}}]}},
        )
        (asset,) = gw.list_recent()
        self.assertEqual(asset.lat, 40.5)
        self.assertEqual(asset.lon, -74.5)
        self.assertEqual(asset.city, "Newark")
        self.assertEqual(asset.taken_at, datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.UTC))

    def test_search_metadata_handles_assets_with_no_gps(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={"assets": {"items": [{"id": "a1"}]}})
        (asset,) = gw.list_recent()
        self.assertIsNone(asset.lat)
        self.assertIsNone(asset.lon)
        self.assertIsNone(asset.city)

    def test_iter_library_assets_pages_until_no_next_page(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.side_effect = [
            _mock_response(json_data={"assets": {"items": [{"id": "a1", "exifInfo": {"latitude": 1.0, "longitude": 2.0}}], "nextPage": "2", "total": 2}}),
            _mock_response(json_data={"assets": {"items": [{"id": "a2", "exifInfo": {"latitude": 3.0, "longitude": 4.0}}], "nextPage": None, "total": 2}}),
        ]
        pages = list(gw.iter_library_assets(page_size=1))
        self.assertEqual(gw.session.post.call_count, 2)
        self.assertEqual([[asset.id for asset in page] for page, _total in pages], [["a1"], ["a2"]])
        self.assertEqual(pages[0][1], 2)
        # Second page's request should carry the token from the first page's nextPage.
        _args, kwargs = gw.session.post.call_args_list[1]
        self.assertEqual(kwargs["json"]["page"], "2")

    def test_iter_library_assets_stops_when_first_page_is_empty(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={"assets": {"items": [], "nextPage": None, "total": 0}})
        pages = list(gw.iter_library_assets())
        self.assertEqual(pages, [])

    def test_metadata_search_requests_exif_data(self) -> None:
        """Without withExif=True, Immich omits exifInfo entirely and every GPS coordinate parses as None."""
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={"assets": {"items": []}})
        list(gw.iter_library_assets())
        _args, kwargs = gw.session.post.call_args
        self.assertTrue(kwargs["json"]["withExif"])

    def test_library_asset_count_reads_search_statistics(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={"total": 194000})
        self.assertEqual(gw.library_asset_count(), 194000)
        args, _kwargs = gw.session.post.call_args
        self.assertTrue(args[0].endswith("/search/statistics"))

    def test_library_asset_count_defaults_to_zero_when_missing(self) -> None:
        gw = ImmichGateway(account=_account(), session=mock.MagicMock())
        gw.session.post.return_value = _mock_response(json_data={})
        self.assertEqual(gw.library_asset_count(), 0)


# -- Settings: connect / disconnect ------------------------------------------------


class ImmichSettingsViewTests(TestCase):
    """Connect only persists credentials that ping() actually verifies."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_valid_credentials_are_saved(self) -> None:
        with mock.patch.object(ImmichGateway, "ping", return_value=True):
            response = self.client.post(reverse("settings.immich"), {"server_url": "https://photos.example.com", "api_key": "good-key"})
        self.assertEqual(response.status_code, 200)
        account = ImmichAccount.objects.get(profile=self.user.profile)
        self.assertEqual(account.server_url, "https://photos.example.com")
        self.assertEqual(account.api_key, "good-key")

    def test_invalid_credentials_are_rejected_and_not_saved(self) -> None:
        with mock.patch.object(ImmichGateway, "ping", return_value=False):
            self.client.post(reverse("settings.immich"), {"server_url": "https://photos.example.com", "api_key": "bad-key"})
        self.assertFalse(ImmichAccount.objects.filter(profile=self.user.profile).exists())

    def test_disconnect_removes_the_account(self) -> None:
        ImmichAccount.objects.create(profile=self.user.profile, server_url="https://photos.example.com", api_key="k")
        self.client.post(reverse("settings.immich.disconnect"))
        self.assertFalse(ImmichAccount.objects.filter(profile=self.user.profile).exists())


def _corrupt_api_key(account: ImmichAccount) -> None:
    """Overwrite a stored api_key with ciphertext that will never decrypt.

    Simulates the real-world failure mode (a field-encryption-key change)
    without needing to actually swap keys process-wide.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(f"UPDATE {ImmichAccount._meta.db_table} SET api_key = %s WHERE id = %s", ["not-a-valid-fernet-token", account.pk])  # noqa: S608 - table name from Django _meta, not user input


class ImmichAccountManagerTests(TestCase):
    """get_for_profile/delete_for_profile self-heal instead of raising InvalidToken."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")
        _corrupt_api_key(self.account)

    def test_get_for_profile_returns_none_instead_of_raising(self) -> None:
        self.assertIsNone(ImmichAccount.objects.get_for_profile(self.profile))

    def test_get_for_profile_clears_the_undecryptable_row(self) -> None:
        ImmichAccount.objects.get_for_profile(self.profile)
        self.assertFalse(ImmichAccount.objects.filter(profile=self.profile).exists())

    def test_get_for_profile_is_a_noop_for_a_healthy_account(self) -> None:
        healthy = ImmichAccount.objects.create(profile=baker.make(User).profile, server_url="https://photos.example.com", api_key="fine")
        result = ImmichAccount.objects.get_for_profile(healthy.profile)
        self.assertEqual(result, healthy)

    def test_delete_for_profile_does_not_raise(self) -> None:
        ImmichAccount.objects.delete_for_profile(self.profile)
        self.assertFalse(ImmichAccount.objects.filter(profile=self.profile).exists())

    def test_settings_page_does_not_500_with_a_corrupted_account(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(reverse("settings.immich"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ImmichAccount.objects.filter(profile=self.profile).exists())

    def test_pin_search_does_not_500_with_a_corrupted_account(self) -> None:
        self.client.force_login(self.user)
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        response = self.client.get(reverse("pin.immich.search", args=[pin.slug]))
        self.assertEqual(response.status_code, 200)

    def test_thumbnail_view_404s_instead_of_500ing(self) -> None:
        self.client.force_login(self.user)
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        response = self.client.get(reverse("pin.immich.thumbnail", args=[pin.slug, "asset-1"]))
        self.assertEqual(response.status_code, 404)


class ImmichLibraryScanStartViewTests(TestCase):
    """The scan trigger requires a connected account and enqueues the sweep task."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def test_requires_a_connected_account(self) -> None:
        response = self.client.post(reverse("settings.immich.scan"))
        self.assertEqual(response.status_code, 400)

    def test_enqueues_the_sweep_task_when_connected(self) -> None:
        ImmichAccount.objects.create(profile=self.user.profile, server_url="https://photos.example.com", api_key="k")
        fake_result = mock.MagicMock(id="task-123")
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task", return_value=fake_result) as enqueue:
            response = self.client.post(reverse("settings.immich.scan"))
        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once()
        self.assertIn(b"task-123", response.content)

    def test_visit_tracking_disabled_is_400_and_never_enqueues(self) -> None:
        ImmichAccount.objects.create(profile=self.user.profile, server_url="https://photos.example.com", api_key="k")
        self.user.profile.track_pin_visits = False
        self.user.profile.save(update_fields=["track_pin_visits"])
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task") as enqueue:
            response = self.client.post(reverse("settings.immich.scan"))
        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()


class ImmichLibraryScanResumeTests(TestCase):
    """The active scan task id survives navigation, so a page reload can resume polling
    instead of only ever offering a fresh "start scan" button (see get_active_scan_task_id)."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")
        self.client.force_login(self.user)

    def test_starting_a_scan_persists_the_task_id(self) -> None:
        from urbanlens.dashboard.controllers.immich import get_active_scan_task_id

        fake_result = mock.MagicMock(id="task-123")
        with mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task", return_value=fake_result):
            self.client.post(reverse("settings.immich.scan"))
        self.assertEqual(get_active_scan_task_id(self.profile.pk), "task-123")

    def test_tools_page_resumes_polling_an_active_scan(self) -> None:
        from urbanlens.dashboard.controllers.immich import _set_active_scan_task_id

        _set_active_scan_task_id(self.profile.pk, "task-123")
        response = self.client.get(reverse("tools.index"))
        self.assertContains(response, "task-123")

    def test_tools_page_shows_bare_button_with_no_active_scan(self) -> None:
        response = self.client.get(reverse("tools.index"))
        self.assertNotContains(response, "settings.immich.scan.progress")

    def test_completed_scan_clears_the_persisted_task_id(self) -> None:
        from urbanlens.dashboard.controllers.immich import _set_active_scan_task_id, get_active_scan_task_id

        _set_active_scan_task_id(self.profile.pk, "task-123")
        with mock.patch("urbanlens.dashboard.controllers.immich.get_task_progress") as get_progress:
            get_progress.return_value = mock.MagicMock(state="SUCCESS", percent=100, message="Done", error="", result={"scanned": 5, "matched_suggestions": 0, "new_pin_suggestions": 0})
            self.client.get(reverse("settings.immich.scan.progress", args=["task-123"]))
        self.assertIsNone(get_active_scan_task_id(self.profile.pk))

    def test_failed_scan_clears_the_persisted_task_id(self) -> None:
        from urbanlens.dashboard.controllers.immich import _set_active_scan_task_id, get_active_scan_task_id

        _set_active_scan_task_id(self.profile.pk, "task-123")
        with mock.patch("urbanlens.dashboard.controllers.immich.get_task_progress") as get_progress:
            get_progress.return_value = mock.MagicMock(state="FAILURE", percent=0, message="", error="boom", result=None)
            self.client.get(reverse("settings.immich.scan.progress", args=["task-123"]))
        self.assertIsNone(get_active_scan_task_id(self.profile.pk))


# -- Pin detail: search -------------------------------------------------------------


class PinImmichSearchViewTests(TestCase):
    """Search filters map markers to the requested radius and flags already-imported assets."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def test_only_markers_within_radius_are_returned(self) -> None:
        # ~11m north and ~1.1km north of the pin, respectively.
        near = MapMarker(id="near", lat=40.0001, lon=-74.0)
        far = MapMarker(id="far", lat=40.01, lon=-74.0)
        with mock.patch.object(ImmichGateway, "get_map_markers", return_value=[near, far]):
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]), {"radius_m": "500"})
        asset_ids = [a["id"] for a in response.context["assets"]]
        self.assertEqual(asset_ids, ["near"])

    def test_already_imported_asset_is_flagged(self) -> None:
        marker = MapMarker(id="dup", lat=40.0, lon=-74.0)
        baker.make(Image, pin=self.pin, profile=self.profile, source_url=self.account.asset_web_url("dup"))
        with mock.patch.object(ImmichGateway, "get_map_markers", return_value=[marker]):
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]))
        self.assertTrue(response.context["assets"][0]["already_imported"])

    def test_no_account_renders_connect_prompt_without_calling_gateway(self) -> None:
        self.account.delete()
        with mock.patch.object(ImmichGateway, "get_map_markers") as get_markers:
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context.get("account"))
        get_markers.assert_not_called()

    def test_visits_mode_with_no_recorded_visits_skips_the_gateway(self) -> None:
        with mock.patch.object(ImmichGateway, "search_by_dates") as search_by_dates:
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]), {"mode": "visits"})
        self.assertEqual(response.context["assets"], [])
        self.assertIn("No recorded visits", response.context["empty_message"])
        search_by_dates.assert_not_called()

    def test_visits_mode_searches_by_recorded_visit_dates(self) -> None:
        baker.make(PinVisit, pin=self.pin, visited_at=timezone.make_aware(datetime.datetime(2024, 1, 5)))
        asset = SearchAsset(id="v1")
        with mock.patch.object(ImmichGateway, "search_by_dates", return_value=[asset]) as search_by_dates:
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]), {"mode": "visits"})
        asset_ids = [a["id"] for a in response.context["assets"]]
        self.assertEqual(asset_ids, ["v1"])
        (dates_arg,), _kwargs = search_by_dates.call_args
        self.assertEqual(list(dates_arg), [datetime.date(2024, 1, 5)])

    def test_all_mode_calls_list_recent(self) -> None:
        asset = SearchAsset(id="r1")
        with mock.patch.object(ImmichGateway, "list_recent", return_value=[asset]) as list_recent:
            response = self.client.get(reverse("pin.immich.search", args=[self.pin.slug]), {"mode": "all"})
        asset_ids = [a["id"] for a in response.context["assets"]]
        self.assertEqual(asset_ids, ["r1"])
        list_recent.assert_called_once()


# -- Celery task: import_immich_photos ----------------------------------------------


class ImportImmichPhotosTaskTests(TestCase):
    """import_immich_photos downloads, dedupes, quota-checks, and logs a visit per new asset."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def _run(self, asset_ids, downloads, visit_id_by_asset=None):
        """Run the task with get_asset_original mapped per asset id from `downloads`."""

        def fake_get_asset_original(self_gw, asset_id):
            return downloads[asset_id]

        with (
            mock.patch.object(ImmichGateway, "get_asset_original", fake_get_asset_original),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            return tasks.import_immich_photos(self.pin.pk, self.profile.pk, asset_ids, visit_id_by_asset)

    def test_imports_a_new_asset_and_logs_a_visit(self) -> None:
        counts = self._run(["a1"], {"a1": (b"jpeg-bytes", "photo.jpg", "image/jpeg")})
        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertEqual(image.source_url, self.account.asset_web_url("a1"))
        self.assertTrue(PinVisit.objects.filter(pin=self.pin, source=VisitSource.PHOTO).exists())

    def test_skips_asset_already_imported_by_checksum(self) -> None:
        content = b"already-here"
        checksum = hashlib.sha256(content).hexdigest()
        baker.make(Image, pin=self.pin, profile=self.profile, checksum=checksum)

        counts = self._run(["dup"], {"dup": (content, "photo.jpg", "image/jpeg")})

        self.assertEqual(counts, {"imported": 0, "skipped": 1, "failed": 0})
        self.assertEqual(Image.objects.filter(pin=self.pin, profile=self.profile).count(), 1)

    def test_over_quota_asset_is_skipped_without_failing_the_batch(self) -> None:
        # First call (asset "ok") is admitted; second ("too_big") exceeds quota.
        with mock.patch("urbanlens.dashboard.services.storage.quota_error_for_upload", side_effect=[None, "Storage quota exceeded."]):
            counts = self._run(
                ["ok", "too_big"],
                {"ok": (b"small", "a.jpg", "image/jpeg"), "too_big": (b"huge", "b.jpg", "image/jpeg")},
            )
        self.assertEqual(counts, {"imported": 1, "failed": 1, "skipped": 0})

    def test_visit_id_by_asset_attaches_to_that_visit_without_creating_another(self) -> None:
        """Regression test for accept_pin_suggestion's photo-import wiring (services/pin_suggestions.py)."""
        target_visit = baker.make_recipe("dashboard.pin_visit", pin=self.pin, source=VisitSource.HISTORY)
        before = PinVisit.objects.filter(pin=self.pin).count()

        counts = self._run(["a1"], {"a1": (b"jpeg-bytes", "photo.jpg", "image/jpeg")}, visit_id_by_asset={"a1": target_visit.pk})

        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertEqual(image.visit_id, target_visit.pk)
        self.assertEqual(PinVisit.objects.filter(pin=self.pin).count(), before)

    def test_asset_not_in_visit_id_by_asset_falls_back_to_its_own_visit(self) -> None:
        counts = self._run(["a1"], {"a1": (b"jpeg-bytes", "photo.jpg", "image/jpeg")}, visit_id_by_asset={"other-asset": 999})
        self.assertEqual(counts, {"imported": 1, "skipped": 0, "failed": 0})
        image = Image.objects.get(pin=self.pin, profile=self.profile)
        self.assertIsNotNone(image.visit_id)
        self.assertTrue(PinVisit.objects.filter(pk=image.visit_id, source=VisitSource.PHOTO).exists())

    def test_missing_account_is_a_noop(self) -> None:
        self.account.delete()
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_immich_photos(self.pin.pk, self.profile.pk, ["a1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})

    def test_undecryptable_account_is_a_noop_not_a_crash(self) -> None:
        """Regression test for the InvalidToken crash seen in production (see tasks.py)."""
        _corrupt_api_key(self.account)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_immich_photos(self.pin.pk, self.profile.pk, ["a1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})


# -- Celery task: sweep_immich_library_locations ------------------------------------


class SweepImmichLibraryLocationsTaskTests(TestCase):
    """The full-library sweep never downloads photos - only matches/clusters GPS+dates."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def _run(self, pages):
        with (
            mock.patch.object(ImmichGateway, "iter_library_assets", return_value=iter(pages)),
            mock.patch.object(ImmichGateway, "library_asset_count", return_value=sum(len(page) for page, _total in pages)),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            return tasks.sweep_immich_library_locations(self.profile.pk)

    def test_sweep_matches_a_geotagged_asset_against_an_existing_pin(self) -> None:
        from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion

        asset = SearchAsset(id="a1", taken_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC), lat=40.0001, lon=-74.0)
        result = self._run([([asset], 1)])
        self.assertEqual(result, {"scanned": 1, "matched_suggestions": 1, "new_pin_suggestions": 0})
        suggestion = PinSuggestion.objects.get()
        self.assertEqual(suggestion.pin_id, self.pin.pk)
        self.assertEqual(suggestion.sample_assets, [{"asset_id": "a1", "taken_at": "2024-01-01"}])

    def test_sweep_skips_assets_without_gps_or_date(self) -> None:
        from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion

        no_gps = SearchAsset(id="a1", taken_at=timezone.now())
        no_date = SearchAsset(id="a2", lat=40.0001, lon=-74.0)
        result = self._run([([no_gps, no_date], 2)])
        self.assertEqual(result["scanned"], 2)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_sweep_creates_notification_only_when_suggestions_are_found(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationLog

        self._run([([], 0)])
        self.assertFalse(NotificationLog.objects.filter(profile=self.profile).exists())

        asset = SearchAsset(id="a1", taken_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC), lat=40.0001, lon=-74.0)
        self._run([([asset], 1)])
        self.assertTrue(NotificationLog.objects.filter(profile=self.profile).exists())

    def test_sweep_handles_gateway_failure_gracefully(self) -> None:
        def failing_iter(self_gw, **kwargs):
            yield [SearchAsset(id="a1", taken_at=timezone.now(), lat=1.0, lon=2.0)], 5
            raise GatewayRequestError("boom")

        with (
            mock.patch.object(ImmichGateway, "iter_library_assets", failing_iter),
            mock.patch.object(ImmichGateway, "library_asset_count", return_value=5),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            result = tasks.sweep_immich_library_locations(self.profile.pk)
        self.assertEqual(result["scanned"], 1)

    def test_missing_account_is_a_noop(self) -> None:
        self.account.delete()
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            result = tasks.sweep_immich_library_locations(self.profile.pk)
        self.assertEqual(result, {"scanned": 0, "matched_suggestions": 0, "new_pin_suggestions": 0})

    def test_visit_tracking_disabled_skips_the_gateway_entirely(self) -> None:
        from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion

        self.profile.track_pin_visits = False
        self.profile.save(update_fields=["track_pin_visits"])
        with (
            mock.patch.object(ImmichGateway, "iter_library_assets") as iter_assets,
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            result = tasks.sweep_immich_library_locations(self.profile.pk)
        iter_assets.assert_not_called()
        self.assertEqual(result, {"scanned": 0, "matched_suggestions": 0, "new_pin_suggestions": 0})
        self.assertFalse(PinSuggestion.objects.exists())

    def test_progress_message_uses_the_real_total_not_the_deprecated_page_total(self) -> None:
        """Regression test: Immich's per-page 'total' field mirrors the page size (a
        deprecated field), so a naive "Scanned N of <page total>" message goes stale
        and nonsensical once scanned outgrows a single page (e.g. "Scanned 194000 of
        1000"). The real library-wide count must come from library_asset_count()."""
        asset = SearchAsset(id="a1", taken_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC), lat=40.0001, lon=-74.0)
        with (
            mock.patch.object(ImmichGateway, "iter_library_assets", return_value=iter([([asset], 1000)])),
            mock.patch.object(ImmichGateway, "library_asset_count", return_value=194000),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            tasks.sweep_immich_library_locations(self.profile.pk)
        messages = [call.kwargs["message"] for call in progress.call_args_list]
        self.assertTrue(any("194000" in message for message in messages))
        self.assertFalse(any("of 1000" in message for message in messages))

    def test_progress_message_falls_back_gracefully_when_statistics_endpoint_fails(self) -> None:
        asset = SearchAsset(id="a1", taken_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC), lat=40.0001, lon=-74.0)
        with (
            mock.patch.object(ImmichGateway, "iter_library_assets", return_value=iter([([asset], 1000)])),
            mock.patch.object(ImmichGateway, "library_asset_count", side_effect=GatewayRequestError("boom")),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress") as progress,
        ):
            result = tasks.sweep_immich_library_locations(self.profile.pk)
        self.assertEqual(result["scanned"], 1)
        messages = [call.kwargs["message"] for call in progress.call_args_list]
        self.assertTrue(any("so far" in message for message in messages))

    def test_undecryptable_account_is_a_noop_not_a_crash(self) -> None:
        """Regression test for the InvalidToken crash seen in production (see tasks.py).

        This is the exact traceback reported: sweep_immich_library_locations
        raised cryptography.fernet.InvalidToken instead of failing gracefully,
        because the account had been connected under a since-invalidated
        per-process encryption key (see EncryptedTextField / _fernet()).
        """
        _corrupt_api_key(self.account)
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            result = tasks.sweep_immich_library_locations(self.profile.pk)
        self.assertEqual(result, {"scanned": 0, "matched_suggestions": 0, "new_pin_suggestions": 0})
