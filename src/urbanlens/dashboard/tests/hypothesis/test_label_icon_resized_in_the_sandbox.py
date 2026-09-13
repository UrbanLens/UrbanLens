"""A label's custom icon must not be decoded by the request that uploads it.

``_resize_custom_icon`` opened every uploaded icon with Pillow inside the label create and edit
requests, with no ``untrusted_parse`` guard - so ``warn`` could not even log it, and the web process
ran a decoder over bytes the uploader chose (P103).
"""

from __future__ import annotations

import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.fields.files import FieldFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
import PIL.Image

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.labels.icons import ICON_MAX_PX, resize_stored_icon
from urbanlens.dashboard.services.sandbox.guard import UnsandboxedParseError
from urbanlens.dashboard.tasks import resize_label_icon

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


def _png(width: int, height: int, name: str = "icon.png") -> SimpleUploadedFile:
    buffer = io.BytesIO()
    PIL.Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _stored_name(stored: FieldFile) -> str:
    assert stored.name, "the label has no icon"
    return stored.name


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
        self.assertTrue(Label.objects.get(profile=self.profile, name="Urbex").custom_icon)
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
        self.assertTrue(label.custom_icon)
        opened.assert_not_called()


class LabelIconIsShrunkInTheSandboxTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _label_with_icon(self, width: int, height: int, name: str = "icon.png") -> Label:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")
        label.custom_icon = _png(width, height, name)
        label.save()
        return label

    def _in_sandbox(self) -> override_settings:
        return override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny")

    def test_uploading_an_icon_queues_its_resize_once_the_label_is_saved(self) -> None:
        with self.captureOnCommitCallbacks(execute=True), mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            self.client.post(
                reverse("label.create", kwargs={"label_kind": "tag"}),
                data={"name": "Urbex", "custom_icon": _png(600, 400)},
            )

        label = Label.objects.get(profile=self.profile, name="Urbex")
        resizes = [call.args for call in enqueue.call_args_list if call.args and call.args[0] is resize_label_icon]
        self.assertEqual(resizes, [(resize_label_icon, label.pk, _stored_name(label.custom_icon))])

    def test_replacing_an_icon_on_edit_queues_its_resize(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Urbex")

        with self.captureOnCommitCallbacks(execute=True), mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            self.client.post(
                reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id}),
                data={"name": "Urbex", f"custom_icon-{label.id}": _png(600, 400)},
            )

        label.refresh_from_db()
        resizes = [call.args for call in enqueue.call_args_list if call.args and call.args[0] is resize_label_icon]
        self.assertEqual(resizes, [(resize_label_icon, label.pk, _stored_name(label.custom_icon))])

    def test_a_shrunk_icon_does_not_leave_the_original_on_disk(self) -> None:
        """Label files are not managed by the cleanup receivers, so the swap is the only thing that removes it."""
        label = self._label_with_icon(600, 400)
        original = _stored_name(label.custom_icon)
        storage = label.custom_icon.storage

        with self._in_sandbox():
            resize_stored_icon(label.pk, original)

        label.refresh_from_db()
        self.assertNotEqual(_stored_name(label.custom_icon), original)
        self.assertFalse(storage.exists(original))
        self.assertTrue(storage.exists(_stored_name(label.custom_icon)))

    def test_a_label_deleted_before_its_resize_keeps_the_original_for_undo(self) -> None:
        """Deleting a label stashes its icon's name for undo, so the file has to still be there to restore."""
        label = self._label_with_icon(600, 400)
        original, storage, label_id = _stored_name(label.custom_icon), label.custom_icon.storage, label.pk
        label.delete()

        with self._in_sandbox():
            self.assertFalse(resize_stored_icon(label_id, original))

        self.assertTrue(storage.exists(original))

    def test_the_sandbox_worker_shrinks_an_oversized_icon(self) -> None:
        label = self._label_with_icon(600, 400)

        with self._in_sandbox():
            self.assertTrue(resize_stored_icon(label.pk, _stored_name(label.custom_icon)))

        label.refresh_from_db()
        with label.custom_icon.open("rb") as stored:
            self.assertLessEqual(max(PIL.Image.open(stored).size), ICON_MAX_PX)

    def test_the_web_process_may_not_do_the_decode(self) -> None:
        """The guard the resize now carries is real: outside the sandbox, under deny, it refuses."""
        label = self._label_with_icon(600, 400)

        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            self.assertRaises(UnsandboxedParseError),
        ):
            resize_stored_icon(label.pk, _stored_name(label.custom_icon))

    def test_an_icon_replaced_since_the_resize_was_queued_is_left_alone(self) -> None:
        label = self._label_with_icon(600, 400)
        stale = _stored_name(label.custom_icon)
        label.custom_icon = _png(64, 64, "newer.png")
        label.save()
        current = _stored_name(label.custom_icon)

        with self._in_sandbox():
            self.assertFalse(resize_stored_icon(label.pk, stale))

        label.refresh_from_db()
        self.assertEqual(_stored_name(label.custom_icon), current)

    def test_a_small_icon_is_still_reencoded(self) -> None:
        """Its size needs nothing, but whatever metadata the upload carried would be served with it (P119)."""
        label = self._label_with_icon(64, 64)
        name = _stored_name(label.custom_icon)

        with self._in_sandbox():
            self.assertTrue(resize_stored_icon(label.pk, name))

        label.refresh_from_db()
        self.assertNotEqual(_stored_name(label.custom_icon), name)
        with label.custom_icon.open("rb") as stored:
            self.assertEqual(PIL.Image.open(stored).size, (64, 64))
