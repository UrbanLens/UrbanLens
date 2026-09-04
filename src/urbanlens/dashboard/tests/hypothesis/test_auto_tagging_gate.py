"""Auto-tagging is granted, then opted *out* of - not opted into.

Reported: the Organize page asked every label two questions (an "auto-tagging"
checkbox and a comma-separated keyword list) that together made automatic
tagging something a user had to assemble by hand, per label. It is now a
capability: a user who has the subscription feature and has not switched it
off gets it for every tag and category label, minus whichever ones they
excluded individually.

Statuses, people and media labels are deliberately outside it - REData's
suggestion service models "which of my labels describes this place", which
"Visited" and a person's name are not.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.labels import _auto_tag_available
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
from urbanlens.dashboard.models.subscriptions.model import SiteFeature


class AutoTagAvailabilityTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _available(self, kind: str, *, has_feature: bool = True, disabled: bool = False) -> bool:
        from unittest import mock

        self.profile.disable_auto_tagging = disabled
        with mock.patch("urbanlens.dashboard.controllers.labels.user_has_feature", return_value=has_feature):
            return _auto_tag_available(self.user, self.profile, kind)

    def test_a_subscriber_gets_it_for_tags_and_categories_by_default(self) -> None:
        self.assertTrue(self._available(KIND_TAG))
        self.assertTrue(self._available(KIND_CATEGORY))

    def test_without_the_feature_there_is_nothing_to_offer(self) -> None:
        self.assertFalse(self._available(KIND_TAG, has_feature=False))
        # Same user/kind, capability granted - an "always gated off" implementation
        # would still pass the assertion above.
        self.assertTrue(self._available(KIND_TAG, has_feature=True))

    def test_the_user_can_switch_it_off_wholesale(self) -> None:
        self.assertFalse(self._available(KIND_TAG, disabled=True))
        self.assertFalse(self._available(KIND_CATEGORY, disabled=True))
        # Same user/kinds, switched back on - an "always disabled" implementation
        # would still pass the assertions above.
        self.assertTrue(self._available(KIND_TAG, disabled=False))
        self.assertTrue(self._available(KIND_CATEGORY, disabled=False))

    def test_statuses_people_and_media_are_out_of_scope(self) -> None:
        """ "Visited" and a person's name are not "what is this place"."""
        for kind in (KIND_STATUS, KIND_USER, KIND_MEDIA):
            self.assertFalse(self._available(kind), f"{kind} labels should never auto-tag")

    def test_the_capability_is_its_own_feature(self) -> None:
        """Not folded into AI: suggestions come from REData, not an LLM call."""
        self.assertIn("auto_tagging", SiteFeature.values)


class OrganizeFormTests(TestCase):
    """The label edit form asks one question, phrased as the exception."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _render(self, label, *, show_toggle: bool) -> str:
        from django.template.loader import render_to_string

        return render_to_string(
            "dashboard/partials/labels/organize_label_edit_form.html",
            {
                "label": label,
                "show_auto_tag_toggle": show_toggle,
                # The form reverses its own action URL from this.
                "label_url_kind": "tags",
                "available_parents": [],
                "selected_ids": [],
                "selected_parents": [],
                "selected_children": [],
            },
        )

    def test_the_keyword_field_is_gone(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant")

        markup = self._render(label, show_toggle=True)

        self.assertNotIn('name="keywords"', markup, "keyword-based tagging was replaced by REData suggestions")

    def test_the_control_is_the_opt_out(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant", allow_auto_tag=True)

        markup = self._render(label, show_toggle=True)

        self.assertIn('name="disable_auto_tag"', markup)
        self.assertNotIn('name="allow_auto_tag"', markup)

    def test_an_excluded_label_shows_as_checked(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Private", allow_auto_tag=False)

        markup = self._render(label, show_toggle=True)

        self.assertIn("checked", markup.split('name="disable_auto_tag"')[1][:80])

    def test_nothing_is_offered_without_the_capability(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant")

        markup = self._render(label, show_toggle=False)

        self.assertNotIn('name="disable_auto_tag"', markup)


class ControlMatchesServerTests(TestCase):
    """The dialog must not offer a control the server ignores, or hide one it honours.

    This is the invariant the previous gating test existed for (a checkbox was
    once shown on AI grounds alone, while the server decided on the user's own
    settings). The gate changed; the invariant did not, so it is asserted here
    against the one helper both halves now consult.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _post_edit(self, label, disable: bool):
        from django.urls import reverse

        data = {"name": label.name, "color": label.color or "#2196F3", "icon": label.icon or ""}
        if disable:
            data["disable_auto_tag"] = "on"
        return self.client.post(reverse("label.edit", kwargs={"label_kind": "tags", "label_id": label.pk}), data)

    def test_the_server_ignores_the_field_without_the_capability(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant", allow_auto_tag=True)

        with mock.patch("urbanlens.dashboard.controllers.labels.user_has_feature", return_value=False):
            self._post_edit(label, disable=True)

        label.refresh_from_db()
        self.assertTrue(
            label.allow_auto_tag, "a user without the capability must not be able to set a flag they cannot see"
        )

    def test_the_server_honours_the_field_with_the_capability(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant", allow_auto_tag=True)

        with mock.patch("urbanlens.dashboard.controllers.labels.user_has_feature", return_value=True):
            self._post_edit(label, disable=True)

        label.refresh_from_db()
        self.assertFalse(label.allow_auto_tag)

    def test_unchecking_it_puts_the_label_back_in(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.models.labels.model import Label

        label = baker.make(Label, profile=self.user.profile, kind=KIND_TAG, name="Power plant", allow_auto_tag=False)

        with mock.patch("urbanlens.dashboard.controllers.labels.user_has_feature", return_value=True):
            self._post_edit(label, disable=False)

        label.refresh_from_db()
        self.assertTrue(label.allow_auto_tag)
