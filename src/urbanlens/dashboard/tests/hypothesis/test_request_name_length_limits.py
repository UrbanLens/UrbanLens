"""A name taken from a request must not reach a bounded column unchecked."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.saved_filter.model import SavedFilter


def _too_long(model, field: str) -> str:
    return "n" * (model._meta.get_field(field).max_length + 1)


class RequestNameLengthTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_label_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tags"}), {"name": _too_long(Label, "name")}
        )

        self.assertEqual(response.status_code, 400)
        # Not `Label.objects.exists()` - the app seeds global labels, so only
        # the name under test says anything about this request.
        self.assertFalse(Label.objects.filter(name__startswith="nnn").exists())

    def test_list_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(reverse("lists.create"), {"name": _too_long(PinList, "name")})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.exists())

    def test_saved_filter_create_rejects_an_overlong_name(self) -> None:
        response = self.client.post(
            reverse("saved_filters.create"), {"filter_name": _too_long(SavedFilter, "name"), "search": "x"}
        )

        self.assertNotEqual(response.status_code, 500)
        self.assertFalse(SavedFilter.objects.filter(name__startswith="nnn").exists())

    def test_pin_alias_create_rejects_an_overlong_name(self) -> None:
        """`sanitize_name` drops characters but does not bound length."""
        pin = baker.make("dashboard.Pin", profile=self.profile, location=baker.make("dashboard.Location"))

        response = self.client.post(
            reverse("pin.aliases", kwargs={"pin_slug": pin.slug}), {"name": _too_long(PinAlias, "name")}
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinAlias.objects.filter(name__startswith="nnn").exists())

    def test_label_edit_rejects_an_overlong_name(self) -> None:
        """The edit path, which chunk 559's fix to the create path did not reach.

        `controllers/labels.py` has the highest fix-density in the repository - 8 of 18 changes in three months
        were bug fixes, six of them about validating user-supplied label attributes on the way in."""
        label = baker.make(Label, profile=self.profile, kind="tag", name="original")

        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tags", "label_id": label.pk}),
            {"name": _too_long(Label, "name")},
        )

        self.assertEqual(response.status_code, 400)
        label.refresh_from_db()
        self.assertEqual(label.name, "original", "an over-long name was written to a 255-wide column")

    def test_label_edit_refuses_an_icon_that_create_would_refuse(self) -> None:
        """Edit truncated arbitrary text; create runs it through clean_icon."""
        label = baker.make(Label, profile=self.profile, kind="tag", name="tagname", icon="star")

        self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tags", "label_id": label.pk}),
            {"name": "tagname", "icon": "not an icon at all"},
        )

        label.refresh_from_db()
        self.assertNotEqual(label.icon, "not an icon at all", "edit stored free text as an icon")

    def test_label_customization_rejects_an_overlong_icon(self) -> None:
        """The third of five Label icon write paths, and the only one that 500s.

        `upsert_label_customization` normalises the icon by stripping it and nothing else - no length bound and
        no `clean_icon` - so a value longer than `LabelCustomization.icon`'s 50 characters reaches the column."""
        # profile=None: customization applies to *global* labels only. A
        # profile-owned label is delegated to LabelEditView instead, so this
        # test silently exercised the wrong path until the fixture was fixed.
        label = baker.make(Label, profile=None, kind="tag", name="globaltag")

        response = self.client.post(
            reverse("label.customize", kwargs={"label_kind": "tags", "label_id": label.pk}),
            {"icon": "x" * 80, "name": "", "color": ""},
        )

        self.assertNotEqual(response.status_code, 500, "an over-long customization icon reached the column")

    def test_a_name_at_exactly_the_limit_is_still_accepted(self) -> None:
        """The boundary belongs to the user, not to the error path."""
        limit = PinList._meta.get_field("name").max_length

        response = self.client.post(reverse("lists.create"), {"name": "n" * limit})

        self.assertNotEqual(response.status_code, 400)
        self.assertTrue(PinList.objects.filter(name="n" * limit).exists())
