"""Tests for permanently deleting a user's own WikiEdit from the history.

Covers LocationWikiEditDeleteView: restoring the pre-edit value (when the
edit hasn't already been reverted), erasing the WikiEdit row - and its
paired revert record, if any - and rejecting attempts to delete someone
else's edit.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit


def _location_with_wiki(name: str = "Old Mill"):
    location = baker.make("dashboard.Location")
    wiki = baker.make("dashboard.Wiki", location=location, name=name)
    return location, wiki


class WikiEditDeleteViewTests(TestCase):
    """POST /location/<slug>/wiki/history/<edit_id>/delete/"""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location, self.wiki = _location_with_wiki()
        baker.make(Pin, profile=self.profile, location=self.location)

    def _delete(self, edit_id: int):
        return self.client.post(reverse("location.wiki.history.delete", args=[self.location.slug, edit_id]))

    def test_restores_value_and_removes_the_edit(self):
        edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.profile,
            changes={"name": {"from": "Old Mill", "to": "My home address, 123 Main St"}},
            reverted=False,
        )

        response = self._delete(edit.pk)

        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")
        self.assertFalse(WikiEdit.objects.filter(pk=edit.pk).exists())

    def test_forbidden_for_someone_elses_edit(self):
        self.wiki.name = "Renamed"
        self.wiki.save(update_fields=["name"])
        other_profile = baker.make("dashboard.Profile")
        edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=other_profile,
            changes={"name": {"from": "Old Mill", "to": "Renamed"}},
            reverted=False,
        )

        response = self._delete(edit.pk)

        self.assertEqual(response.status_code, 403)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Renamed")
        self.assertTrue(WikiEdit.objects.filter(pk=edit.pk).exists())

    def test_already_reverted_edit_also_deletes_its_revert_record(self):
        self.wiki.name = "Old Mill"
        self.wiki.save(update_fields=["name"])
        target_edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.profile,
            changes={"name": {"from": "Old Mill", "to": "My home address, 123 Main St"}},
            reverted=True,
        )
        revert_edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.profile,
            changes={"name": {"from": "My home address, 123 Main St", "to": "Old Mill"}},
        )
        target_edit.reverted_by = revert_edit
        target_edit.save(update_fields=["reverted_by"])

        response = self._delete(target_edit.pk)

        self.assertEqual(response.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Old Mill")
        self.assertFalse(WikiEdit.objects.filter(pk=target_edit.pk).exists())
        self.assertFalse(WikiEdit.objects.filter(pk=revert_edit.pk).exists())

    def test_delete_button_only_shown_for_own_edits(self):
        own_edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=self.profile,
            changes={"name": {"from": "Old Mill", "to": "Renamed"}},
            reverted=False,
        )
        other_profile = baker.make("dashboard.Profile")
        others_edit = baker.make(
            "dashboard.WikiEdit",
            wiki=self.wiki,
            editor=other_profile,
            changes={"name": {"from": "Renamed", "to": "Renamed Again"}},
            reverted=False,
        )

        response = self.client.get(reverse("location.wiki.history", args=[self.location.slug]))

        delete_url = reverse("location.wiki.history.delete", args=[self.location.slug, own_edit.pk])
        others_delete_url = reverse("location.wiki.history.delete", args=[self.location.slug, others_edit.pk])
        self.assertContains(response, delete_url)
        self.assertNotContains(response, others_delete_url)
