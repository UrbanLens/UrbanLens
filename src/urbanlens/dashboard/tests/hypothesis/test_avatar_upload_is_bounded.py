"""An avatar is scanned synchronously, and bounded by the site's photo ceiling.

N21 H45. `set_profile_avatar` runs the shared `image_upload_error` gauntlet
without `skip_malware_scan`, so the antivirus scan happens inside the request:
the file is copied into a BytesIO in the gunicorn worker and streamed to the
shared clamd daemon. The only size bound on it is `file_size_error_for_upload`,
which is the *site-wide photo/video* cap - 250MB by default, and an admin may
raise it to 900MB. So one person changing their profile picture can occupy a
worker, and the clamd daemon every other upload shares, for as long as a
quarter-gigabyte scan takes.

The asymmetry is the argument. `_download_avatar_from_url` - the OAuth path,
fetching from Discord or Google - already refuses anything over 512KB. The same
product concept is bounded two orders of magnitude apart depending on which
door it arrives through, and the tighter door is the one where the bytes are not
even the user's choice.

Bounded rather than deferred, deliberately. Moving the scan off the request the
way comment images do would mean storing and potentially serving an avatar that
has not been scanned yet, and comment images only get away with that because
`pending_scan` hides them from everyone but their author until it clears. There
is no equivalent gate on a profile picture, so the safe fix is to make the scan
cheap rather than to make it late - the cost is proportional to the bytes, and
an avatar has no business being 250MB.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.profile.avatar import AvatarTooLargeError, set_profile_avatar

CEILING_SETTING = "AVATAR_MAX_UPLOAD_BYTES"
_SCAN = "urbanlens.dashboard.services.security.malware_scan.malware_error_for_upload"


def _upload(size: int) -> SimpleUploadedFile:
    # A real PNG header so the content sniffer is not what refuses it.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * max(0, size - 8)
    return SimpleUploadedFile("avatar.png", png, content_type="image/png")


class _AvatarCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile


class TheSettingExistsTests(_AvatarCase):
    def test_the_avatar_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, CEILING_SETTING), f"nothing reads {CEILING_SETTING}")
        self.assertGreater(getattr(settings, CEILING_SETTING), 0)


@override_settings(**{CEILING_SETTING: 4096})
class TheUploadIsBoundedTests(_AvatarCase):
    def test_an_avatar_over_the_ceiling_is_refused(self) -> None:
        with self.assertRaises(AvatarTooLargeError):
            set_profile_avatar(self.profile, _upload(8192))

    def test_the_scan_never_runs_for_an_oversized_avatar(self) -> None:
        """The whole point: the scan is the expensive part, so refusing after it saves nothing."""
        with mock.patch(_SCAN) as scan, self.assertRaises(AvatarTooLargeError):
            set_profile_avatar(self.profile, _upload(8192))

        scan.assert_not_called()

    def test_nothing_is_stored_for_an_oversized_avatar(self) -> None:
        with self.assertRaises(AvatarTooLargeError):
            set_profile_avatar(self.profile, _upload(8192))

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.avatar)


@override_settings(**{CEILING_SETTING: 4096})
class OrdinaryAvatarsStillWorkTests(_AvatarCase):
    """The half that stops the ceiling passing against a path that refuses everything."""

    def test_an_avatar_under_the_ceiling_is_stored(self) -> None:
        with mock.patch(_SCAN, return_value=None):
            set_profile_avatar(self.profile, _upload(1024))

        self.profile.refresh_from_db()
        self.assertTrue(self.profile.avatar)

    def test_an_ordinary_avatar_is_still_scanned(self) -> None:
        """Bounded, not skipped - the scan must still happen for what is accepted."""
        with mock.patch(_SCAN, return_value=None) as scan:
            set_profile_avatar(self.profile, _upload(1024))

        scan.assert_called_once()
