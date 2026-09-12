"""The Immich proxy reads a user-supplied server's whole response into memory.

N21 H24/H38. The *cache* half is already fixed - `bounded_cache.set_if_small`
refuses to store an oversized body, with a comment noting the server is the
user's own and `size=thumbnail` is a request rather than a guarantee. What that
does not touch is the read: `_get_binary` calls `response.content`, which
buffers the entire body in the gunicorn worker before any size is known.

So the cache is safe and the worker is not. The server on the other end is
configured by the account holder, which makes the response size something one
user chooses and every user on that worker pays for.

Streamed with a ceiling, refused as soon as it is passed, so a hostile or
broken server costs a few chunks rather than its whole body. Two ceilings,
because the same helper serves both doors: a thumbnail is small, and an
original is a photo the site is about to store - so it is bounded by the same
limit the site applies to a direct upload. A file it would refuse from a
browser is not one it should accept from Immich.
"""

from __future__ import annotations

from typing import Self
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.services.apis.immich.gateway import GatewayRequestError, ImmichGateway

THUMBNAIL_SETTING = "IMMICH_MAX_THUMBNAIL_BYTES"


def _account() -> ImmichAccount:
    user = baker.make(User)
    return ImmichAccount(profile=user.profile, server_url="https://photos.example.com", api_key="test-key")


class _StreamingResponse:
    """A response whose body arrives in chunks, counting how many were pulled."""

    def __init__(self, chunk: bytes, chunks: int, content_type: str = "image/jpeg") -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.ok = True
        self.status_code = 200
        self.headers = {"Content-Type": content_type}
        self.text = ""
        self.chunks_read = 0

    def iter_content(self, chunk_size: int = 8192):
        for _ in range(self._chunks):
            self.chunks_read += 1
            yield self._chunk

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _gateway_returning(response) -> ImmichGateway:
    gateway = ImmichGateway(account=_account(), session=mock.MagicMock())
    gateway.session.get.return_value = response
    return gateway


class TheSettingExistsTests(TestCase):
    def test_the_thumbnail_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, THUMBNAIL_SETTING), f"nothing reads {THUMBNAIL_SETTING}")
        self.assertGreater(getattr(settings, THUMBNAIL_SETTING), 0)


@override_settings(**{THUMBNAIL_SETTING: 4096})
class TheThumbnailDoorIsBoundedTests(TestCase):
    def test_an_oversized_thumbnail_is_refused(self) -> None:
        gateway = _gateway_returning(_StreamingResponse(b"x" * 1024, chunks=20))

        with self.assertRaises(GatewayRequestError):
            gateway.get_asset_thumbnail("asset-1")

    def test_the_body_is_not_read_to_the_end(self) -> None:
        """The point is not to buffer it: a refusal after the fact saves nothing."""
        response = _StreamingResponse(b"x" * 1024, chunks=2000)
        gateway = _gateway_returning(response)

        with self.assertRaises(GatewayRequestError):
            gateway.get_asset_thumbnail("asset-1")

        self.assertLess(response.chunks_read, 20, f"read {response.chunks_read} chunks before refusing")

    def test_an_ordinary_thumbnail_still_arrives_whole(self) -> None:
        """The half that stops the ceiling passing against a gateway that refuses everything."""
        gateway = _gateway_returning(_StreamingResponse(b"x" * 512, chunks=2))

        content, content_type = gateway.get_asset_thumbnail("asset-1")

        self.assertEqual(content, b"x" * 1024)
        self.assertEqual(content_type, "image/jpeg")


class TheOriginalDoorIsBoundedTests(TestCase):
    """The original is a photo the site is about to store, so the upload limit is its ceiling."""

    def test_an_original_over_the_site_upload_limit_is_refused(self) -> None:
        from urbanlens.dashboard.services.media.storage import max_upload_file_size_bytes

        over = max_upload_file_size_bytes() + 1
        gateway = _gateway_returning(_StreamingResponse(b"x" * 65536, chunks=over // 65536 + 2))

        with self.assertRaises(GatewayRequestError):
            gateway.get_asset_original("asset-1")

    def test_an_ordinary_original_still_arrives_whole(self) -> None:
        gateway = _gateway_returning(_StreamingResponse(b"y" * 1024, chunks=3))

        content, _content_type, _filename = gateway.get_asset_original("asset-1")

        self.assertEqual(content, b"y" * 3072)
