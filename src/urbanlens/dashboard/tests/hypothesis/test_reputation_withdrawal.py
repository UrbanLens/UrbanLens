"""A contribution that no longer exists must stop counting toward reputation.

P55 records one half of this: withdrawing a photo from a wiki takes the quota
bonus back but leaves the scored `ReputationEvent` standing. Writing it down
turned up two more ways the same thing happens, because `ReputationEvent.target_id`
is a plain `IntegerField` rather than a foreign key and the ledger subscribes to
`post_save` only - there is no `post_delete` handler anywhere:

1. the contributor withdraws the photo from its wiki (P55's item);
2. the contributor deletes the whole wiki, which the quota bonus already handles
   via `revoke_community_bonuses_on_wiki_delete`;
3. the photo row is deleted outright - nothing observes that at all.

(1) is what this file covers. (3) is real - reproduced while writing these - but
it is **not** fixed here and is filed as P86, because `post_delete` cannot tell a
contributor withdrawing their own photo from a moderator removing it, and the
one-way rule says those must not end the same way. That is a product question,
not a refactor.

`score_event`'s `target_deleted` retraction covers none of them: it fires only
when the target is already gone by the time the deferred scoring task runs, which
is a race window, not a lifecycle hook.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reputation.model import ReputationEvent
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.reputation.builtin_rules import register_builtin_rules


class WithdrawnContributionTests(TestCase):
    """Withdrawing a contribution must retract the points it earned."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def _contribute(self) -> Image:
        """A wiki photo whose ledger row has been scored, as it would be in production."""
        image = baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            pin=self.pin,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )
        # Scoring is deferred through on_commit, which does not run inside a
        # TestCase transaction - so stand in for it, since what is under test is
        # what happens to an *already scored* row.
        ReputationEvent.objects.filter(target_id=image.pk, rule_key="photo_upload").update(value=Decimal(5))
        return image

    def _event(self, image: Image) -> ReputationEvent | None:
        return ReputationEvent.objects.filter(rule_key="photo_upload", target_id=image.pk).first()

    def test_the_contribution_starts_out_counting(self) -> None:
        """Anti-vacuity: the fixture has to produce a row that actually counts."""
        image = self._contribute()
        event = self._event(image)

        self.assertIsNotNone(event, "the upload must have written a ledger row")
        self.assertFalse(event.retracted)
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(5))

    def test_withdrawing_the_photo_retracts_its_points(self) -> None:
        """P55's item: the quota bonus is taken back here and the points were not."""
        from urbanlens.dashboard.services.media.images import detach_image_from_wiki

        image = self._contribute()

        detach_image_from_wiki(image, withdrawn_by_contributor=True)

        event = self._event(image)
        self.assertIsNotNone(event, "retraction, not deletion - the ledger keeps its history")
        self.assertTrue(event.retracted, "a withdrawn contribution must stop counting")
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(0))

    def test_someone_elses_removal_does_not_retract(self) -> None:
        """The one-way rule: only the contributor ending their own contribution counts.

        The same rule the quota bonus follows - votes withdrawn, a moderator
        removing the photo, or the low-engagement sweep must all leave the
        contributor's standing alone.
        """
        from urbanlens.dashboard.services.media.images import detach_image_from_wiki

        image = self._contribute()

        detach_image_from_wiki(image, withdrawn_by_contributor=False)

        event = self._event(image)
        self.assertFalse(event.retracted, "somebody else's action must not cost the contributor points")
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(5))

    def test_lifetime_earned_is_not_reduced_by_a_retraction(self) -> None:
        """Retraction lowers standing, never history.

        `recompute_total`'s docstring is explicit that `lifetime_earned` ignores
        retraction, so that reverting somebody's contributions cannot take away
        access they already had. Whatever retracts must not undo that.
        """
        from urbanlens.dashboard.services.media.images import detach_image_from_wiki
        from urbanlens.dashboard.services.reputation.scoring import recompute_total

        image = self._contribute()
        recompute_total(self.profile)
        detach_image_from_wiki(image, withdrawn_by_contributor=True)
        recompute_total(self.profile)

        self.profile.refresh_from_db()
        record = self.profile.reputation
        self.assertEqual(record.total, Decimal(0), "standing reflects the retraction")
        self.assertEqual(record.lifetime_earned, Decimal(5), "history does not")
