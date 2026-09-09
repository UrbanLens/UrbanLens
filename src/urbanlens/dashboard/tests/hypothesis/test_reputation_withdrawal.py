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
from django.urls import reverse
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


class RemovalWeightTests(TestCase):
    """A weight reduces standing without erasing the contribution (D9).

    The middle ground `retract_event` cannot express: it is a boolean, so it
    takes the whole value. D9 asked for an ending somebody else caused to cost
    "only very slightly", reversibly and re-weightably - which is a multiplier
    applied where the total is summed, not a re-score.
    """

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        self.image = baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            pin=self.pin,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )
        ReputationEvent.objects.filter(target_id=self.image.pk, rule_key="photo_upload").update(value=Decimal(10))

    def _total(self) -> Decimal:
        return ReputationEvent.objects.for_profile(self.profile).total_value()

    def test_a_weight_scales_the_total_without_erasing_the_row(self) -> None:
        from urbanlens.dashboard.services.reputation.scoring import MODERATED_REMOVAL_WEIGHT, weight_events_for_target

        self.assertEqual(self._total(), Decimal(10))

        weight_events_for_target(self.image, weight=MODERATED_REMOVAL_WEIGHT, reason="removed_by_other")

        self.assertEqual(self._total(), Decimal(10) * MODERATED_REMOVAL_WEIGHT)
        event = ReputationEvent.objects.get(target_id=self.image.pk, rule_key="photo_upload")
        self.assertFalse(event.retracted, "a weighted row still counts - it is not a retraction")
        self.assertEqual(event.value, Decimal(10), "and its original score is untouched")

    def test_a_weight_is_reversible(self) -> None:
        """Setting it back to 1 restores the full value, with no re-scoring."""
        from urbanlens.dashboard.services.reputation.scoring import MODERATED_REMOVAL_WEIGHT, weight_events_for_target

        weight_events_for_target(self.image, weight=MODERATED_REMOVAL_WEIGHT, reason="removed_by_other")
        self.assertNotEqual(self._total(), Decimal(10))

        weight_events_for_target(self.image, weight=Decimal(1), reason="restored")

        self.assertEqual(self._total(), Decimal(10))

    def test_a_weight_does_not_reduce_lifetime_earned(self) -> None:
        """Standing moves; access already earned does not.

        `recompute_total` ignores retraction for `lifetime_earned` so reverting
        contributions cannot take away access someone already had. A weight has
        to be excluded for the same reason.
        """
        from urbanlens.dashboard.services.reputation.scoring import (
            MODERATED_REMOVAL_WEIGHT,
            recompute_total,
            weight_events_for_target,
        )

        recompute_total(self.profile)
        weight_events_for_target(self.image, weight=MODERATED_REMOVAL_WEIGHT, reason="removed_by_other")
        recompute_total(self.profile)

        record = self.profile.reputation
        record.refresh_from_db()
        self.assertEqual(record.total, Decimal(10) * MODERATED_REMOVAL_WEIGHT, "standing reflects the weight")
        self.assertEqual(record.lifetime_earned, Decimal(10), "history does not")

    def test_an_unweighted_row_counts_in_full(self) -> None:
        """Anti-vacuity: weight defaults to 1, so nothing changes for ordinary rows."""
        self.assertEqual(self._total(), Decimal(10))
        self.assertEqual(
            ReputationEvent.objects.get(target_id=self.image.pk, rule_key="photo_upload").weight,
            Decimal(1),
        )


class CascadeKeepsBenefitsTests(TestCase):
    """A cascade must not strip a contributor who did nothing.

    Jess, 2026-09-07: "I'd rather err on the side of keeping positive benefits
    awarded to users who contributed rather than stripping them." Deleting a
    detail pin deletes a child `Wiki`, and `Comment.wiki` is CASCADE - so
    somebody else's comments go with it, through no act of theirs. This is the
    test that stops a future blanket `post_delete` quietly reversing that ruling.
    """

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        baker.make(User)
        self.contributor = baker.make(User).profile
        self.location = baker.make(Location)
        self.parent_wiki = baker.make(Wiki, location=self.location)
        self.child_wiki = baker.make(Wiki, location=baker.make(Location), parent_wiki=self.parent_wiki)

    def test_a_cascaded_comment_keeps_its_points(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        comment = baker.make(Comment, wiki=self.child_wiki, profile=self.contributor, text="Useful note")
        ReputationEvent.objects.filter(target_id=comment.pk, rule_key="wiki_comment").update(value=Decimal(4))
        self.assertEqual(ReputationEvent.objects.for_profile(self.contributor).total_value(), Decimal(4))

        # What deleting a detail pin does: the child wiki goes, and the comment
        # with it, without the contributor doing anything.
        self.child_wiki.delete()

        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists(), "precondition: the cascade really happened")
        event = ReputationEvent.objects.filter(rule_key="wiki_comment", target_id=comment.pk).first()
        self.assertIsNotNone(event)
        self.assertFalse(event.retracted, "a cascade is nobody's withdrawal")
        self.assertEqual(event.weight, Decimal(1), "and nobody's moderation either")
        self.assertEqual(ReputationEvent.objects.for_profile(self.contributor).total_value(), Decimal(4))


class WikiCommentDeleteRetractionTests(TestCase):
    """Deleting your own wiki comment retracts its points, like withdrawing a photo.

    `WikiCommentDeleteView` is author-only - it 403s when `comment.profile` is
    not the caller - so every deletion through it is the contributor ending
    their own contribution. That is the same act as
    `detach_image_from_wiki(..., withdrawn_by_contributor=True)`, and until
    2026-09-07 the two ended differently: the photo retracted and the comment
    kept its points.
    """

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        # A pin on the same location is what makes the wiki visible to this user.
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def _comment(self):
        from urbanlens.dashboard.models.comments.model import Comment

        comment = baker.make(Comment, wiki=self.wiki, profile=self.profile, text="My note")
        ReputationEvent.objects.filter(target_id=comment.pk, rule_key="wiki_comment").update(value=Decimal(4))
        return comment

    def test_deleting_your_own_comment_retracts_its_points(self) -> None:
        comment = self._comment()
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(4))

        response = self.client.delete(reverse("location.wiki.comment.delete", args=[self.location.slug, comment.pk]))

        self.assertEqual(response.status_code, 200)
        event = ReputationEvent.objects.get(rule_key="wiki_comment", target_id=comment.pk)
        self.assertTrue(event.retracted, "ending your own contribution stops it counting")
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(0))

    def test_an_undeleted_comment_still_counts(self) -> None:
        """Anti-vacuity: the retraction must come from the delete, not the fixture."""
        self._comment()

        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).total_value(), Decimal(4))

    def test_somebody_elses_comment_cannot_be_deleted_here_at_all(self) -> None:
        """Why this view retracts outright rather than weighting.

        There is no moderator path through it, so every deletion it performs is
        a withdrawal and D9's weighted case has no site here to hook.

        Asserted on the outcome rather than the status: the refusal is a **404**,
        not the 403 the author check would give, because
        `_wiki_comment_addressable_by` refuses first and deliberately does not
        distinguish "not yours" from "no such comment" - its docstring calls the
        alternative an existence oracle. Which of the two gates fired is not the
        point; that nothing was deleted and no points moved is.
        """
        from urbanlens.dashboard.models.comments.model import Comment

        other = baker.make(User).profile
        theirs = baker.make(Comment, wiki=self.wiki, profile=other, text="Their note")
        ReputationEvent.objects.filter(target_id=theirs.pk, rule_key="wiki_comment").update(value=Decimal(4))

        response = self.client.delete(reverse("location.wiki.comment.delete", args=[self.location.slug, theirs.pk]))

        self.assertNotEqual(response.status_code, 200, f"a non-author must not delete it: got {response.status_code}")
        self.assertTrue(Comment.objects.filter(pk=theirs.pk).exists())
        event = ReputationEvent.objects.get(rule_key="wiki_comment", target_id=theirs.pk)
        self.assertFalse(event.retracted, "and their points stand, unretracted")
        self.assertEqual(event.weight, Decimal(1), "and unweighted")


class WikiEditRevertReputationTests(TestCase):
    """Reverting a wiki edit is not author-only, unlike deleting a comment.

    `LocationWikiRevertView` shows its Revert button to any viewer with wiki
    access (the sibling Expunge button beside it is the author-only one), so
    a non-author reverting somebody else's edit is a real, live path - not a
    hypothetical D9 was written to cover in advance. Before this test existed,
    `on_wiki_edit_reverted` retracted in full regardless of who reverted,
    which let any wiki-access viewer erase another editor's standing outright
    - exactly the removal-costs-only-slightly case `MODERATED_REMOVAL_WEIGHT`
    exists for, left unwired at this, its first real call site.
    """

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        baker.make(User)
        self.editor = baker.make(User).profile
        self.reverter = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)

    def _edit(self):
        from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit

        edit = apply_wiki_edit(self.wiki, self.editor, {"name": "New Name"})
        assert edit is not None
        ReputationEvent.objects.filter(target_id=edit.pk, rule_key="wiki_field_edit").update(value=Decimal(3))
        return edit

    def _event(self, edit) -> ReputationEvent:
        return ReputationEvent.objects.get(rule_key="wiki_field_edit", target_id=edit.pk)

    def test_reverting_your_own_edit_retracts_in_full(self) -> None:
        """Self-reverting is a withdrawal, same as any other - it retracts."""
        from urbanlens.dashboard.services.wiki.wiki_edits import revert_wiki_edit

        edit = self._edit()
        self.assertEqual(ReputationEvent.objects.for_profile(self.editor).total_value(), Decimal(3))

        revert_wiki_edit(self.location, self.wiki, self.editor, edit)

        event = self._event(edit)
        self.assertTrue(event.retracted, "the editor undid their own contribution")
        self.assertEqual(event.weight, Decimal(1), "a withdrawal is not a moderated removal")
        self.assertEqual(ReputationEvent.objects.for_profile(self.editor).total_value(), Decimal(0))

    def test_someone_else_reverting_weights_instead_of_retracting(self) -> None:
        """The bug this test pins: a non-author revert must not erase standing outright."""
        from urbanlens.dashboard.services.reputation.scoring import MODERATED_REMOVAL_WEIGHT
        from urbanlens.dashboard.services.wiki.wiki_edits import revert_wiki_edit

        edit = self._edit()

        revert_wiki_edit(self.location, self.wiki, self.reverter, edit)

        event = self._event(edit)
        self.assertFalse(event.retracted, "a removal the editor did not choose must not retract outright")
        self.assertEqual(event.weight, MODERATED_REMOVAL_WEIGHT)
        self.assertEqual(
            ReputationEvent.objects.for_profile(self.editor).total_value(),
            Decimal(3) * MODERATED_REMOVAL_WEIGHT,
        )

    def test_reverting_the_revert_restores_full_weight(self) -> None:
        """Un-reverting undoes whichever adjustment fired, self or other."""
        from urbanlens.dashboard.services.wiki.wiki_edits import revert_wiki_edit

        edit = self._edit()
        revert_edit, skipped = revert_wiki_edit(self.location, self.wiki, self.reverter, edit)
        assert revert_edit is not None and not skipped

        revert_wiki_edit(self.location, self.wiki, self.editor, revert_edit)

        event = self._event(edit)
        self.assertFalse(event.retracted)
        self.assertEqual(event.weight, Decimal(1))
        self.assertEqual(ReputationEvent.objects.for_profile(self.editor).total_value(), Decimal(3))
