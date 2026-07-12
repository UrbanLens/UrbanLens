"""Tests for the Immich photo-import integration.

Covers:
- EncryptedTextField - values round-trip through encrypt/decrypt, and the raw
  DB-stored value is not the plaintext (property-based).
- ImmichGateway - auth header, map-marker parsing, GatewayRequestError on
  failure. All HTTP calls are mocked; no real network access occurs.
- ImmichSettingsView / ImmichDisconnectView - connect only persists a
  credential that ping() verifies; disconnect removes it.
- PinImmichSearchView - distance filtering and already-imported flagging.
- import_immich_photos task - creates Image + PinVisit for a new asset,
  skips a duplicate checksum, skips an over-quota asset, without failing the
  rest of the batch.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.fields import EncryptedTextField
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.apis.immich.gateway import GatewayRequestError, ImmichGateway, MapMarker

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
            cursor.execute(f"SELECT api_key FROM {ImmichAccount._meta.db_table} WHERE id = %s", [account.pk])  # noqa: SLF001
            (raw_value,) = cursor.fetchone()
        self.assertNotEqual(raw_value, "s3cret-key")

        account.refresh_from_db()
        self.assertEqual(account.api_key, "s3cret-key")


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


# -- Celery task: import_immich_photos ----------------------------------------------


class ImportImmichPhotosTaskTests(TestCase):
    """import_immich_photos downloads, dedupes, quota-checks, and logs a visit per new asset."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude=Decimal("40.000000"), longitude=Decimal("-74.000000"))
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)
        self.account = ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")

    def _run(self, asset_ids, downloads):
        """Run the task with get_asset_original mapped per asset id from `downloads`."""

        def fake_get_asset_original(self_gw, asset_id):  # noqa: ARG001
            return downloads[asset_id]

        with (
            mock.patch.object(ImmichGateway, "get_asset_original", fake_get_asset_original),
            mock.patch("urbanlens.dashboard.tasks.update_task_progress"),
        ):
            return tasks.import_immich_photos(self.pin.pk, self.profile.pk, asset_ids)

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

    def test_missing_account_is_a_noop(self) -> None:
        self.account.delete()
        with mock.patch("urbanlens.dashboard.tasks.update_task_progress"):
            counts = tasks.import_immich_photos(self.pin.pk, self.profile.pk, ["a1"])
        self.assertEqual(counts, {"imported": 0, "skipped": 0, "failed": 0})
