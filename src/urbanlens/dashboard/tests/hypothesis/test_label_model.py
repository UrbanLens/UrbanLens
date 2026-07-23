"""Tests for Label model properties and LabelQuerySet filter methods.

get_label_and_descendants is already thoroughly covered in test_label.py.
This file covers the customization-aware display properties and queryset filters.

Property tests use unsaved Label instances with _user_customizations injected
directly - no DB access required.  Queryset tests use baker.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth.models import User
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.labels.model import (
    KIND_CATEGORY,
    KIND_STATUS,
    KIND_TAG,
    Label,
)

_hyp = settings(max_examples=50, deadline=None)
_text = st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N")))


def _label(name: str = "test-label", icon: str | None = None, color: str | None = None) -> Label:
    """Create an unsaved Label with no customization prefetched."""
    b = Label()
    b.name = name
    b.icon = icon
    b.color = color
    b.__dict__["_user_customizations"] = []
    return b


def _custom(name: str | None = None, icon: str | None = None, color: str | None = None):
    """Simulate a prefetched LabelCustomization row."""
    return SimpleNamespace(name=name, icon=icon, color=color)


# -- _get_customization --------------------------------------------------------


class LabelGetCustomizationTests(SimpleTestCase):
    """_get_customization() returns the first prefetched item or None."""

    def test_returns_none_with_empty_list(self) -> None:
        b = _label()
        self.assertIsNone(b._get_customization())

    def test_returns_first_prefetched_item(self) -> None:
        b = _label()
        c = _custom(name="Override")
        b.__dict__["_user_customizations"] = [c]
        self.assertIs(b._get_customization(), c)


# -- effective_name ------------------------------------------------------------


class LabelEffectiveNameTests(SimpleTestCase):
    """effective_name returns the user's override or falls back to the label name."""

    def test_no_customization_returns_label_name(self) -> None:
        self.assertEqual(_label(name="Global Tag").effective_name, "Global Tag")

    def test_customization_with_name_overrides(self) -> None:
        b = _label(name="Global Tag")
        b.__dict__["_user_customizations"] = [_custom(name="My Name")]
        self.assertEqual(b.effective_name, "My Name")

    def test_customization_with_none_name_falls_back(self) -> None:
        b = _label(name="Global Tag")
        b.__dict__["_user_customizations"] = [_custom(name=None)]
        self.assertEqual(b.effective_name, "Global Tag")

    def test_customization_with_empty_string_falls_back(self) -> None:
        b = _label(name="Global Tag")
        b.__dict__["_user_customizations"] = [_custom(name="")]
        self.assertEqual(b.effective_name, "Global Tag")

    @given(_text)
    @_hyp
    def test_override_name_is_always_returned_when_truthy(self, name: str) -> None:
        b = _label(name="Fallback")
        b.__dict__["_user_customizations"] = [_custom(name=name)]
        self.assertEqual(b.effective_name, name)


# -- effective_icon ------------------------------------------------------------


class LabelEffectiveIconTests(SimpleTestCase):
    """effective_icon returns the user's override or falls back to the label icon."""

    def test_no_customization_returns_label_icon(self) -> None:
        self.assertEqual(_label(icon="star").effective_icon, "star")

    def test_customization_with_icon_overrides(self) -> None:
        b = _label(icon="star")
        b.__dict__["_user_customizations"] = [_custom(icon="heart")]
        self.assertEqual(b.effective_icon, "heart")

    def test_customization_with_none_icon_falls_back(self) -> None:
        b = _label(icon="star")
        b.__dict__["_user_customizations"] = [_custom(icon=None)]
        self.assertEqual(b.effective_icon, "star")

    def test_no_icon_anywhere_returns_none(self) -> None:
        b = _label(icon=None)
        self.assertIsNone(b.effective_icon)


# -- effective_color -----------------------------------------------------------


class LabelEffectiveColorTests(SimpleTestCase):
    """effective_color returns the user's override or falls back to the label color."""

    def test_no_customization_returns_label_color(self) -> None:
        self.assertEqual(_label(color="#ff0000").effective_color, "#ff0000")

    def test_customization_with_color_overrides(self) -> None:
        b = _label(color="#ff0000")
        b.__dict__["_user_customizations"] = [_custom(color="#00ff00")]
        self.assertEqual(b.effective_color, "#00ff00")

    def test_customization_with_none_color_falls_back(self) -> None:
        b = _label(color="#ff0000")
        b.__dict__["_user_customizations"] = [_custom(color=None)]
        self.assertEqual(b.effective_color, "#ff0000")

    def test_no_color_anywhere_returns_none(self) -> None:
        b = _label(color=None)
        self.assertIsNone(b.effective_color)


# -- is_customized -------------------------------------------------------------


class LabelIsCustomizedTests(SimpleTestCase):
    """is_customized is True when the prefetched customization has any non-None field."""

    def test_no_customization_is_false(self) -> None:
        self.assertFalse(_label().is_customized)

    def test_customization_with_name_is_true(self) -> None:
        b = _label()
        b.__dict__["_user_customizations"] = [_custom(name="X")]
        self.assertTrue(b.is_customized)

    def test_customization_with_icon_is_true(self) -> None:
        b = _label()
        b.__dict__["_user_customizations"] = [_custom(icon="🏭")]
        self.assertTrue(b.is_customized)

    def test_customization_with_color_is_true(self) -> None:
        b = _label()
        b.__dict__["_user_customizations"] = [_custom(color="#abc")]
        self.assertTrue(b.is_customized)

    def test_all_none_customization_is_false(self) -> None:
        b = _label()
        b.__dict__["_user_customizations"] = [_custom(name=None, icon=None, color=None)]
        self.assertFalse(b.is_customized)


# -- icon_is_overridden --------------------------------------------------------


class LabelIconIsOverriddenTests(SimpleTestCase):
    """icon_is_overridden is True only when customization.icon is not None."""

    def test_no_customization_is_false(self) -> None:
        self.assertFalse(_label(icon="star").icon_is_overridden)

    def test_customization_with_icon_is_true(self) -> None:
        b = _label(icon="star")
        b.__dict__["_user_customizations"] = [_custom(icon="heart")]
        self.assertTrue(b.icon_is_overridden)

    def test_customization_with_none_icon_is_false(self) -> None:
        b = _label(icon="star")
        b.__dict__["_user_customizations"] = [_custom(icon=None, name="Override")]
        self.assertFalse(b.icon_is_overridden)


# -- Label.__str__ -------------------------------------------------------------


class LabelStrTests(TestCase):
    """__str__ includes [global] for shared labels and (profile) for user-owned ones."""

    def test_global_label_has_global_label(self) -> None:
        b: Label = baker.make(Label, name="Urban Ruin", profile=None, kind=KIND_TAG)
        self.assertIn("[global]", str(b))
        self.assertIn("Urban Ruin", str(b))

    def test_user_label_excludes_global_label(self) -> None:
        user: User = baker.make(User)
        b: Label = baker.make(Label, name="Mine", profile=user.profile, kind=KIND_TAG)
        self.assertIn("Mine", str(b))
        self.assertNotIn("[global]", str(b))


# -- LabelQuerySet.tags / categories / statuses --------------------------------


class LabelQuerySetKindTests(TestCase):
    """tags(), categories(), and statuses() filter by the kind field."""

    def setUp(self):
        self.tag = baker.make("dashboard.Label", name="t", kind=KIND_TAG, profile=None)
        self.cat = baker.make("dashboard.Label", name="c", kind=KIND_CATEGORY, profile=None)
        self.status = baker.make("dashboard.Label", name="s", kind=KIND_STATUS, profile=None)

    def test_tags_includes_tag_excludes_category_and_status(self) -> None:
        qs = Label.objects.tags()
        self.assertIn(self.tag, qs)
        self.assertNotIn(self.cat, qs)
        self.assertNotIn(self.status, qs)

    def test_categories_includes_category_excludes_others(self) -> None:
        qs = Label.objects.categories()
        self.assertIn(self.cat, qs)
        self.assertNotIn(self.tag, qs)

    def test_statuses_includes_status_excludes_others(self) -> None:
        qs = Label.objects.statuses()
        self.assertIn(self.status, qs)
        self.assertNotIn(self.tag, qs)


# -- LabelQuerySet.visible_to / global_only / for_profile ---------------------


class LabelQuerySetVisibilityTests(TestCase):
    """visible_to, global_only, and for_profile filter by label ownership."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.other = baker.make("auth.User")
        self.global_b = baker.make("dashboard.Label", name="g", profile=None, kind=KIND_TAG)
        self.user_b = baker.make("dashboard.Label", name="u", profile=self.user.profile, kind=KIND_TAG)
        self.other_b = baker.make("dashboard.Label", name="o", profile=self.other.profile, kind=KIND_TAG)

    def test_visible_to_includes_global_and_own_labels(self) -> None:
        qs = Label.objects.visible_to(self.user.profile)
        self.assertIn(self.global_b, qs)
        self.assertIn(self.user_b, qs)

    def test_visible_to_excludes_other_users_labels(self) -> None:
        qs = Label.objects.visible_to(self.user.profile)
        self.assertNotIn(self.other_b, qs)

    def test_visible_to_accepts_int_pk(self) -> None:
        qs = Label.objects.visible_to(self.user.profile.pk)
        self.assertIn(self.global_b, qs)
        self.assertIn(self.user_b, qs)

    def test_global_only_includes_global_and_excludes_user(self) -> None:
        qs = Label.objects.global_only()
        self.assertIn(self.global_b, qs)
        self.assertNotIn(self.user_b, qs)
        self.assertNotIn(self.other_b, qs)

    def test_for_profile_returns_only_that_profile(self) -> None:
        qs = Label.objects.for_profile(self.user.profile)
        self.assertIn(self.user_b, qs)
        self.assertNotIn(self.global_b, qs)
        self.assertNotIn(self.other_b, qs)

    def test_for_profile_accepts_int_pk(self) -> None:
        qs = Label.objects.for_profile(self.user.profile.pk)
        self.assertIn(self.user_b, qs)
        self.assertNotIn(self.global_b, qs)


# -- LabelQuerySet.with_icon ---------------------------------------------------


class LabelQuerySetWithIconTests(TestCase):
    """with_icon() returns labels that have a non-empty icon or custom_icon."""

    def setUp(self):
        self.with_icon = baker.make(
            "dashboard.Label",
            name="starred",
            icon="⭐",
            custom_icon=None,
            profile=None,
            kind=KIND_TAG,
        )
        self.no_icon = baker.make(
            "dashboard.Label",
            name="plain",
            icon=None,
            custom_icon=None,
            profile=None,
            kind=KIND_TAG,
        )

    def test_includes_label_with_icon(self) -> None:
        qs = Label.objects.with_icon()
        self.assertIn(self.with_icon, qs)

    def test_excludes_label_without_icon(self) -> None:
        qs = Label.objects.with_icon()
        self.assertNotIn(self.no_icon, qs)


# -- LabelQuerySet.ordered -----------------------------------------------------


class LabelQuerySetOrderedTests(TestCase):
    """ordered() returns a queryset ordered by -order then name."""

    def test_ordered_returns_queryset(self) -> None:
        baker.make("dashboard.Label", name="z", order=1, profile=None, kind=KIND_TAG)
        baker.make("dashboard.Label", name="a", order=2, profile=None, kind=KIND_TAG)
        qs = Label.objects.ordered()
        self.assertGreater(qs.count(), 0)

    def test_ordered_higher_order_comes_first(self) -> None:
        b_low: Label = baker.make(Label, name="low", order=1, profile=None, kind=KIND_TAG)
        b_high: Label = baker.make(Label, name="high", order=10, profile=None, kind=KIND_TAG)
        pks = list(Label.objects.filter(pk__in=[b_low.pk, b_high.pk]).ordered().values_list("pk", flat=True))
        self.assertEqual(pks[0], b_high.pk)


# -- LabelQuerySet.with_customizations_for ------------------------------------


class LabelQuerySetWithCustomizationsForTests(TestCase):
    """with_customizations_for() prefetches LabelCustomization rows into _user_customizations."""

    def setUp(self):
        from urbanlens.dashboard.models.labels.customization import LabelCustomization

        self.LabelCustomization = LabelCustomization
        self.user = baker.make("auth.User")
        self.other = baker.make("auth.User")
        self.label = baker.make(Label, name="Global", profile=None, kind=KIND_TAG)

    def test_prefetch_populates_user_customizations_attr(self) -> None:
        # Create a customization for this user.
        baker.make(
            self.LabelCustomization,
            profile=self.user.profile,
            label=self.label,
            name="My Override",
        )
        qs = Label.objects.filter(pk=self.label.pk).with_customizations_for(self.user.profile)
        result = qs.get()
        customizations = getattr(result, "_user_customizations", [])
        self.assertEqual(len(customizations), 1)
        self.assertEqual(customizations[0].name, "My Override")

    def test_prefetch_empty_when_no_customization_exists(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_customizations_for(self.user.profile)
        result = qs.get()
        customizations = getattr(result, "_user_customizations", [])
        self.assertEqual(len(customizations), 0)

    def test_prefetch_excludes_other_users_customizations(self) -> None:
        # Only the other user has a customization - our user should see empty list.
        baker.make(
            self.LabelCustomization,
            profile=self.other.profile,
            label=self.label,
            name="Other Override",
        )
        qs = Label.objects.filter(pk=self.label.pk).with_customizations_for(self.user.profile)
        result = qs.get()
        customizations = getattr(result, "_user_customizations", [])
        self.assertEqual(len(customizations), 0)

    def test_accepts_int_profile_pk(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_customizations_for(self.user.profile.pk)
        result = qs.get()
        self.assertTrue(hasattr(result, "_user_customizations"))


# -- LabelQuerySet.with_pin_counts ---------------------------------------------


class LabelQuerySetWithPinCountsTests(TestCase):
    """with_pin_counts() annotates pin_count and location_count on each label."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.label = baker.make(Label, name="Pinned", profile=None, kind=KIND_TAG)

    def test_annotates_pin_count_attribute(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_pin_counts()
        result = qs.get()
        self.assertTrue(hasattr(result, "pin_count"))

    def test_annotates_location_count_attribute(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_pin_counts()
        result = qs.get()
        self.assertTrue(hasattr(result, "location_count"))

    def test_pin_count_is_zero_when_no_pins(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_pin_counts()
        result = qs.get()
        self.assertEqual(result.pin_count, 0)

    def test_location_count_is_zero_when_no_locations(self) -> None:
        qs = Label.objects.filter(pk=self.label.pk).with_pin_counts()
        result = qs.get()
        self.assertEqual(result.location_count, 0)


# -- Label.get_label_and_descendants ------------------------------------------


class LabelGetLabelAndDescendantsTests(TestCase):
    """get_label_and_descendants() BFS returns a label and all its descendants."""

    def test_single_label_with_no_children_returns_itself(self) -> None:
        b = baker.make(Label, name="leaf", profile=None, kind=KIND_TAG)
        result = Label.get_label_and_descendants(b.pk)
        self.assertEqual(result, {b.pk})

    def test_returns_parent_and_direct_child(self) -> None:
        parent = baker.make(Label, name="parent", profile=None, kind=KIND_TAG)
        child = baker.make(Label, name="child", profile=None, kind=KIND_TAG)
        child.parents.add(parent)
        result = Label.get_label_and_descendants(parent.pk)
        self.assertIn(parent.pk, result)
        self.assertIn(child.pk, result)

    def test_returns_multi_level_descendants(self) -> None:
        grandparent = baker.make(Label, name="gp", profile=None, kind=KIND_TAG)
        parent = baker.make(Label, name="p", profile=None, kind=KIND_TAG)
        child = baker.make(Label, name="c", profile=None, kind=KIND_TAG)
        parent.parents.add(grandparent)
        child.parents.add(parent)
        result = Label.get_label_and_descendants(grandparent.pk)
        self.assertIn(grandparent.pk, result)
        self.assertIn(parent.pk, result)
        self.assertIn(child.pk, result)

    def test_does_not_include_unrelated_label(self) -> None:
        b1 = baker.make(Label, name="b1", profile=None, kind=KIND_TAG)
        unrelated = baker.make(Label, name="unrelated", profile=None, kind=KIND_TAG)
        result = Label.get_label_and_descendants(b1.pk)
        self.assertNotIn(unrelated.pk, result)

    def test_cycle_safe(self) -> None:
        # A -> B -> A (cycle) should not loop infinitely.
        a = baker.make(Label, name="a", profile=None, kind=KIND_TAG)
        b = baker.make(Label, name="b", profile=None, kind=KIND_TAG)
        b.parents.add(a)
        a.parents.add(b)
        result = Label.get_label_and_descendants(a.pk)
        self.assertIn(a.pk, result)
        self.assertIn(b.pk, result)

    def test_returns_set_not_list(self) -> None:
        b = baker.make(Label, name="s", profile=None, kind=KIND_TAG)
        result = Label.get_label_and_descendants(b.pk)
        self.assertIsInstance(result, set)


# -- Label.initial_order_for_parents -------------------------------------------


class LabelInitialOrderForParentsTests(TestCase):
    """initial_order_for_parents() derives order from selected parent labels."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_returns_none_without_parent_ids(self) -> None:
        self.assertIsNone(Label.initial_order_for_parents(self.profile, []))

    def test_returns_none_when_parents_not_visible(self) -> None:
        other = baker.make("auth.User")
        hidden = baker.make(Label, name="hidden", profile=other.profile, kind=KIND_TAG, order=50)
        self.assertIsNone(Label.initial_order_for_parents(self.profile, [hidden.pk]))

    def test_single_parent_order_minus_one(self) -> None:
        parent = baker.make(Label, name="Hospital", profile=self.profile, kind=KIND_CATEGORY, order=20)
        self.assertEqual(Label.initial_order_for_parents(self.profile, [parent.pk]), 19)

    def test_multiple_parents_uses_lowest_order(self) -> None:
        hospital = baker.make(Label, name="Hospital", profile=self.profile, kind=KIND_CATEGORY, order=20)
        pennsylvania = baker.make(Label, name="Pennsylvania", profile=self.profile, kind=KIND_TAG, order=35)
        self.assertEqual(
            Label.initial_order_for_parents(self.profile, [hospital.pk, pennsylvania.pk]),
            19,
        )


# -- Profile default labels ---------------------------------------------------


class ProfileDefaultLabelSignalTests(TestCase):
    """New user profiles receive ordinary starter tag labels."""

    def test_new_user_gets_editable_default_tag_labels(self) -> None:
        user = baker.make(User)

        labels = Label.objects.filter(
            profile=user.profile,
            kind=KIND_TAG,
            name__in=[
                "Notable",
                "Graffiti",
                "Photography",
                "Dangerous",
                "Popular",
            ],
        )

        self.assertEqual(labels.count(), 5)
        self.assertFalse(labels.filter(is_protected=True).exists())
