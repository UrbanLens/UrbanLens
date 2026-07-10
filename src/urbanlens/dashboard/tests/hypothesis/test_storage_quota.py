"""Tests for the storage quota service and upload downscale policy.

Covers:
- get_quota_bytes() - site default, role overrides, largest-wins, 0 = unlimited
- get_storage_used_bytes() - sums file_size, skipping unmeasured rows
- quota_error_for_upload() - boundary behaviour at the quota edge
- get_downscale_policy() / get_entitled_policy() - site policy, subscriber
  exemption, and the user's voluntary cap (which can only tighten)
- estimate_bytes_per_photo() / estimate_photos_remaining() - monotonicity
- allowed_user_dimension_values() - only caps below the entitlement
"""

from __future__ import annotations

from django.contrib.auth.models import User
from hypothesis import given, settings as hyp_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions.model import SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.storage import (
    DOWNSCALE_DIMENSION_CHOICES,
    GIB,
    allowed_user_dimension_values,
    estimate_bytes_per_photo,
    estimate_photos_remaining,
    get_downscale_policy,
    get_entitled_policy,
    get_quota_bytes,
    get_storage_used_bytes,
    quota_error_for_upload,
)

_hyp = hyp_settings(max_examples=40, deadline=None)


def _make_profile():
    return baker.make(User).profile


def _grant_role(profile, **role_fields):
    role = baker.make(SubscriptionRole, slug=role_fields.pop("slug", "vip"), **role_fields)
    granter = baker.make(User)
    grant_subscription(profile.user, role, granter, months=None)
    return role


class QuotaResolutionTests(TestCase):
    """get_quota_bytes() combines the site default with role overrides."""

    def test_site_default_applies_without_roles(self):
        profile = _make_profile()
        self.assertEqual(get_quota_bytes(profile), 10 * GIB)

    def test_role_quota_wins_when_larger(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=500)
        self.assertEqual(get_quota_bytes(profile), 500 * GIB)

    def test_smaller_role_quota_never_shrinks_default(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=1)
        self.assertEqual(get_quota_bytes(profile), 10 * GIB)

    def test_role_without_quota_is_ignored(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=None)
        self.assertEqual(get_quota_bytes(profile), 10 * GIB)

    def test_zero_means_unlimited(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=0)
        self.assertIsNone(get_quota_bytes(profile))

    def test_site_default_zero_means_unlimited(self):
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(storage_quota_gb=0)
        profile = _make_profile()
        self.assertIsNone(get_quota_bytes(profile))

    def test_revoked_subscription_does_not_count(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=500)
        from urbanlens.dashboard.models.subscriptions.model import UserSubscription

        for sub in UserSubscription.objects.filter(user=profile.user):
            sub.revoke()
        self.assertEqual(get_quota_bytes(profile), 10 * GIB)


class StorageUsageTests(TestCase):
    """get_storage_used_bytes() sums measured rows only."""

    def test_sums_file_sizes_and_skips_unmeasured(self):
        profile = _make_profile()
        baker.make("dashboard.Image", profile=profile, file_size=100)
        baker.make("dashboard.Image", profile=profile, file_size=250)
        baker.make("dashboard.Image", profile=profile, file_size=None)
        other = _make_profile()
        baker.make("dashboard.Image", profile=other, file_size=999)
        self.assertEqual(get_storage_used_bytes(profile), 350)

    def test_zero_when_no_uploads(self):
        self.assertEqual(get_storage_used_bytes(_make_profile()), 0)


class QuotaErrorTests(TestCase):
    """quota_error_for_upload() rejects only uploads that would exceed the quota."""

    def test_allows_upload_exactly_at_quota(self):
        profile = _make_profile()
        baker.make("dashboard.Image", profile=profile, file_size=10 * GIB - 5)
        self.assertIsNone(quota_error_for_upload(profile, 5))

    def test_rejects_upload_past_quota(self):
        profile = _make_profile()
        baker.make("dashboard.Image", profile=profile, file_size=10 * GIB - 5)
        error = quota_error_for_upload(profile, 6)
        self.assertIsNotNone(error)
        self.assertIn("storage quota", error)

    def test_unlimited_never_rejects(self):
        profile = _make_profile()
        _grant_role(profile, storage_quota_gb=0)
        baker.make("dashboard.Image", profile=profile, file_size=50 * GIB)
        self.assertIsNone(quota_error_for_upload(profile, 10 * GIB))


class DownscalePolicyTests(TestCase):
    """Site policy, subscriber exemption, and voluntary user caps."""

    def test_default_policy_uses_site_dimension(self):
        profile = _make_profile()
        self.assertEqual(get_downscale_policy(profile), (1920, True))

    def test_disabled_downscale_means_no_cap(self):
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(image_downscale_enabled=False)
        profile = _make_profile()
        self.assertEqual(get_downscale_policy(profile), (None, True))

    def test_subscriber_exempt_by_default(self):
        profile = _make_profile()
        _grant_role(profile)
        self.assertEqual(get_entitled_policy(profile), (None, False))

    def test_subscriber_not_exempt_when_admin_says_so(self):
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(image_downscale_vip=True, image_convert_webp=True)
        profile = _make_profile()
        _grant_role(profile)
        self.assertEqual(get_entitled_policy(profile), (1920, True))

    def test_user_cap_tightens_policy(self):
        profile = _make_profile()
        profile.image_downscale_max_dimension = 1280
        self.assertEqual(get_downscale_policy(profile), (1280, True))

    def test_user_cap_cannot_loosen_policy(self):
        profile = _make_profile()
        profile.image_downscale_max_dimension = 9999
        self.assertEqual(get_downscale_policy(profile), (1920, True))

    def test_exempt_subscriber_can_still_opt_in(self):
        profile = _make_profile()
        _grant_role(profile)
        profile.image_downscale_max_dimension = 1920
        self.assertEqual(get_downscale_policy(profile), (1920, False))

    def test_webp_follows_admin_setting(self):
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(image_convert_webp=True)
        profile = _make_profile()
        self.assertEqual(get_downscale_policy(profile), (1920, True))

    def test_allowed_values_are_below_entitlement(self):
        profile = _make_profile()
        allowed = allowed_user_dimension_values(profile)
        self.assertTrue(all(dim < 1920 for dim in allowed))
        self.assertNotIn(1920, allowed)

    def test_allowed_values_unrestricted_when_no_entitlement(self):
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(image_downscale_enabled=False)
        profile = _make_profile()
        self.assertEqual(allowed_user_dimension_values(profile), {dim for dim, _ in DOWNSCALE_DIMENSION_CHOICES})


class EstimateTests(TestCase):
    """Photo-count estimates behave sensibly."""

    @given(dimension=st.integers(min_value=256, max_value=8000))
    @_hyp
    def test_webp_estimates_smaller_than_jpeg(self, dimension: int):
        self.assertLessEqual(
            estimate_bytes_per_photo(dimension, convert_webp=True),
            estimate_bytes_per_photo(dimension, convert_webp=False),
        )

    @given(smaller=st.integers(min_value=256, max_value=4000), delta=st.integers(min_value=200, max_value=4000))
    @_hyp
    def test_smaller_dimension_fits_more_photos(self, smaller: int, delta: int):
        budget = 10 * GIB
        self.assertGreaterEqual(
            estimate_photos_remaining(budget, smaller, False),
            estimate_photos_remaining(budget, smaller + delta, False),
        )

    @given(budget=st.integers(min_value=0, max_value=100 * GIB))
    @_hyp
    def test_estimate_never_negative(self, budget: int):
        self.assertGreaterEqual(estimate_photos_remaining(budget, None, False), 0)

    def test_original_size_assumed_when_no_cap(self):
        # No cap estimates against a 12 MP original, so it must never fit more
        # photos than any downscaled setting.
        budget = GIB
        capped = estimate_photos_remaining(budget, 1920, False)
        original = estimate_photos_remaining(budget, None, False)
        self.assertLessEqual(original, capped)
