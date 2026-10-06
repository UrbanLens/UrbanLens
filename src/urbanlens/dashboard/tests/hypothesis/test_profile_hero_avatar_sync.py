"""Regression guards for the edit-profile page's avatars not refreshing: each avatar of the user's is marked for the page script, and an upload's progress can be asked after."""

from __future__ import annotations

import re

from defusedxml import ElementTree
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.profile.avatar import AvatarService


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


def _marked(html: str) -> list[str]:
    """The ``data-user-avatar`` value of every element that carries one."""
    return re.findall(r'data-user-avatar="([^"]*)"', html)


class UserAvatarMarkerTests(TestCase):
    """``data-user-avatar`` is how the page script finds every avatar to redraw when the user picks a new one."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User, username="urbex_owl")
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_edit_page_marks_the_form_the_hero_and_both_navbar_avatars(self) -> None:
        html = self.client.get(reverse("profile.edit")).content.decode()
        self.assertEqual(
            sorted(_marked(html)), ["edit-avatar-preview", "nav-avatar-img", "nav-avatar-img", "profile-avatar-img"]
        )

    def test_the_same_marks_sit_on_the_image_once_an_avatar_is_set(self) -> None:
        Profile.objects.filter(user=self.user).update(avatar="avatars/mine.png")
        html = self.client.get(reverse("profile.edit")).content.decode()
        self.assertEqual(
            sorted(_marked(html)), ["edit-avatar-preview", "nav-avatar-img", "nav-avatar-img", "profile-avatar-img"]
        )
        for tag in re.findall(r"<img\b[^>]*data-user-avatar[^>]*>", html):
            self.assertIn("/avatars/mine.png", tag)

    def test_the_navbar_marks_say_to_keep_the_image_decorative(self) -> None:
        html = self.client.get(reverse("profile.edit")).content.decode()
        nav = re.findall(r'<[^>]*data-user-avatar="nav-avatar-img"[^>]*>', html)
        self.assertEqual(len(nav), 2)
        for tag in nav:
            self.assertIn('data-avatar-alt=""', tag)

    def test_someone_elses_hero_is_not_marked(self) -> None:
        other = baker.make(User, username="someone_else")
        other.profile.profile_visibility = "anyone"
        other.profile.save(update_fields=["profile_visibility"])
        html = self.client.get(
            reverse("profile.view_user", kwargs={"profile_slug": other.profile.slug or other.profile.ensure_slug()})
        ).content.decode()
        self.assertIn('id="profile-hero-avatar"', html)
        self.assertNotIn('data-user-avatar="profile-avatar-img"', html)
        # The navbar still shows the viewer, who is the only one it is ever about.
        self.assertEqual(_marked(html), ["nav-avatar-img", "nav-avatar-img"])


class AvatarStateTests(TestCase):
    """GET ``?field=avatar`` answers "has my upload been published yet?" for the page that just saved one."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("profile.field.update")

    def _state(self) -> dict:
        response = self.client.get(self.url, {"field": "avatar"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_no_avatar_and_nothing_held(self) -> None:
        self.assertEqual(self._state(), {"avatar_url": None, "avatar_pending": False})

    def test_a_held_upload_is_pending_and_the_old_picture_is_still_what_shows(self) -> None:
        Profile.objects.filter(user=self.user).update(avatar="avatars/old.png", avatar_upload="unprocessed/abc")
        state = self._state()
        self.assertTrue(state["avatar_pending"])
        self.assertTrue(state["avatar_url"].endswith("/avatars/old.png"))

    def test_a_published_upload_is_the_avatar_and_no_longer_pending(self) -> None:
        Profile.objects.filter(user=self.user).update(avatar="avatars/new.png", avatar_upload="")
        state = self._state()
        self.assertFalse(state["avatar_pending"])
        self.assertTrue(state["avatar_url"].endswith("/avatars/new.png"))

    def test_it_is_about_the_requester_only(self) -> None:
        Profile.objects.filter(user=baker.make(User)).update(
            avatar="avatars/theirs.png", avatar_upload="unprocessed/theirs"
        )
        self.assertEqual(self._state(), {"avatar_url": None, "avatar_pending": False})

    def test_it_needs_a_login(self) -> None:
        response = Client().get(self.url, {"field": "avatar"})
        self.assertEqual(response.status_code, 302)

    def test_other_fields_still_are_not_readable(self) -> None:
        self.assertEqual(self.client.get(self.url, {"field": "bio"}).status_code, 400)


class EmojiAvatarSvgTests(SimpleTestCase):
    """An emoji the colour of its circle still shows: the generated avatar carries a faint shadow under the glyph."""

    def test_the_emoji_is_drawn_through_a_drop_shadow_filter(self) -> None:
        svg = ElementTree.fromstring(AvatarService.generate_emoji_svg("\U0001f426", "#f44336"))
        ns = {"svg": "http://www.w3.org/2000/svg"}
        filters = svg.findall(".//svg:filter", ns)
        self.assertEqual([f.get("id") for f in filters], ["glyph-shadow"])
        self.assertEqual(len(filters[0].findall("svg:feDropShadow", ns)), 1)
        text = svg.find("svg:text", ns)
        assert text is not None
        self.assertEqual(text.get("filter"), "url(#glyph-shadow)")
        self.assertEqual(text.text, "\U0001f426")

    def test_the_circle_itself_is_not_shadowed(self) -> None:
        svg = ElementTree.fromstring(AvatarService.generate_emoji_svg("\U0001f426", "#f44336"))
        circle = svg.find("{http://www.w3.org/2000/svg}circle")
        assert circle is not None
        self.assertIsNone(circle.get("filter"))
        self.assertEqual(circle.get("fill"), "#f44336")


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
