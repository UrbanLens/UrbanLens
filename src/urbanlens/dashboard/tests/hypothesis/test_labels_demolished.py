"""Tests for services.labels - auto-applying the protected "Demolished" status.

Mirrors services.visits.add_visited_status's contract for pins, plus the
wiki-side counterpart that uses one canonical global label instead of a
per-profile one (a Wiki has no owning profile - see services.labels'
module docstring).
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.labels.statuses import add_demolished_status, add_demolished_status_to_wiki


def _make_profile():
    from urbanlens.dashboard.models.profile.model import Profile

    user = baker.make("auth.User")
    return Profile.objects.get(user=user)


class AddDemolishedStatusTests(TestCase):
    """add_demolished_status - the per-profile pin label, mirroring add_visited_status."""

    def test_adds_the_profiles_own_demolished_label(self) -> None:
        profile = _make_profile()
        pin = baker.make(Pin, profile=profile, location=baker.make("dashboard.Location"))
        demolished = Label.objects.get(profile=profile, kind="status", name="Demolished")

        add_demolished_status(pin)

        self.assertIn(demolished, pin.labels.all())

    def test_is_idempotent(self) -> None:
        profile = _make_profile()
        pin = baker.make(Pin, profile=profile, location=baker.make("dashboard.Location"))

        add_demolished_status(pin)
        add_demolished_status(pin)

        demolished = Label.objects.get(profile=profile, kind="status", name="Demolished")
        self.assertEqual(pin.labels.filter(pk=demolished.pk).count(), 1)

    def test_noop_when_profile_has_no_demolished_label(self) -> None:
        profile = _make_profile()
        Label.objects.filter(profile=profile, kind="status", name="Demolished").delete()
        pin = baker.make(Pin, profile=profile, location=baker.make("dashboard.Location"))

        add_demolished_status(pin)  # must not raise

        self.assertEqual(pin.labels.count(), 0)


class AddDemolishedStatusToWikiTests(TestCase):
    """add_demolished_status_to_wiki - the one canonical global label."""

    def setUp(self) -> None:
        super().setUp()
        # Deterministic regardless of whether migration 0087 already seeded
        # the global row in this test database.
        Label.objects.filter(profile=None, kind="status", name="Demolished").delete()
        self.global_demolished = baker.make(Label, profile=None, kind="status", name="Demolished", is_protected=True)

    def test_adds_the_global_demolished_label(self) -> None:
        wiki = baker.make(Wiki, location=baker.make("dashboard.Location"))

        add_demolished_status_to_wiki(wiki)

        self.assertIn(self.global_demolished, wiki.labels.all())

    def test_is_idempotent(self) -> None:
        wiki = baker.make(Wiki, location=baker.make("dashboard.Location"))

        add_demolished_status_to_wiki(wiki)
        add_demolished_status_to_wiki(wiki)

        self.assertEqual(wiki.labels.filter(pk=self.global_demolished.pk).count(), 1)

    def test_does_not_use_a_profiles_private_demolished_label(self) -> None:
        profile = _make_profile()
        wiki = baker.make(Wiki, location=baker.make("dashboard.Location"))

        add_demolished_status_to_wiki(wiki)

        private_demolished = Label.objects.get(profile=profile, kind="status", name="Demolished")
        self.assertNotIn(private_demolished, wiki.labels.all())
        self.assertIn(self.global_demolished, wiki.labels.all())
