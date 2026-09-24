"""Two uploads by one profile at once must behave as if they ran one after the other.

Each test holds the first writer inside its critical section, just before the ``Image`` insert,
until the second call has finished or ``_HOLD_SECONDS`` pass. Without a real per-profile critical
section the second call reads the quota, the day's external-media total and the checksums while the
first row is still unwritten, and both land. With one, the second call waits for the first to commit
and then sees its row.
"""

from __future__ import annotations

import io
import threading
from typing import Self
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.media.storage import GIB, get_storage_used_bytes

_HOLD_SECONDS = 1.5
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
_FAKE_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (1, 1), color).save(buffer, format="PNG")
    return buffer.getvalue()


_RED = _png((255, 0, 0))
_BLUE = _png((0, 0, 255))


class _FirstWriterHeld:
    """Patches ``Image.save`` so the first insert waits for the other thread to finish."""

    def __init__(self) -> None:
        self.first_inside = threading.Event()
        self.second_done = threading.Event()
        self._guard = threading.Lock()
        self._first: int | None = None
        self._original = Image.save

    def __enter__(self) -> Self:
        original = self._original
        held = self

        def save(instance: Image, *args: object, **kwargs: object) -> None:
            if instance.pk is None:
                with held._guard:
                    first = held._first is None
                    if first:
                        held._first = threading.get_ident()
                if first:
                    held.first_inside.set()
                    held.second_done.wait(_HOLD_SECONDS)
            original(instance, *args, **kwargs)

        self._patch = mock.patch.object(Image, "save", save)
        self._patch.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()


@override_settings(CACHES=_LOCMEM)
class _RaceCase(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        enqueue = mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        enqueue.start()
        self.addCleanup(enqueue.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile

    def race(self, first, second) -> tuple[list[object], list[BaseException]]:
        """Run *first* until it is about to insert, then *second* to completion, then let *first* finish.

        Returns:
            The two calls' return values (in call order, missing when a call raised) and what they raised.
        """
        results: list[object] = []
        errors: list[BaseException] = []

        def run(fn, done: threading.Event | None = None) -> threading.Thread:
            def inner() -> None:
                try:
                    results.append(fn())
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    connections.close_all()
                    if done is not None:
                        done.set()

            thread = threading.Thread(target=inner)
            thread.start()
            return thread

        with _FirstWriterHeld() as held:
            first_thread = run(first)
            self.assertTrue(held.first_inside.wait(10), "the first call never reached its insert")
            second_thread = run(second, held.second_done)
            first_thread.join(30)
            second_thread.join(30)
        self.assertFalse(first_thread.is_alive() or second_thread.is_alive(), "a racing upload hung")
        return results, errors

    def set_quota_gb(self, gigabytes: int) -> None:
        site = SiteSettings.get_current()
        site.storage_quota_gb = gigabytes
        site.save()

    def fill_to(self, remaining: int) -> None:
        """Leave the profile *remaining* bytes short of a 1 GiB quota."""
        self.set_quota_gb(1)
        baker.make(Image, profile=self.profile, file_size=GIB - remaining)


class ParallelUploadQuotaTests(_RaceCase):
    """G3-21 / G5-19: the quota check and the insert were never one critical section."""

    def test_two_parallel_uploads_cannot_both_fit_where_one_does(self) -> None:
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo

        size = len(_RED)
        self.fill_to(size + size // 2)

        results, errors = self.race(
            lambda: upload_photo(self.profile, SimpleUploadedFile("a.png", _RED, content_type="image/png")),
            lambda: upload_photo(self.profile, SimpleUploadedFile("b.png", _BLUE, content_type="image/png")),
        )

        self.assertEqual(len(results), 1, f"expected exactly one upload to be admitted, got {results} / {errors}")
        self.assertEqual([(type(e), getattr(e, "status", None)) for e in errors], [(PhotoUploadError, 413)])
        self.assertLessEqual(get_storage_used_bytes(self.profile), GIB, "parallel uploads overran the quota")


class ParallelDuplicateUploadTests(_RaceCase):
    """G3-23 / G3-30: the checksum lookup ran before the critical section."""

    def test_the_vault_service_stores_identical_bytes_once(self) -> None:
        from urbanlens.dashboard.services.photos.photo_upload import upload_photo

        results, errors = self.race(
            lambda: upload_photo(self.profile, SimpleUploadedFile("a.png", _RED, content_type="image/png")),
            lambda: upload_photo(self.profile, SimpleUploadedFile("again.png", _RED, content_type="image/png")),
        )

        self.assertEqual(Image.objects.filter(profile=self.profile).count(), 1, f"{results} / {errors}")
        self.assertEqual([getattr(e, "status", e) for e in errors], [409])

    def test_the_owner_service_stores_identical_bytes_on_a_pin_once(self) -> None:
        from urbanlens.dashboard.services.photos.uploads import UploadRejection, upload_photo_for_owner

        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

        results, errors = self.race(
            lambda: upload_photo_for_owner(
                pin, self.profile, SimpleUploadedFile("a.png", _RED, content_type="image/png")
            ),
            lambda: upload_photo_for_owner(
                pin, self.profile, SimpleUploadedFile("b.png", _RED, content_type="image/png")
            ),
        )

        self.assertEqual(errors, [])
        self.assertEqual(Image.objects.filter(pin=pin).count(), 1, f"two rows for one file on one pin: {results}")
        self.assertIn(409, [r.status for r in results if isinstance(r, UploadRejection)])

    def test_the_pin_upload_view_stores_identical_bytes_once(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
        url = reverse("pin.upload_image", args=[pin.slug])

        def post(name: str):
            def call() -> int:
                client = Client()
                client.force_login(self.user)
                return client.post(url, {"image": SimpleUploadedFile(name, _RED, content_type="image/png")}).status_code

            return call

        results, errors = self.race(post("a.png"), post("b.png"))

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(Image.objects.filter(pin=pin).count(), 1)


EXTERNAL_CEILING = 1000


@override_settings(EXTERNAL_MEDIA_DAILY_BYTES=EXTERNAL_CEILING)
class ParallelExternalMediaCeilingTests(_RaceCase):
    """G2-22: the daily external-media ceiling was read, then written."""

    def setUp(self) -> None:
        super().setUp()
        dns = mock.patch("socket.getaddrinfo", return_value=_FAKE_DNS_RESULT)
        dns.start()
        self.addCleanup(dns.stop)
        self.location = baker.make(Location)

    def test_two_parallel_caches_cannot_both_pass_a_spent_allowance(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item

        baker.make(
            Image,
            profile=self.profile,
            location=self.location,
            file_size=EXTERNAL_CEILING - 50,
            quota_exempt_reason=QuotaExemption.EXTERNAL_MEDIA,
        )

        def response():
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.raw.read.return_value = b"x" * 100
            resp.is_redirect = False
            return resp

        def cache_one(url: str):
            return lambda: materialize_media_item(
                location=self.location, profile=self.profile, source="wikimedia", url=url
            )

        with mock.patch(
            "urbanlens.dashboard.services.media.media_materialize.requests.get", side_effect=lambda *a, **k: response()
        ):
            results, errors = self.race(
                cache_one("https://example.test/a.jpg"), cache_one("https://example.test/b.jpg")
            )

        self.assertEqual(len(results), 1, f"both caches passed a spent allowance: {results} / {errors}")
        self.assertEqual([type(e) for e in errors], [MaterializeError])


class UploadReservationLockTests(_RaceCase):
    """The lock under ``reserve_upload``: bounded, per profile, released with the transaction."""

    def hold(self, profile) -> tuple[threading.Thread, threading.Event]:
        """Hold *profile*'s reservation in another connection until the returned event is set."""
        from urbanlens.dashboard.services.media.storage import reserve_upload

        held, release = threading.Event(), threading.Event()

        def run() -> None:
            try:
                with reserve_upload(profile, None):
                    held.set()
                    release.wait(10)
            finally:
                connections.close_all()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(held.wait(10))
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        return thread, release

    def test_a_second_reservation_gives_up_after_its_wait(self) -> None:
        from urbanlens.dashboard.services.media.storage import UploadReservationBusyError, reserve_upload

        thread, release = self.hold(self.profile)
        with (
            self.assertRaises(UploadReservationBusyError) as caught,
            reserve_upload(self.profile, None, wait_seconds=0.2),
        ):
            pass
        self.assertEqual(caught.exception.status, 429)

        release.set()
        thread.join(10)
        with reserve_upload(self.profile, None, wait_seconds=0.2):
            pass

    def test_another_profiles_reservation_is_not_held(self) -> None:
        from urbanlens.dashboard.services.media.storage import reserve_upload

        self.hold(self.profile)
        other = baker.make(User).profile
        with reserve_upload(other, None, wait_seconds=0.2):
            pass

    def test_a_refused_upload_releases_the_reservation(self) -> None:
        from urbanlens.dashboard.services.media.storage import StorageQuotaExceededError, reserve_upload

        self.fill_to(10)
        with self.assertRaises(StorageQuotaExceededError), reserve_upload(self.profile, 11):
            pass
        with reserve_upload(self.profile, None, wait_seconds=0.2):
            pass
        thread, release = self.hold(self.profile)
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "the refused reservation's lock outlived its transaction")

    def test_a_nested_reservation_for_the_same_profile_does_not_wait_on_itself(self) -> None:
        from urbanlens.dashboard.services.media.storage import reserve_upload

        with reserve_upload(self.profile, None), reserve_upload(self.profile, None, wait_seconds=0.2):
            pass

    def test_the_callers_lock_timeout_survives_the_wait(self) -> None:
        from django.db import connection, transaction

        from urbanlens.dashboard.services.media.storage import reserve_upload

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '7s'")
            with reserve_upload(self.profile, None, wait_seconds=0.2):
                pass
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], "7s")

    def test_a_busy_refusal_leaves_the_callers_transaction_usable(self) -> None:
        from django.db import connection, transaction

        from urbanlens.dashboard.services.media.storage import UploadReservationBusyError, reserve_upload

        self.hold(self.profile)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '7s'")
            with self.assertRaises(UploadReservationBusyError), reserve_upload(self.profile, None, wait_seconds=0.2):
                pass
            cursor.execute("SHOW lock_timeout")
            self.assertEqual(cursor.fetchone()[0], "7s")

    def test_the_lock_refuses_to_run_outside_a_transaction(self) -> None:
        from django.db.transaction import TransactionManagementError

        from urbanlens.dashboard.services.media.storage import lock_profile_uploads

        with self.assertRaises(TransactionManagementError):
            lock_profile_uploads(self.profile)


class VisitBatchEnqueueTests(_RaceCase):
    """Rows stored under a reservation are only handed to the worker once they are committed."""

    def test_visit_photos_are_queued_after_the_commit(self) -> None:
        from django.db import connection

        from urbanlens.dashboard.controllers import visits
        from urbanlens.dashboard.models.visits.model import PinVisit

        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))
        visit = baker.make(PinVisit, pin=pin)
        in_transaction: list[bool] = []

        def record(*_args: object, **_kwargs: object) -> None:
            in_transaction.append(connection.in_atomic_block)

        request = mock.Mock()
        request.FILES.getlist.return_value = [
            SimpleUploadedFile("a.png", _RED, content_type="image/png"),
            SimpleUploadedFile("b.png", _BLUE, content_type="image/png"),
        ]
        request.POST.getlist.return_value = []
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task", side_effect=record):
            self.assertTrue(visits._sync_visit_photos(request, pin, visit))

        self.assertEqual(in_transaction, [False, False])
