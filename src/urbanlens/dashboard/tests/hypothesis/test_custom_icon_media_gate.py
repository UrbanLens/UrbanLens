"""Custom pin and label icons are served only to viewers who can see the pin or label (P14)."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

_BYTES = b"fake-icon-bytes"
PIN_ICON = "pin_custom_icons/zz/token/owner-pin.png"
LABEL_ICON = "label_icons/zz/token/owner-label.png"


class _IconGateCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_icon_gate_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        for rel_path in (PIN_ICON, LABEL_ICON):
            target = Path(media_root) / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_BYTES)

        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.owner_user = baker.make("auth.User")
        self.owner = self.owner_user.profile
        self.pin = baker.make(Pin, profile=self.owner, location=baker.make(Location), custom_icon=PIN_ICON)
        self.label = Label.objects.create(profile=self.owner, kind=KIND_TAG, name="ZzIcon", custom_icon=LABEL_ICON)

    def _fetch(self, rel_path: str, user) -> int:
        self.client.force_login(user)
        response = self.client.get(f"/media/{rel_path}")
        if getattr(response, "streaming", False):
            b"".join(response.streaming_content)
            if getattr(response, "file_to_stream", None) is not None:
                response.file_to_stream.close()
        return response.status_code


class OwnerTests(_IconGateCase):
    def test_the_owner_fetches_their_own_icons(self) -> None:
        self.assertEqual(self._fetch(PIN_ICON, self.owner_user), 200)
        self.assertEqual(self._fetch(LABEL_ICON, self.owner_user), 200)


class StrangerTests(_IconGateCase):
    def test_a_stranger_cannot_fetch_another_accounts_pin_icon(self) -> None:
        self.assertEqual(self._fetch(PIN_ICON, baker.make("auth.User")), 404)

    def test_a_stranger_cannot_fetch_another_accounts_personal_label_icon(self) -> None:
        self.assertEqual(self._fetch(LABEL_ICON, baker.make("auth.User")), 404)

    def test_a_global_labels_icon_is_served_to_everyone(self) -> None:
        Label.objects.filter(pk=self.label.pk).update(profile=None)

        self.assertEqual(self._fetch(LABEL_ICON, baker.make("auth.User")), 200)
