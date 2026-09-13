"""A label's custom icon must not be decoded by the request that uploads it.

``_resize_custom_icon`` opened every uploaded icon with Pillow inside the label create and edit
requests, with no ``untrusted_parse`` guard - so ``warn`` could not even log it, and the web process
ran a decoder over bytes the uploader chose (P103). The upload is now held until the sandbox worker
has re-encoded it (P119).
"""

from __future__ import annotations

import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
import PIL.Image

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.media.held_upload import ICON_MAX_PX, hold_upload
from urbanlens.dashboard.services.sandbox.guard import UnsandboxedParseError
from urbanlens.dashboard.tasks import publish_held_upload

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
KEY = "dashboard.Label.custom_icon"


def _png(width: int, height: int, name: str = "icon.png") -> SimpleUploadedFile:
    buffer = io.BytesIO()
    PIL.Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class LabelIconIsNotDecodedInTheRequestTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_creating_a_label_with_an_oversized_icon_decodes_nothing(self) -> None:
        icon = _png(600, 400)

        with (
            mock.patch("PIL.Image.open", wraps=PIL.Image.open) as opened,
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            response = self.client.post(
                reverse("label.create", kwargs={"label_kind": "tag"}), data={"name": "Urbex", "custom_icon": icon}
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Label.objects.get(profile=self.profile, name="Urbex").custom_icon_upload)
        opened.assert_not_called()

    def test_editing_a_label_icon_decodes_nothing(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        icon = _png(600, 400)

        with (
            mock.patch("PIL.Image.open", wraps=PIL.Image.open) as opened,
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            response = self.client.post(
                reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id}),
                data={"name": "Urbex", f"custom_icon-{label.id}": icon},
            )

        self.assertEqual(response.status_code, 200, response.content)
        label.refresh_from_db()
        self.assertTrue(label.custom_icon_upload)
        opened.assert_not_called()


class LabelIconIsShrunkInTheSandboxTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _label_holding(self, width: int, height: int, name: str = "icon.png") -> Label:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        label.save(update_fields=[hold_upload(label, "custom_icon", _png(width, height, name))])
        return label

    def _in_sandbox(self) -> override_settings:
        return override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny")

    def _publishes(self, enqueue: mock.MagicMock) -> list[tuple]:
        return [call.args for call in enqueue.call_args_list if call.args and call.args[0] is publish_held_upload]

    def test_uploading_an_icon_queues_its_publish_once_the_label_is_saved(self) -> None:
        with self.captureOnCommitCallbacks(execute=True), mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            self.client.post(
                reverse("label.create", kwargs={"label_kind": "tag"}),
                data={"name": "Urbex", "custom_icon": _png(600, 400)},
            )

        label = Label.objects.get(profile=self.profile, name="Urbex")
        self.assertEqual(self._publishes(enqueue), [(publish_held_upload, KEY, label.pk, label.custom_icon_upload)])

    def test_replacing_an_icon_on_edit_queues_its_publish(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")

        with self.captureOnCommitCallbacks(execute=True), mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            self.client.post(
                reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id}),
                data={"name": "Urbex", f"custom_icon-{label.id}": _png(600, 400)},
            )

        label.refresh_from_db()
        self.assertEqual(self._publishes(enqueue), [(publish_held_upload, KEY, label.pk, label.custom_icon_upload)])

    def test_a_published_icon_does_not_leave_the_upload_on_disk(self) -> None:
        label = self._label_holding(600, 400)
        held = label.custom_icon_upload
        storage = label.custom_icon.storage

        with self._in_sandbox():
            self.assertTrue(publish_held_upload(KEY, label.pk, held))

        label.refresh_from_db()
        self.assertFalse(storage.exists(held))
        self.assertTrue(label.custom_icon.name and storage.exists(label.custom_icon.name))

    def test_a_label_deleted_while_its_icon_waited_keeps_the_upload_for_undo(self) -> None:
        """Deleting a label stashes the held upload's name for undo, so the file has to still be there to restore."""
        label = self._label_holding(600, 400)
        held, storage, label_id = label.custom_icon_upload, label.custom_icon.storage, label.pk
        label.delete()

        with self._in_sandbox():
            self.assertFalse(publish_held_upload(KEY, label_id, held))

        self.assertTrue(storage.exists(held))

    def test_the_sandbox_worker_shrinks_an_oversized_icon(self) -> None:
        label = self._label_holding(600, 400)

        with self._in_sandbox():
            self.assertTrue(publish_held_upload(KEY, label.pk, label.custom_icon_upload))

        label.refresh_from_db()
        with label.custom_icon.open("rb") as stored:
            self.assertLessEqual(max(PIL.Image.open(stored).size), ICON_MAX_PX)

    def test_the_web_process_may_not_do_the_decode(self) -> None:
        """The guard the re-encode carries is real: outside the sandbox, under deny, it refuses."""
        label = self._label_holding(600, 400)

        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            self.assertRaises(UnsandboxedParseError),
        ):
            publish_held_upload(KEY, label.pk, label.custom_icon_upload)

    def test_a_small_icon_is_still_reencoded(self) -> None:
        """Its size needs nothing, but whatever metadata the upload carried would be served with it (P119)."""
        label = self._label_holding(64, 64)

        with self._in_sandbox():
            self.assertTrue(publish_held_upload(KEY, label.pk, label.custom_icon_upload))

        label.refresh_from_db()
        with label.custom_icon.open("rb") as stored:
            self.assertEqual(PIL.Image.open(stored).size, (64, 64))
