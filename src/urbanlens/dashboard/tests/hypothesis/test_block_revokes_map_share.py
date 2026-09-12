"""Blocking must end a standalone map share, which is live access, not a copy."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.markup.share import MarkupMapShare
from urbanlens.dashboard.services.social.friendship import block_profile


def _profile():
    return baker.make(User).profile


class BlockRevokesMapShareTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.blocker = _profile()
        self.blocked = _profile()

    def _share(self, sender, recipient) -> MarkupMapShare:
        markup_map = baker.make(MarkupMap, profile=sender)
        return MarkupMapShare.objects.create(markup_map=markup_map, from_profile=sender, to_profile=recipient)

    def test_a_map_shared_with_the_blocked_profile_is_revoked(self) -> None:
        share = self._share(self.blocker, self.blocked)

        block_profile(self.blocker, self.blocked)

        self.assertFalse(
            MarkupMapShare.objects.filter(pk=share.pk).exists(),
            "a blocked profile keeps live access to the blocker's map",
        )

    def test_a_map_the_blocked_profile_shared_is_revoked_too(self) -> None:
        """Mutual disengagement, matching how blocking already treats safety partners."""
        share = self._share(self.blocked, self.blocker)

        block_profile(self.blocker, self.blocked)

        self.assertFalse(
            MarkupMapShare.objects.filter(pk=share.pk).exists(),
            "the blocker kept watching a map belonging to someone they blocked",
        )

    def test_an_unrelated_share_is_untouched(self) -> None:
        """The revocation must be scoped to the two profiles involved."""
        bystander = _profile()
        share = self._share(self.blocker, bystander)

        block_profile(self.blocker, self.blocked)

        self.assertTrue(
            MarkupMapShare.objects.filter(pk=share.pk).exists(), "blocking one profile revoked an unrelated share"
        )

    def test_an_unrelated_share_involving_the_blocked_profile_is_untouched(self) -> None:
        """Scoping must hold from the blocked profile's side too, not just the blocker's.

        A filter written as "wipe every share the blocked profile is party to" (ignoring who the blocker is)
        would pass every other test in this file, since none of them give the blocked profile a share with
        anyone but the blocker - this pins that third-party case down."""
        bystander = _profile()
        share = self._share(bystander, self.blocked)

        block_profile(self.blocker, self.blocked)

        self.assertTrue(
            MarkupMapShare.objects.filter(pk=share.pk).exists(),
            "blocking revoked a share between the blocked profile and someone else",
        )

    def test_all_shares_between_the_pair_are_revoked_not_just_one(self) -> None:
        """The delete must be a bulk queryset delete, not "find one match and stop".

        Both directions exist simultaneously here so a `.first()`-then-delete regression - which every
        single-share test above would still pass - gets caught: one of the two rows would survive."""
        outbound = self._share(self.blocker, self.blocked)
        inbound = self._share(self.blocked, self.blocker)

        block_profile(self.blocker, self.blocked)

        self.assertFalse(
            MarkupMapShare.objects.filter(pk__in=[outbound.pk, inbound.pk]).exists(),
            "blocking left at least one of several shares between the pair in place",
        )

    def test_the_map_itself_is_not_deleted(self) -> None:
        """Only the grant goes - the owner's own map is not collateral."""
        share = self._share(self.blocker, self.blocked)
        map_pk = share.markup_map_id

        block_profile(self.blocker, self.blocked)

        self.assertTrue(
            MarkupMap.objects.filter(pk=map_pk).exists(), "blocking deleted the blocker's own map, not just the share"
        )
