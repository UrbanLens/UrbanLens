"""SearchForm must never let one profile's private badges leak into another's tags/exclude_tags choices."""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.profile.model import Profile


class SearchFormBadgePrivacyTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        other_user = baker.make("auth.User")
        self.other_profile = Profile.objects.get(user=other_user)

        self.own_badge = baker.make(Badge, profile=self.profile, kind="tag", name="Mine")
        self.other_badge = baker.make(Badge, profile=self.other_profile, kind="tag", name="Theirs")
        self.global_badge = baker.make(Badge, profile=None, kind="tag", name="Everyone's")

    def test_tags_queryset_excludes_other_profiles_badges(self) -> None:
        form = SearchForm({}, profile=self.profile)
        choices = set(form.fields["tags"].queryset)
        self.assertIn(self.own_badge, choices)
        self.assertIn(self.global_badge, choices)
        self.assertNotIn(self.other_badge, choices)

    def test_exclude_tags_queryset_excludes_other_profiles_badges(self) -> None:
        form = SearchForm({}, profile=self.profile)
        choices = set(form.fields["exclude_tags"].queryset)
        self.assertIn(self.own_badge, choices)
        self.assertIn(self.global_badge, choices)
        self.assertNotIn(self.other_badge, choices)

    def test_no_profile_only_sees_global_badges(self) -> None:
        form = SearchForm({})
        choices = set(form.fields["tags"].queryset)
        self.assertIn(self.global_badge, choices)
        self.assertNotIn(self.own_badge, choices)
        self.assertNotIn(self.other_badge, choices)
