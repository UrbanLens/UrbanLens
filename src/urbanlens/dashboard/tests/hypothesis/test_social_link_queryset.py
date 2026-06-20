"""Tests for SocialLink QuerySet methods: for_profile and platform.

All tests require the database — records are created with model_bakery.
"""
from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.social_link.model import SocialLink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_link(profile, platform: str = "instagram", handle: str = "testuser") -> SocialLink:
    """Create a SocialLink for a profile."""
    return baker.make(SocialLink, profile=profile, platform=platform, handle=handle)


# ---------------------------------------------------------------------------
# for_profile — Profile instance path (line 18)
# ---------------------------------------------------------------------------

class SocialLinkForProfileInstanceTests(TestCase):
    """for_profile(profile_instance) filters by the model object."""

    def setUp(self):
        self.user_a = baker.make("auth.User")
        self.user_b = baker.make("auth.User")
        self.profile_a = self.user_a.profile
        self.profile_b = self.user_b.profile

        self.link_a = _make_link(self.profile_a, platform="instagram")
        self.link_b = _make_link(self.profile_b, platform="bluesky")

    def test_returns_link_for_given_profile_instance(self):
        qs = SocialLink.objects.for_profile(self.profile_a)
        self.assertIn(self.link_a, qs)

    def test_excludes_link_for_other_profile_instance(self):
        qs = SocialLink.objects.for_profile(self.profile_a)
        self.assertNotIn(self.link_b, qs)

    def test_other_profile_returns_its_own_links(self):
        qs = SocialLink.objects.for_profile(self.profile_b)
        self.assertIn(self.link_b, qs)

    def test_profile_with_no_links_returns_empty_queryset(self):
        user_c = baker.make("auth.User")
        qs = SocialLink.objects.for_profile(user_c.profile)
        self.assertFalse(qs.exists())

    def test_multiple_links_for_same_profile_all_returned(self):
        link_a2 = _make_link(self.profile_a, platform="bluesky")
        qs = SocialLink.objects.for_profile(self.profile_a)
        self.assertIn(self.link_a, qs)
        self.assertIn(link_a2, qs)
        self.assertEqual(qs.count(), 2)


# ---------------------------------------------------------------------------
# for_profile — integer pk path (lines 16-17)
# ---------------------------------------------------------------------------

class SocialLinkForProfileIntTests(TestCase):
    """for_profile(int) filters by profile_id FK."""

    def setUp(self):
        self.user_a = baker.make("auth.User")
        self.user_b = baker.make("auth.User")
        self.profile_a = self.user_a.profile
        self.profile_b = self.user_b.profile

        self.link_a = _make_link(self.profile_a, platform="flickr")
        self.link_b = _make_link(self.profile_b, platform="500px")

    def test_returns_link_for_correct_profile_id(self):
        qs = SocialLink.objects.for_profile(self.profile_a.pk)
        self.assertIn(self.link_a, qs)

    def test_excludes_link_for_other_profile_id(self):
        qs = SocialLink.objects.for_profile(self.profile_a.pk)
        self.assertNotIn(self.link_b, qs)

    def test_nonexistent_profile_id_returns_empty(self):
        qs = SocialLink.objects.for_profile(999999)
        self.assertFalse(qs.exists())

    def test_int_and_instance_return_same_results(self):
        qs_by_int = SocialLink.objects.for_profile(self.profile_a.pk)
        qs_by_instance = SocialLink.objects.for_profile(self.profile_a)
        self.assertQuerySetEqual(qs_by_int.order_by("pk"), qs_by_instance.order_by("pk"))


# ---------------------------------------------------------------------------
# platform (line 22)
# ---------------------------------------------------------------------------

class SocialLinkPlatformTests(TestCase):
    """platform(key) filters to links with that exact platform string."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

        self.instagram = _make_link(self.profile, platform="instagram", handle="insta_user")
        self.bluesky = _make_link(self.profile, platform="bluesky", handle="bsky_user")

    def test_returns_link_with_matching_platform(self):
        qs = SocialLink.objects.for_profile(self.profile).platform("instagram")
        self.assertIn(self.instagram, qs)

    def test_excludes_link_with_different_platform(self):
        qs = SocialLink.objects.for_profile(self.profile).platform("instagram")
        self.assertNotIn(self.bluesky, qs)

    def test_unknown_platform_returns_empty(self):
        qs = SocialLink.objects.for_profile(self.profile).platform("nonexistent_platform")
        self.assertFalse(qs.exists())

    def test_platform_filter_is_case_sensitive(self):
        # Platform keys are stored as-is; "Instagram" should not match "instagram"
        qs = SocialLink.objects.for_profile(self.profile).platform("Instagram")
        self.assertFalse(qs.exists())

    def test_platform_chainable_with_for_profile(self):
        # Ensure we can chain for_profile().platform() fluently
        qs = SocialLink.objects.for_profile(self.profile).platform("bluesky")
        self.assertIn(self.bluesky, qs)
        self.assertEqual(qs.count(), 1)
