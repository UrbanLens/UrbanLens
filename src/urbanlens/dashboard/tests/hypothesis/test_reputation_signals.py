"""Contributions reach the ledger by themselves, and reverts take them back.

The write half of these handlers is deliberately synchronous, so these tests
assert that a row exists immediately after the contributing save - not after a
Celery round trip. Scoring is the deferred half and is queued through
``transaction.on_commit``, which does not run inside a TestCase's transaction;
that is why the rows here are expected to be *unscored* rather than valued.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reputation.meta import TargetKind
from urbanlens.dashboard.models.reputation.model import ReputationEvent
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.reputation.builtin_rules import register_builtin_rules


class ContributionSignalTests(TestCase):
    """A contribution writes its own ledger row."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        self.profile = baker.make(User).profile
        self.wiki = baker.make(Wiki, location=baker.make(Location))

    def _rows(self, rule_key: str) -> list[ReputationEvent]:
        return list(ReputationEvent.objects.for_profile(self.profile).for_rule(rule_key))

    def test_a_wiki_photo_upload_records_itself(self) -> None:
        """No caller has to remember to write the row."""
        baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )

        rows = self._rows("photo_upload")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].value, "scoring is the deferred half")
        self.assertEqual(rows[0].wiki_id, self.wiki.pk)

    def test_a_materialised_external_photo_records_nothing(self) -> None:
        """`Image.profile` is the up-voter on those rows, not the photographer.

        Writing a row here would credit somebody for another person's work, and
        a bulk import would credit the importer hundreds of times over.
        """
        baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            source=ImageSource.WIKIMEDIA,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )

        self.assertEqual(self._rows("photo_upload"), [])

    def test_saving_the_same_photo_again_does_not_double_record(self) -> None:
        """The subscription is not created_only, so ordinary re-saves re-fire."""
        image = baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )
        image.caption = "changed"
        image.save()

        self.assertEqual(len(self._rows("photo_upload")), 1)

    def test_a_root_pin_earns_and_a_child_pin_does_not(self) -> None:
        """Child pins are structure within one place, not separate discoveries."""
        root = baker.make(Pin, profile=self.profile, parent_pin=None)
        baker.make(Pin, profile=self.profile, parent_pin=root)

        rows = self._rows("pin_created")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].target_id, root.pk)

    def test_a_wiki_comment_records_itself(self) -> None:
        """Comments on a pin are private notes; comments on a wiki are given."""
        baker.make("dashboard.Comment", profile=self.profile, wiki=self.wiki, pin=None)

        self.assertEqual(len(self._rows("wiki_comment")), 1)


class RevertSignalTests(TestCase):
    """A reverted edit stops counting, and un-reverting starts it again."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        self.profile = baker.make(User).profile
        self.wiki = baker.make(Wiki, location=baker.make(Location))
        self.edit = baker.make(
            WikiEdit,
            wiki=self.wiki,
            editor=self.profile,
            changes={"description": ["before", "after"]},
            reverted=False,
        )

    def _event(self) -> ReputationEvent:
        return ReputationEvent.objects.get(
            rule_key="wiki_field_edit", target_kind=TargetKind.WIKI_EDIT, target_id=self.edit.pk
        )

    def test_reverting_the_edit_retracts_its_row(self) -> None:
        """A contribution somebody undid should not go on paying."""
        self.assertFalse(self._event().retracted)

        self.edit.reverted = True
        self.edit.reverted_at = timezone.now()
        self.edit.save()

        self.assertTrue(self._event().retracted)

    def test_reverting_the_revert_restores_it(self) -> None:
        """`WikiEdit.reverted` is current state - a revert-of-a-revert clears it.

        So retraction has to be re-applicable in both directions rather than a
        one-way subtraction.
        """
        self.edit.reverted = True
        self.edit.save()
        self.assertTrue(self._event().retracted)

        self.edit.reverted = False
        self.edit.save()

        self.assertFalse(self._event().retracted)
