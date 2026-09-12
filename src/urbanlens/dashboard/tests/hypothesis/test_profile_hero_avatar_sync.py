"""Regression guard for the edit-profile page hero avatar not refreshing."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


class ProfileHeroAvatarIdTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)

    def test_edit_page_hero_avatar_has_a_stable_id(self) -> None:
        response = self.client.get(reverse("profile.edit"))
        self.assertContains(response, 'id="profile-hero-avatar"')

    def test_edit_page_avatar_placeholder_also_carries_the_id_when_no_avatar_set(self) -> None:
        self.user.profile.avatar = None
        self.user.profile.save(update_fields=["avatar"])
        response = self.client.get(reverse("profile.edit"))
        self.assertContains(response, 'id="profile-hero-avatar"')

    def test_index_page_hero_avatar_also_carries_the_id(self) -> None:
        """profile/index.html shares _profile_hero_body.html with edit.html -
        the id must be present there too, not just on the edit page."""
        response = self.client.get(reverse("profile.view"))
        self.assertContains(response, 'id="profile-hero-avatar"')


class ProfileBioShownOnceTests(TestCase):
    """The hero no longer renders it at all; only the About section does."""

    def setUp(self) -> None:
        super().setUp()
        self.bio = "Exploring abandoned places since childhood."
        self.user = baker.make(User)
        self.user.profile.bio = self.bio
        self.user.profile.save(update_fields=["bio"])
        self.client = Client()
        self.client.force_login(self.user)

    def test_bio_appears_exactly_once_as_visible_text(self) -> None:
        content = self.client.get(reverse("profile.view")).content.decode()
        self.assertEqual(content.count(f">{self.bio}"), 1)
        self.assertEqual(content.count(self.bio), 2)

    def test_bio_lives_in_the_about_section_not_the_hero(self) -> None:
        content = self.client.get(reverse("profile.view")).content.decode()
        hero_start = content.index('id="profile-hero"')
        about_start = content.index("profile-bio-full")
        self.assertLess(hero_start, about_start)
        # The bio text itself must not appear before the About section's own
        # marker - i.e. not inside the hero markup that precedes it.
        bio_idx = content.index(self.bio)
        self.assertGreater(bio_idx, about_start)
