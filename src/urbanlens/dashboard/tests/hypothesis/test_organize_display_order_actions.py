"""Display Order offers the same delete/merge actions as the other label tabs.

The tab listed tags, categories and statuses together for reordering, but was the
only one with no way to remove or merge anything from it - a user reordering their
labels had to switch tabs to delete one and come back.

Per-item delete is rendered here; bulk delete and merge come from the shared
``#org-bulk-bar`` toolbar, which the tab already drove for bulk *edit*. Merge is
deliberately not offered per item: every other tab passes ``merge_url=''`` to the
label card, so merge has always been a 2-or-more selection action, and adding a
single-item variant here would be a new interaction rather than parity.

Global labels get neither control, matching the other tabs - a user does not own
them, so they can only stop using them.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class DisplayOrderActionsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Rooftop")

    def _priority_html(self) -> str:
        response = self.client.get(reverse("organize.priority.list"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_an_owned_label_offers_delete(self) -> None:
        html = self._priority_html()

        self.assertIn(reverse("label.delete", kwargs={"label_kind": KIND_TAG, "label_id": self.tag.pk}), html)
        self.assertIn("priority-delete-btn", html)

    def test_deleting_asks_for_confirmation_first(self) -> None:
        """Matching the other tabs: the confirm names the label and reassures about pins."""
        html = self._priority_html()

        self.assertIn("Pins will NOT be deleted", html)

    def test_a_global_label_offers_no_delete(self) -> None:
        """A user does not own it, so the other tabs do not offer this either."""
        Label.objects.filter(pk=self.tag.pk).update(profile=None)

        html = self._priority_html()

        self.assertNotIn(reverse("label.delete", kwargs={"label_kind": KIND_TAG, "label_id": self.tag.pk}), html)

    def test_the_delete_refreshes_the_reordered_list(self) -> None:
        """Otherwise the row lingers until the next full page load."""
        html = self._priority_html()

        self.assertIn("refreshPriority", html)

    def test_each_kind_targets_its_own_rows_container(self) -> None:
        """The delete re-renders the owning tab's rows, so its ids must line up."""
        ensure_label( profile=self.profile, kind=KIND_CATEGORY, name="Hospital")
        baker.make(Label, profile=self.profile, kind=KIND_STATUS, name="Sealed")

        html = self._priority_html()

        for kind in (KIND_TAG, KIND_CATEGORY, KIND_STATUS):
            with self.subTest(kind=kind):
                self.assertIn(f'hx-target="#{kind}-rows"', html)

    def test_the_delete_endpoint_still_removes_the_label(self) -> None:
        """The button is only useful if the URL it posts to works from here."""
        response = self.client.post(reverse("label.delete", kwargs={"label_kind": KIND_TAG, "label_id": self.tag.pk}))

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Label.objects.filter(pk=self.tag.pk).exists())
