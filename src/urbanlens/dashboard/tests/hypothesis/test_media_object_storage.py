"""Media can move to an object store without moving out from behind the gate."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import TYPE_CHECKING
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, default_storage
from django.http import Http404
from django.test import override_settings

from hypothesis import given, settings as hypothesis_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase

if TYPE_CHECKING:
    from django.core.files.base import File

_GATED_BACKEND = "urbanlens.dashboard.services.media.object_storage.GatedS3Storage"

_S3_OPTIONS = {
    "bucket_name": "ul-media",
    "endpoint_url": "http://garage-s3.example.invalid:3900",
    "access_key": "AKIAEXAMPLEEXAMPLE00",
    "secret_key": "sekritsekritsekritsekritsekritsekritsekr",
    "region_name": "garage",
    "addressing_style": "path",
    "default_acl": None,
    "querystring_auth": True,
    "file_overwrite": False,
    "signature_version": "s3v4",
}

#: A key shaped like the ones `upload_to` actually produces.
_REAL_KEY = "pin_images/a7/Kd3xq8Lm2Zpq/2026-4f1c8ab29e1d4f0.jpg"


def _object_storage_settings(**overrides: object) -> dict[str, object]:
    """Settings that put the app on the object-store backend.

    Args:
        **overrides: Extra settings to apply alongside.

    Returns:
        Keyword arguments for ``override_settings``."""
    return {
        "UL_MEDIA_STORAGE_BACKEND": "s3",
        "STORAGES": {
            "default": {"BACKEND": _GATED_BACKEND, "OPTIONS": dict(_S3_OPTIONS)},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        },
        **overrides,
    }


class FakeObjectStorage(Storage):
    """An in-memory stand-in for a remote store, so no test opens a socket.

    Deliberately *not* a ``FileSystemStorage`` subclass: the gate picks its byte source by asking whether the
    default storage is one, so a fake that inherits from it would take the filesystem branch and test nothing."""

    def __init__(self, contents: dict[str, bytes] | None = None, *, signer: bool = True) -> None:
        self.contents = dict(contents or {})
        self.opened: list[str] = []
        if not signer:
            # Deleting the attribute models a backend with no presigning at all,
            # which must fall back to streaming rather than emitting a broken
            # X-Accel-Redirect.
            self.signed_object_url = None  # type: ignore[assignment]

    def exists(self, name: str) -> bool:
        return name in self.contents

    def _open(self, name: str, mode: str = "rb") -> File:
        self.opened.append(name)
        if name not in self.contents:
            raise FileNotFoundError(name)
        return ContentFile(self.contents[name], name=name)

    def url(self, name: str) -> str:
        return f"/media/{name}"

    def signed_object_url(self, name: str, expire: int) -> str:
        return f"http://garage-s3.example.invalid:3900/ul-media/{name}?X-Amz-Expires={expire}&X-Amz-Signature=deadbeef"


class GatedUrlTests(SimpleTestCase):
    """`url()` returns the gate's path, never the bucket's."""

    def _storage(self):
        from urbanlens.dashboard.services.media.object_storage import GatedS3Storage

        return GatedS3Storage(**_S3_OPTIONS)

    @given(
        st.lists(
            st.text(alphabet=st.characters(min_codepoint=32, blacklist_characters="/\\"), min_size=1, max_size=12),
            min_size=1,
            max_size=4,
        ),
    )
    @hypothesis_settings(max_examples=50, deadline=None)
    def test_url_matches_the_filesystem_backend_exactly(self, segments: list[str]) -> None:
        """Whatever the name, both backends render the same URL.

        This is the property that makes the switch invisible to every existing
        call site - and the one that stops a presigned URL reaching a template.
        """
        name = "/".join(segments)
        self.assertEqual(self._storage().url(name), FileSystemStorage(base_url="/media/").url(name))

    def test_url_is_a_media_path_not_a_bucket_url(self) -> None:
        url = self._storage().url(_REAL_KEY)
        self.assertEqual(url, f"/media/{_REAL_KEY}")
        self.assertNotIn("garage-s3", url)
        self.assertNotIn("X-Amz-Signature", url)

    @override_settings(UL_MEDIA_BASE_URL="https://media.example.org", MEDIA_URL="https://media.example.org/media/")
    def test_url_follows_a_configured_media_origin(self) -> None:
        """The media-origin split has to keep working, since MEDIA_URL is how it works."""
        self.assertEqual(self._storage().url(_REAL_KEY), f"https://media.example.org/media/{_REAL_KEY}")

    def test_signed_url_is_available_but_is_not_what_url_returns(self) -> None:
        """The signing machinery exists for the nginx hand-off, and only for it."""
        storage = self._storage()
        signed = storage.signed_object_url(_REAL_KEY, 60)
        self.assertIn("X-Amz-Signature", signed)
        self.assertIn("garage-s3.example.invalid", signed)
        self.assertNotEqual(signed, storage.url(_REAL_KEY))


class ObjectKeyNormalizationTests(SimpleTestCase):
    """Traversal is refused structurally, since there is no root to contain a key."""

    def _normalize(self, path: str) -> str:
        from urbanlens.dashboard.controllers.media import _normalize_object_key

        return _normalize_object_key(path)

    def test_real_key_is_accepted_unchanged(self) -> None:
        self.assertEqual(self._normalize(_REAL_KEY), _REAL_KEY)

    def test_key_with_unicode_and_spaces_is_accepted(self) -> None:
        """Only separators are refused; a filename is free to be a filename."""
        name = "vault_documents/b3/xY9/Straße report (final).pdf"
        self.assertEqual(self._normalize(name), name)

    def test_traversal_and_separator_payloads_are_refused(self) -> None:
        payloads = [
            "",
            "..",
            "../etc/passwd",
            "pin_images/../../etc/passwd",
            "pin_images/a7/../../../etc/passwd",
            "/etc/passwd",
            "pin_images//a7/x.jpg",
            "pin_images/./a7/x.jpg",
            "pin_images/a7/",
            "pin_images\\a7\\x.jpg",
            "pin_images/a7/x.jpg\x00.png",
            "\x00",
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(Http404):
                self._normalize(payload)


class ByteSourceSelectionTests(SimpleTestCase):
    """The resolver picks its byte source from the storage that is actually configured."""

    def test_filesystem_storage_still_takes_the_local_branch(self) -> None:
        from urbanlens.dashboard.controllers.media import LocalMediaSource, resolve_media_path

        self.assertIsInstance(default_storage, FileSystemStorage)
        with self.assertRaises(Http404):
            # Nothing is on disk under this name, and the local branch is the
            # only one that checks - which is what proves the branch was taken.
            resolve_media_path(_REAL_KEY)
        self.assertTrue(issubclass(LocalMediaSource, object))

    def test_object_storage_takes_the_remote_branch_without_touching_the_disk(self) -> None:
        from urbanlens.dashboard.controllers.media import ObjectMediaSource, resolve_media_path

        with mock.patch("urbanlens.dashboard.controllers.media.default_storage", FakeObjectStorage()):
            source = resolve_media_path(_REAL_KEY)
        self.assertIsInstance(source, ObjectMediaSource)
        self.assertEqual(source.rel_path, _REAL_KEY)

    def test_object_storage_still_refuses_traversal(self) -> None:
        from urbanlens.dashboard.controllers.media import resolve_media_path

        with (
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", FakeObjectStorage()),
            self.assertRaises(Http404),
        ):
            resolve_media_path("pin_images/../../etc/passwd")


class LocalByteDeliveryTests(SimpleTestCase):
    """The filesystem branch's half of the same contract."""

    def test_a_file_that_vanished_after_resolution_is_a_404_not_a_500(self) -> None:
        """The resolver checks existence; delivery opens the file. Async processing replaces a just-uploaded photo's file between the two, and the row - which is what authorizes the request - still names the old one until the task ends."""
        from urbanlens.dashboard.controllers.media import LocalMediaSource

        with tempfile.TemporaryDirectory() as media_root:
            source = LocalMediaSource(_REAL_KEY, Path(media_root) / "vanished.jpg")
            with override_settings(MEDIA_X_ACCEL=False), self.assertRaises(Http404):
                source.response()

    def test_the_accel_hand_off_does_not_re_open_the_file(self) -> None:
        """nginx serves the bytes, so a vanished file is nginx's 404 to answer, not this
        process's - and re-checking here would cost a stat on every media request."""
        from urbanlens.dashboard.controllers.media import LocalMediaSource

        with tempfile.TemporaryDirectory() as media_root:
            source = LocalMediaSource(_REAL_KEY, Path(media_root) / "vanished.jpg")
            with override_settings(MEDIA_X_ACCEL=True, MEDIA_X_ACCEL_PREFIX="/_protected_media/"):
                response = source.response()
        self.assertTrue(response["X-Accel-Redirect"].startswith("/_protected_media/"))


class ObjectByteDeliveryTests(SimpleTestCase):
    """How the bytes get out, and what the client is never given."""

    def _source(self, storage: FakeObjectStorage):
        from urbanlens.dashboard.controllers.media import ObjectMediaSource

        return storage, ObjectMediaSource(_REAL_KEY)

    def test_streams_the_object_when_no_internal_proxy_is_configured(self) -> None:
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}))
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX=""),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
        ):
            response = source.response()
        self.assertEqual(b"".join(response.streaming_content), b"jpeg-bytes")
        self.assertEqual(storage.opened, [_REAL_KEY])
        self.assertNotIn("X-Accel-Redirect", response)

    def test_a_streamed_response_leaks_no_signed_url(self) -> None:
        """The negative half: nothing signed may appear in a client-visible header."""
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}))
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX=""),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
        ):
            response = source.response()
        rendered = " ".join(f"{key}: {value}" for key, value in response.items())
        self.assertNotIn("X-Amz-Signature", rendered)
        self.assertNotIn("garage-s3", rendered)

    def test_missing_object_is_a_404_not_a_500(self) -> None:
        storage, source = self._source(FakeObjectStorage())
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX=""),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
            self.assertRaises(Http404),
        ):
            source.response()

    def test_hands_nginx_a_signed_url_when_an_internal_proxy_is_configured(self) -> None:
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}))
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX="/_object_media/"),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
        ):
            response = source.response()
        target = response["X-Accel-Redirect"]
        self.assertTrue(target.startswith("/_object_media/"))
        self.assertIn("X-Amz-Signature", target)
        # The body is empty: nginx supplies the bytes, so no gevent worker moves
        # them and nothing was opened.
        self.assertEqual(response.content, b"")
        self.assertEqual(storage.opened, [])

    def test_the_accel_target_names_no_host_of_its_own(self) -> None:
        """nginx pins its upstream; this header must not be able to move it.

        An absolute URL here would turn any bug that can influence a stored path into server-side request
        forgery performed by the one process that sits inside the cluster."""
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}))
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX="/_object_media/"),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
        ):
            target = source.response()["X-Accel-Redirect"]
        self.assertNotIn("://", target)
        self.assertNotIn("garage-s3.example.invalid", target)
        self.assertEqual(target.split("?")[0], "/_object_media/ul-media/" + _REAL_KEY)

    def test_falls_back_to_streaming_when_the_backend_cannot_sign(self) -> None:
        """A prefix plus a backend with no presigning must not emit a broken redirect."""
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}, signer=False))
        with (
            override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX="/_object_media/"),
            mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage),
        ):
            response = source.response()
        self.assertNotIn("X-Accel-Redirect", response)
        self.assertEqual(b"".join(response.streaming_content), b"jpeg-bytes")

    def test_both_delivery_paths_mark_the_response_private(self) -> None:
        """A shared cache must never keep one viewer's photo for the next one."""
        storage, source = self._source(FakeObjectStorage({_REAL_KEY: b"jpeg-bytes"}))
        with mock.patch("urbanlens.dashboard.controllers.media.default_storage", storage):
            with override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX="/_object_media/"):
                accel = source.response()
            with override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX=""):
                streamed = source.response()
        for response in (accel, streamed):
            with self.subTest(response=response):
                self.assertIn("private", response["Cache-Control"])


class ObjectStorageCheckTests(SimpleTestCase):
    """`manage.py check` refuses a configuration that would leak or fail."""

    def _run(self) -> list:
        from urbanlens.dashboard.checks import check_object_storage_is_configured

        return check_object_storage_is_configured()

    def test_filesystem_default_is_silent(self) -> None:
        self.assertEqual(self._run(), [])

    def test_orphaned_object_prefix_is_an_error(self) -> None:
        with override_settings(MEDIA_X_ACCEL_OBJECT_PREFIX="/_object_media/"):
            self.assertEqual([message.id for message in self._run()], ["dashboard.E009"])

    def test_a_complete_object_configuration_only_warns_about_local_subtrees(self) -> None:
        with override_settings(**_object_storage_settings()):
            messages = self._run()
        self.assertEqual([message.id for message in messages], ["dashboard.W002"])
        self.assertIn("exports/", messages[0].msg)

    def test_upstream_backend_without_the_url_override_is_an_error(self) -> None:
        """The whole point of the subclass, asserted as a refusal to start."""
        overrides = _object_settings_with(backend="storages.backends.s3.S3Storage")
        with override_settings(**overrides):
            self.assertIn("dashboard.E010", [message.id for message in self._run()])

    def test_missing_credentials_are_an_error(self) -> None:
        overrides = _object_settings_with(options={**_S3_OPTIONS, "access_key": None, "bucket_name": ""})
        with override_settings(**overrides):
            messages = [message for message in self._run() if message.id == "dashboard.E011"]
        self.assertEqual(len(messages), 1)
        self.assertIn("UL_S3_BUCKET_NAME", messages[0].msg)
        self.assertIn("UL_S3_ACCESS_KEY_ID", messages[0].msg)

    def test_a_public_acl_is_an_error(self) -> None:
        overrides = _object_settings_with(options={**_S3_OPTIONS, "default_acl": "public-read"})
        with override_settings(**overrides):
            self.assertIn("dashboard.E012", [message.id for message in self._run()])

    def test_the_settings_it_reads_actually_exist(self) -> None:
        """An override that invents a setting proves nothing about production.

        Every name the check consults is read off the real settings module here, so a rename cannot leave the
        check passing against a setting no deployment has."""
        from django.conf import settings as django_settings

        for name in (
            "UL_MEDIA_STORAGE_BACKEND",
            "MEDIA_X_ACCEL_OBJECT_PREFIX",
            "MEDIA_X_ACCEL_OBJECT_URL_TTL_SECONDS",
            "STORAGES",
        ):
            with self.subTest(setting=name):
                self.assertTrue(hasattr(django_settings, name), f"{name} is missing from settings/base.py")


def _object_settings_with(
    *, backend: str = _GATED_BACKEND, options: dict[str, object] | None = None
) -> dict[str, object]:
    """Object-store settings with one field swapped, for the negative cases.

    Args:
        backend: Dotted path for ``STORAGES["default"]["BACKEND"]``.
        options: Replacement storage options.

    Returns:
        Keyword arguments for ``override_settings``."""
    return {
        "UL_MEDIA_STORAGE_BACKEND": "s3",
        "STORAGES": {
            "default": {"BACKEND": backend, "OPTIONS": dict(options or _S3_OPTIONS)},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        },
    }
