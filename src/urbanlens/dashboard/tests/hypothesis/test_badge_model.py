"""Tests for Badge model properties and BadgeQuerySet filter methods.

get_badge_and_descendants is already thoroughly covered in test_badge.py.
This file covers the customization-aware display properties and queryset filters.

Property tests use unsaved Badge instances with _user_customizations injected
directly — no DB access required.  Queryset tests use baker.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.dashboard.models.badges.model import (
	Badge,
	KIND_CATEGORY,
	KIND_STATUS,
	KIND_TAG,
)


_hyp = settings(max_examples=50, deadline=None)
_text = st.text(min_size=1, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N")))


def _badge(name: str = "test-badge", icon: str | None = None, color: str | None = None) -> Badge:
	"""Create an unsaved Badge with no customization prefetched."""
	b = Badge()
	b.name = name
	b.icon = icon
	b.color = color
	b.__dict__["_user_customizations"] = []
	return b


def _custom(name: str | None = None, icon: str | None = None, color: str | None = None):
	"""Simulate a prefetched BadgeCustomization row."""
	return SimpleNamespace(name=name, icon=icon, color=color)


# ── _get_customization ────────────────────────────────────────────────────────

class BadgeGetCustomizationTests(TestCase):
	"""_get_customization() returns the first prefetched item or None."""

	def test_returns_none_with_empty_list(self) -> None:
		b = _badge()
		self.assertIsNone(b._get_customization())

	def test_returns_first_prefetched_item(self) -> None:
		b = _badge()
		c = _custom(name="Override")
		b.__dict__["_user_customizations"] = [c]
		self.assertIs(b._get_customization(), c)


# ── effective_name ────────────────────────────────────────────────────────────

class BadgeEffectiveNameTests(TestCase):
	"""effective_name returns the user's override or falls back to the badge name."""

	def test_no_customization_returns_badge_name(self) -> None:
		self.assertEqual(_badge(name="Global Tag").effective_name, "Global Tag")

	def test_customization_with_name_overrides(self) -> None:
		b = _badge(name="Global Tag")
		b.__dict__["_user_customizations"] = [_custom(name="My Name")]
		self.assertEqual(b.effective_name, "My Name")

	def test_customization_with_none_name_falls_back(self) -> None:
		b = _badge(name="Global Tag")
		b.__dict__["_user_customizations"] = [_custom(name=None)]
		self.assertEqual(b.effective_name, "Global Tag")

	def test_customization_with_empty_string_falls_back(self) -> None:
		b = _badge(name="Global Tag")
		b.__dict__["_user_customizations"] = [_custom(name="")]
		self.assertEqual(b.effective_name, "Global Tag")

	@given(_text)
	@_hyp
	def test_override_name_is_always_returned_when_truthy(self, name: str) -> None:
		b = _badge(name="Fallback")
		b.__dict__["_user_customizations"] = [_custom(name=name)]
		self.assertEqual(b.effective_name, name)


# ── effective_icon ────────────────────────────────────────────────────────────

class BadgeEffectiveIconTests(TestCase):
	"""effective_icon returns the user's override or falls back to the badge icon."""

	def test_no_customization_returns_badge_icon(self) -> None:
		self.assertEqual(_badge(icon="star").effective_icon, "star")

	def test_customization_with_icon_overrides(self) -> None:
		b = _badge(icon="star")
		b.__dict__["_user_customizations"] = [_custom(icon="heart")]
		self.assertEqual(b.effective_icon, "heart")

	def test_customization_with_none_icon_falls_back(self) -> None:
		b = _badge(icon="star")
		b.__dict__["_user_customizations"] = [_custom(icon=None)]
		self.assertEqual(b.effective_icon, "star")

	def test_no_icon_anywhere_returns_none(self) -> None:
		b = _badge(icon=None)
		self.assertIsNone(b.effective_icon)


# ── effective_color ───────────────────────────────────────────────────────────

class BadgeEffectiveColorTests(TestCase):
	"""effective_color returns the user's override or falls back to the badge color."""

	def test_no_customization_returns_badge_color(self) -> None:
		self.assertEqual(_badge(color="#ff0000").effective_color, "#ff0000")

	def test_customization_with_color_overrides(self) -> None:
		b = _badge(color="#ff0000")
		b.__dict__["_user_customizations"] = [_custom(color="#00ff00")]
		self.assertEqual(b.effective_color, "#00ff00")

	def test_customization_with_none_color_falls_back(self) -> None:
		b = _badge(color="#ff0000")
		b.__dict__["_user_customizations"] = [_custom(color=None)]
		self.assertEqual(b.effective_color, "#ff0000")

	def test_no_color_anywhere_returns_none(self) -> None:
		b = _badge(color=None)
		self.assertIsNone(b.effective_color)


# ── is_customized ─────────────────────────────────────────────────────────────

class BadgeIsCustomizedTests(TestCase):
	"""is_customized is True when the prefetched customization has any non-None field."""

	def test_no_customization_is_false(self) -> None:
		self.assertFalse(_badge().is_customized)

	def test_customization_with_name_is_true(self) -> None:
		b = _badge()
		b.__dict__["_user_customizations"] = [_custom(name="X")]
		self.assertTrue(b.is_customized)

	def test_customization_with_icon_is_true(self) -> None:
		b = _badge()
		b.__dict__["_user_customizations"] = [_custom(icon="🏭")]
		self.assertTrue(b.is_customized)

	def test_customization_with_color_is_true(self) -> None:
		b = _badge()
		b.__dict__["_user_customizations"] = [_custom(color="#abc")]
		self.assertTrue(b.is_customized)

	def test_all_none_customization_is_false(self) -> None:
		b = _badge()
		b.__dict__["_user_customizations"] = [_custom(name=None, icon=None, color=None)]
		self.assertFalse(b.is_customized)


# ── icon_is_overridden ────────────────────────────────────────────────────────

class BadgeIconIsOverriddenTests(TestCase):
	"""icon_is_overridden is True only when customization.icon is not None."""

	def test_no_customization_is_false(self) -> None:
		self.assertFalse(_badge(icon="star").icon_is_overridden)

	def test_customization_with_icon_is_true(self) -> None:
		b = _badge(icon="star")
		b.__dict__["_user_customizations"] = [_custom(icon="heart")]
		self.assertTrue(b.icon_is_overridden)

	def test_customization_with_none_icon_is_false(self) -> None:
		b = _badge(icon="star")
		b.__dict__["_user_customizations"] = [_custom(icon=None, name="Override")]
		self.assertFalse(b.icon_is_overridden)


# ── Badge.__str__ ─────────────────────────────────────────────────────────────

class BadgeStrTests(TestCase):
	"""__str__ includes [global] for shared badges and (profile) for user-owned ones."""

	def test_global_badge_has_global_label(self) -> None:
		b: Badge = baker.make(Badge, name="Urban Ruin", profile=None, kind=KIND_TAG)
		self.assertIn("[global]", str(b))
		self.assertIn("Urban Ruin", str(b))

	def test_user_badge_excludes_global_label(self) -> None:
		user: User = baker.make(User)
		b: Badge = baker.make(Badge, name="Mine", profile=user.profile, kind=KIND_TAG)
		self.assertIn("Mine", str(b))
		self.assertNotIn("[global]", str(b))


# ── BadgeQuerySet.tags / categories / statuses ────────────────────────────────

class BadgeQuerySetKindTests(TestCase):
	"""tags(), categories(), and statuses() filter by the kind field."""

	def setUp(self):
		self.tag = baker.make("dashboard.Badge", name="t", kind=KIND_TAG, profile=None)
		self.cat = baker.make("dashboard.Badge", name="c", kind=KIND_CATEGORY, profile=None)
		self.status = baker.make("dashboard.Badge", name="s", kind=KIND_STATUS, profile=None)

	def test_tags_includes_tag_excludes_category_and_status(self) -> None:
		qs = Badge.objects.tags()
		self.assertIn(self.tag, qs)
		self.assertNotIn(self.cat, qs)
		self.assertNotIn(self.status, qs)

	def test_categories_includes_category_excludes_others(self) -> None:
		qs = Badge.objects.categories()
		self.assertIn(self.cat, qs)
		self.assertNotIn(self.tag, qs)

	def test_statuses_includes_status_excludes_others(self) -> None:
		qs = Badge.objects.statuses()
		self.assertIn(self.status, qs)
		self.assertNotIn(self.tag, qs)


# ── BadgeQuerySet.visible_to / global_only / for_profile ─────────────────────

class BadgeQuerySetVisibilityTests(TestCase):
	"""visible_to, global_only, and for_profile filter by badge ownership."""

	def setUp(self):
		self.user = baker.make("auth.User")
		self.other = baker.make("auth.User")
		self.global_b = baker.make("dashboard.Badge", name="g", profile=None, kind=KIND_TAG)
		self.user_b = baker.make("dashboard.Badge", name="u", profile=self.user.profile, kind=KIND_TAG)
		self.other_b = baker.make("dashboard.Badge", name="o", profile=self.other.profile, kind=KIND_TAG)

	def test_visible_to_includes_global_and_own_badges(self) -> None:
		qs = Badge.objects.visible_to(self.user.profile)
		self.assertIn(self.global_b, qs)
		self.assertIn(self.user_b, qs)

	def test_visible_to_excludes_other_users_badges(self) -> None:
		qs = Badge.objects.visible_to(self.user.profile)
		self.assertNotIn(self.other_b, qs)

	def test_visible_to_accepts_int_pk(self) -> None:
		qs = Badge.objects.visible_to(self.user.profile.pk)
		self.assertIn(self.global_b, qs)
		self.assertIn(self.user_b, qs)

	def test_global_only_includes_global_and_excludes_user(self) -> None:
		qs = Badge.objects.global_only()
		self.assertIn(self.global_b, qs)
		self.assertNotIn(self.user_b, qs)
		self.assertNotIn(self.other_b, qs)

	def test_for_profile_returns_only_that_profile(self) -> None:
		qs = Badge.objects.for_profile(self.user.profile)
		self.assertIn(self.user_b, qs)
		self.assertNotIn(self.global_b, qs)
		self.assertNotIn(self.other_b, qs)

	def test_for_profile_accepts_int_pk(self) -> None:
		qs = Badge.objects.for_profile(self.user.profile.pk)
		self.assertIn(self.user_b, qs)
		self.assertNotIn(self.global_b, qs)


# ── BadgeQuerySet.with_icon ───────────────────────────────────────────────────

class BadgeQuerySetWithIconTests(TestCase):
	"""with_icon() returns badges that have a non-empty icon or custom_icon."""

	def setUp(self):
		self.with_icon = baker.make(
			"dashboard.Badge", name="starred", icon="⭐", custom_icon=None, profile=None, kind=KIND_TAG
		)
		self.no_icon = baker.make(
			"dashboard.Badge", name="plain", icon=None, custom_icon=None, profile=None, kind=KIND_TAG
		)

	def test_includes_badge_with_icon(self) -> None:
		qs = Badge.objects.with_icon()
		self.assertIn(self.with_icon, qs)

	def test_excludes_badge_without_icon(self) -> None:
		qs = Badge.objects.with_icon()
		self.assertNotIn(self.no_icon, qs)


# ── BadgeQuerySet.ordered ─────────────────────────────────────────────────────

class BadgeQuerySetOrderedTests(TestCase):
	"""ordered() returns a queryset ordered by -order then name."""

	def test_ordered_returns_queryset(self) -> None:
		baker.make("dashboard.Badge", name="z", order=1, profile=None, kind=KIND_TAG)
		baker.make("dashboard.Badge", name="a", order=2, profile=None, kind=KIND_TAG)
		qs = Badge.objects.ordered()
		self.assertGreater(qs.count(), 0)

	def test_ordered_higher_order_comes_first(self) -> None:
		b_low: Badge = baker.make(Badge, name="low", order=1, profile=None, kind=KIND_TAG)
		b_high: Badge = baker.make(Badge, name="high", order=10, profile=None, kind=KIND_TAG)
		pks = list(Badge.objects.filter(pk__in=[b_low.pk, b_high.pk]).ordered().values_list("pk", flat=True))
		self.assertEqual(pks[0], b_high.pk)
