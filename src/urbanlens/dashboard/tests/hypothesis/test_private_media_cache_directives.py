"""Per-viewer media responses must forbid shared caches from storing them.

Every one of these endpoints authorizes its bytes *per viewer*: the same URL
legitimately returns an image for one profile and a 404 for another. Django
emits ``Vary: Cookie`` on them, which is right but not sufficient - shared
caches commonly honour only ``Vary: Accept-Encoding`` and otherwise key on the
URL alone, and these URLs end in real image extensions, which is exactly what
extension-based CDN cache rules match. With no ``Cache-Control`` at all (the
state before ``mark_private_media`` existed) such a cache applies its own
default TTL to one user's private photo.

The interesting test here is the last one: it asserts that every view in the
package that serves raw bytes actually routes through the helper. A per-view
test can only cover the views someone remembered to write a test for, and the
failure mode is silent - the response is correct in every visible way except
the missing header.
"""

from __future__ import annotations

import ast
from pathlib import Path
import shutil
import tempfile

from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers import media_auth
from urbanlens.dashboard.controllers.media_auth import PRIVATE_MEDIA_MAX_AGE_SECONDS, mark_private_media
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.tests.hypothesis.test_media_gate import _IMAGE_BYTES, _new_user

_CONTROLLERS = Path(media_auth.__file__).parent

#: Byte-serving views live in these modules; each returns an image body from a
#: path the requester had to be authorized for.
_BYTE_SERVING_MODULES = (
    "media.py",
    "media_proxy.py",
    "media_preview.py",
    "immich.py",
    "pin_suggestions.py",
    "google_photos.py",
)


class MarkPrivateMediaTests(SimpleTestCase):
    def test_sets_private_and_a_max_age(self) -> None:
        from django.http import HttpResponse

        response = mark_private_media(HttpResponse(b"bytes"))

        self.assertEqual(response["Cache-Control"], f"private, max-age={PRIVATE_MEDIA_MAX_AGE_SECONDS}")

    def test_private_is_what_forbids_shared_storage(self) -> None:
        """``max-age`` alone would *invite* a shared cache to store it."""
        from django.http import HttpResponse

        directives = {part.strip() for part in mark_private_media(HttpResponse(b"")).get("Cache-Control", "").split(",")}

        self.assertIn("private", directives)


class MediaGateCacheDirectiveTests(TestCase):
    """The media gate itself, through the URLconf, in both serving modes."""

    def setUp(self) -> None:
        self._media_root = tempfile.mkdtemp(prefix="ul_media_cache_")
        self.addCleanup(shutil.rmtree, self._media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self._media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)

        target = Path(self._media_root) / "pin_images" / "owned.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_IMAGE_BYTES)

        self.owner_user = _new_user()
        self.owner: Profile = self.owner_user.profile
        baker.make(Image, image="pin_images/owned.png", profile=self.owner)
        self.client.force_login(self.owner_user)

    def _drain(self, response) -> None:
        """Consume a streaming body so the temp MEDIA_ROOT can be removed."""
        if getattr(response, "streaming", False):
            b"".join(response.streaming_content)
            handle = getattr(response, "file_to_stream", None)
            if handle is not None:
                handle.close()

    def test_dev_mode_response_is_private(self) -> None:
        response = self.client.get("/media/pin_images/owned.png")
        self._drain(response)

        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])

    def test_x_accel_response_is_private(self) -> None:
        """nginx copies upstream headers onto the file it streams, so this is the live one."""
        with override_settings(MEDIA_X_ACCEL=True):
            response = self.client.get("/media/pin_images/owned.png")
        self._drain(response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("X-Accel-Redirect"), "/_protected_media/pin_images/owned.png")
        self.assertIn("private", response["Cache-Control"])


class EveryByteServingViewIsMarkedTests(SimpleTestCase):
    """Static check: no byte-serving return escapes ``mark_private_media``.

    Matches on the response *construction* rather than the returned name, so a
    view that builds its response and returns it a few lines later is still
    covered.
    """

    def _image_body_returns(self, module: str) -> list[int]:
        """Line numbers of ``return HttpResponse(<bytes>, content_type=...)`` not wrapped."""
        tree = ast.parse((_CONTROLLERS / module).read_text(encoding="utf-8"))
        unwrapped = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            if name == "mark_private_media":
                continue
            if name not in {"HttpResponse", "FileResponse"} or not call.args:
                continue  # empty responses: redirects, X-Accel handoffs built later, errors
            if name == "HttpResponse" and not any(kw.arg == "content_type" for kw in call.keywords):
                continue  # a plain text/status body (e.g. the 429), not media bytes
            unwrapped.append(node.lineno)
        return unwrapped

    def test_no_unmarked_byte_serving_return(self) -> None:
        offenders = {module: lines for module in _BYTE_SERVING_MODULES if (lines := self._image_body_returns(module))}

        self.assertEqual(offenders, {}, f"byte-serving returns missing mark_private_media(): {offenders}")

    def test_the_check_would_notice_a_regression(self) -> None:
        """Guard against the scan silently matching nothing at all."""
        marked = sum((_CONTROLLERS / module).read_text(encoding="utf-8").count("mark_private_media(") for module in _BYTE_SERVING_MODULES)

        self.assertGreaterEqual(marked, len(_BYTE_SERVING_MODULES), "scan found almost no marked responses - it has stopped measuring anything")
