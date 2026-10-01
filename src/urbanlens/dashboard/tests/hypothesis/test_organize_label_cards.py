"""The Organize label cards: the data attributes the organize scripts read, and the per-label action rules (P66)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
import lxml.html
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

_READ_BY_EVERY_MANAGER = ("id", "name", "color", "icon", "pin-count", "parents")


class _CardFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _cards(self, url_kind: str) -> dict[str, lxml.html.HtmlElement]:
        response = self.client.get(reverse("label.rows", kwargs={"label_kind": url_kind}))
        self.assertEqual(response.status_code, 200)
        root = lxml.html.fragment_fromstring(response.content.decode(), create_parent="div")
        return {card.get("data-id"): card for card in root.find_class("tag-card")}


class DataAttributeTests(_CardFixture):
    def test_every_kind_renders_what_its_manager_reads(self) -> None:
        for kind, url_kind, prefix in (
            (KIND_TAG, "tags", "tag"),
            (KIND_CATEGORY, "categories", "category"),
            (KIND_STATUS, "statuses", "status"),
            (KIND_USER, "people", "people"),
            (KIND_MEDIA, "media", "media"),
        ):
            with self.subTest(kind=kind):
                parent = ensure_label(
                    profile=self.profile, name=f"Parent {kind}", kind=kind, color="#123456", icon="star"
                )
                child = ensure_label(profile=self.profile, name=f'Child "{kind}" <b>', kind=kind)
                child.parents.add(parent)

                card = self._cards(url_kind)[str(child.pk)]

                for attr in _READ_BY_EVERY_MANAGER:
                    self.assertIsNotNone(card.get(f"data-{prefix}-{attr}"), attr)
                self.assertEqual(card.get(f"data-{prefix}-id"), str(child.pk))
                self.assertEqual(card.get(f"data-{prefix}-name"), f'Child "{kind}" <b>')
                self.assertEqual(card.get(f"data-{prefix}-parents"), str(parent.pk))
                self.assertEqual(self._cards(url_kind)[str(parent.pk)].get(f"data-{prefix}-color"), "#123456")

    def test_a_tag_reports_its_pins(self) -> None:
        tag = ensure_label(profile=self.profile, name="Rooftop", kind=KIND_TAG)
        baker.make(Pin, profile=self.profile, _quantity=3, labels=[tag])

        card = self._cards("tags")[str(tag.pk)]

        self.assertEqual(card.get("data-tag-pin-count"), "3")
        self.assertEqual(card.find_class("tag-card-stat-value")[0].text_content(), "3")

    def test_a_status_says_whether_it_is_protected(self) -> None:
        mine = ensure_label(profile=self.profile, name="Scouted", kind=KIND_STATUS)

        self.assertEqual(self._cards("statuses")[str(mine.pk)].get("data-status-protected"), "false")


class ActionRuleTests(_CardFixture):
    @staticmethod
    def _titles(card: lxml.html.HtmlElement) -> set[str]:
        return {el.get("title") for el in card.iter() if el.get("title")}

    def test_a_protected_label_can_be_edited_but_not_deleted_or_merged(self) -> None:
        status = ensure_label(profile=self.profile, name="Locked", kind=KIND_STATUS, is_protected=True)

        titles = self._titles(self._cards("statuses")[str(status.pk)])

        self.assertIn("Edit", titles)
        self.assertNotIn("Delete", titles)
        self.assertNotIn("Merge", titles)

    def test_an_own_tag_can_be_edited_merged_and_deleted(self) -> None:
        tag = ensure_label(profile=self.profile, name="Mine", kind=KIND_TAG)

        titles = self._titles(self._cards("tags")[str(tag.pk)])

        self.assertTrue({"Edit", "Merge", "Delete"} <= titles)

    def test_people_and_media_cannot_be_merged_and_show_no_counts(self) -> None:
        for kind, url_kind in ((KIND_USER, "people"), (KIND_MEDIA, "media")):
            with self.subTest(kind=kind):
                label = ensure_label(profile=self.profile, name=f"Plain {kind}", kind=kind)

                card = self._cards(url_kind)[str(label.pk)]

                self.assertNotIn("Merge", self._titles(card))
                self.assertNotIn("View on map", self._titles(card))
                self.assertEqual({el.text_content() for el in card.find_class("tag-card-stat-value")}, {"—"})

    def test_a_global_tag_offers_customizing_and_only_an_admin_edits_it(self) -> None:
        tag = ensure_label(profile=None, name="Everyone's", kind=KIND_TAG)

        titles = self._titles(self._cards("tags")[str(tag.pk)])

        self.assertIn("Customize your display of this global label", titles)
        self.assertNotIn("Edit global label", titles)
        # The delete view refuses a non-owner, so a button here could only fail.
        self.assertNotIn("Delete", titles)

    def test_an_admin_gets_one_delete_for_a_global_tag(self) -> None:
        tag = ensure_label(profile=None, name="Everyone's", kind=KIND_TAG)
        self.client.force_login(User.objects.order_by("pk").first())

        card = self._cards("tags")[str(tag.pk)]

        self.assertIn("Edit global label", self._titles(card))
        self.assertEqual(len([el for el in card.iter() if el.get("title") == "Delete"]), 1)

    def test_the_map_link_needs_a_pin_in_the_subtree(self) -> None:
        parent = ensure_label(profile=self.profile, name="Area", kind=KIND_TAG)
        child = ensure_label(profile=self.profile, name="Spot", kind=KIND_TAG)
        empty = ensure_label(profile=self.profile, name="Empty", kind=KIND_TAG)
        child.parents.add(parent)
        baker.make(Pin, profile=self.profile, labels=[child])

        cards = self._cards("tags")

        self.assertIn("View on map", self._titles(cards[str(parent.pk)]))
        self.assertIn("No pins in this label or its sub-labels", self._titles(cards[str(empty.pk)]))
