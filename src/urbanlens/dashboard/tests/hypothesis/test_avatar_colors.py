"""Tests for the distinct-per-list fallback avatar color system.

Covers the pure assignment algorithm (services.avatar_colors) and its two
wired-in render sites: the group members dialog and the group member search
results (both listing several people's fallback avatars together, where a
shared default color made them indistinguishable).
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.avatar_colors import PALETTE_SIZE, assign_avatar_colors
from urbanlens.dashboard.services.group_chats import create_group_chat


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


class _Item:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class AssignAvatarColorsTests(SimpleTestCase):
    """Pure algorithm: deterministic per-identity, collision-free within one call."""

    def test_same_identity_always_gets_the_same_color(self) -> None:
        a = _Item("same-slug")
        assign_avatar_colors([a], identity=lambda i: i.identity)
        b = _Item("same-slug")
        assign_avatar_colors([b], identity=lambda i: i.identity)
        self.assertEqual(a.avatar_color_class, b.avatar_color_class)

    def test_all_classes_valid_palette_indices(self) -> None:
        items = [_Item(f"person-{n}") for n in range(PALETTE_SIZE)]
        assign_avatar_colors(items, identity=lambda i: i.identity)
        valid = {f"avatar-color-{n}" for n in range(PALETTE_SIZE)}
        for item in items:
            self.assertIn(item.avatar_color_class, valid)

    def test_no_two_items_in_the_same_list_collide_up_to_palette_size(self) -> None:
        items = [_Item(f"person-{n}") for n in range(PALETTE_SIZE)]
        assign_avatar_colors(items, identity=lambda i: i.identity)
        classes = [item.avatar_color_class for item in items]
        self.assertEqual(len(classes), len(set(classes)))

    def test_custom_attr_name(self) -> None:
        item = _Item("x")
        assign_avatar_colors([item], identity=lambda i: i.identity, attr="my_color")
        self.assertTrue(hasattr(item, "my_color"))
        self.assertFalse(hasattr(item, "avatar_color_class"))

    def test_empty_list_is_a_no_op(self) -> None:
        assign_avatar_colors([], identity=lambda i: i.identity)  # must not raise


class GroupMembersDialogAvatarColorTests(TestCase):
    """GET messages.group.members assigns distinct avatar_color_class per member."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.members = [_profile() for _ in range(4)]
        self.group = create_group_chat(self.creator, "Crew", self.members)
        self.client.force_login(self.creator.user)

    def test_members_get_distinct_colors(self) -> None:
        response = self.client.get(reverse("messages.group.members", args=[self.group.uuid]))
        self.assertEqual(response.status_code, 200)
        colors = [m.profile.avatar_color_class for m in response.context["memberships"]]
        self.assertEqual(len(colors), 5)  # creator + 4 members
        self.assertEqual(len(colors), len(set(colors)))
        for color in colors:
            self.assertRegex(color, r"^avatar-color-\d+$")

    def test_avatar_color_class_rendered_in_html(self) -> None:
        response = self.client.get(reverse("messages.group.members", args=[self.group.uuid]))
        content = response.content.decode()
        self.assertIn("avatar-color-", content)


class GroupMemberSearchAvatarColorTests(TestCase):
    """GET messages.group.member_search assigns distinct avatar_color_class per result."""

    def setUp(self) -> None:
        super().setUp()
        self.searcher = _profile()
        self.client.force_login(self.searcher.user)
        self.candidates = []
        for n in range(4):
            candidate = _profile()  # already ANYONE-visible, so can_direct_message needs no friendship
            candidate.user.username = f"searchable-user-{n}"
            candidate.user.save(update_fields=["username"])
            self.candidates.append(candidate)

    def test_results_get_distinct_colors(self) -> None:
        response = self.client.get(reverse("messages.group.member_search"), {"q": "searchable-user"})
        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertEqual(len(results), 4)
        colors = [candidate.avatar_color_class for candidate in results]
        self.assertEqual(len(colors), len(set(colors)))
