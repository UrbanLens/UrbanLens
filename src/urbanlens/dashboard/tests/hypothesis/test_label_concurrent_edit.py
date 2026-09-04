"""LabelEditView and the external API's LabelDetailView.patch must not clobber
concurrent edits to fields they don't themselves touch.

Both ended with a bare label.save(), writing every column from that request's
in-memory snapshot - reverting any field a concurrent request (another tab, or
the other of these two independent implementations of "edit this label")
changed in the window between this request's load and its own save. Same bug
class fixed for PinList's equivalent pair of views; see PROBLEMS.md.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import labels
from urbanlens.dashboard.external_api import views as external_api_views
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.labels.model import KIND_TAG, Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


class LabelEditViewConcurrentWriteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_concurrent_edit_to_keywords_survives_a_rename(self) -> None:
        """keywords has no control on this form at all - a bare save() would
        always revert it to whatever was loaded at the top of the request."""
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Old", keywords="original")
        real_get = labels._owned_label

        def load_then_inject_concurrent_write(*args, **kwargs):
            loaded = real_get(*args, **kwargs)
            Label.objects.filter(pk=loaded.pk).update(keywords="Changed elsewhere")
            return loaded

        with mock.patch.object(labels, "_owned_label", side_effect=load_then_inject_concurrent_write):
            response = self.client.post(
                reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.id}),
                data={"name": "New"},
            )

        self.assertEqual(response.status_code, 200)
        label.refresh_from_db()
        self.assertEqual(label.name, "New")
        self.assertEqual(
            label.keywords,
            "Changed elsewhere",
            "a concurrent edit to another field was reverted by this request's save",
        )


class LabelDetailViewConcurrentWriteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Labels client")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[ApiKeyScope.LABELS_READ.value, ApiKeyScope.LABELS_WRITE.value]
        )

    def _bearer(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def test_concurrent_edit_to_another_field_survives_a_rename(self) -> None:
        label = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Old", keywords="original")
        real_get = external_api_views._get_label

        def load_then_inject_concurrent_write(*args, **kwargs):
            loaded = real_get(*args, **kwargs)
            Label.objects.filter(pk=loaded.pk).update(keywords="Changed elsewhere")
            return loaded

        with mock.patch.object(external_api_views, "_get_label", side_effect=load_then_inject_concurrent_write):
            response = self.client.patch(
                f"/dashboard/api/external/v1/labels/{label.uuid}/",
                {"name": "New"},
                content_type="application/json",
                **self._bearer(),
            )

        self.assertEqual(response.status_code, 200)
        label.refresh_from_db()
        self.assertEqual(label.name, "New")
        self.assertEqual(
            label.keywords,
            "Changed elsewhere",
            "a concurrent edit to another field was reverted by this request's save",
        )
