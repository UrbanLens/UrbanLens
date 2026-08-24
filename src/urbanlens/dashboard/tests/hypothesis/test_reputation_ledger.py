"""The reputation ledger: recording, scoring, decay, caps and retraction.

The ledger is the only part of this system with no source of truth outside
itself - every achievement metric is a count over other tables and can be
rebuilt at any time, and this cannot. That is why rows are written
synchronously and only their *value* is deferred, and it is why most of what
these tests pin is about rows surviving: idempotency under retry, null-vs-zero,
and retraction being reversible.

The scoring tests exist mainly to hold the traps that were found by reading the
models these rules touch - a materialised external photo is attributed to
whoever voted for it, and one Suggest-Edits submit spanning six fields writes a
single WikiEdit row.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.reputation.model import ProfileReputation, ReputationEvent
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.reputation import coefficients
from urbanlens.dashboard.services.reputation.builtin_rules import register_builtin_rules
from urbanlens.dashboard.services.reputation.scoring import (
    record_event,
    recompute_total,
    restore_event,
    retract_event,
    score_event,
)


class LedgerWriteTests(TestCase):
    """Recording a contribution."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        self.profile = baker.make(User).profile
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location, officially_created=True)

    def _photo(self, **kwargs) -> Image:
        """An ordinary user upload attached to the fixture wiki."""
        defaults = {
            "profile": self.profile,
            "wiki": self.wiki,
            "source": ImageSource.UPLOAD,
            "media_type": MediaKind.PHOTO,
            "image": "pin_images/x.png",
        }
        return baker.make(Image, **{**defaults, **kwargs})

    def test_a_recorded_row_starts_unscored_rather_than_worth_zero(self) -> None:
        """Null and zero are different states, and summing must not confuse them."""
        event = record_event(self.profile, "photo_upload", target=self._photo(), wiki=self.wiki)

        self.assertIsNotNone(event)
        self.assertIsNone(event.value)
        self.assertIn(event, ReputationEvent.objects.unscored())
        self.assertNotIn(event, ReputationEvent.objects.counting())

    def test_recording_the_same_contribution_twice_writes_one_row(self) -> None:
        """Celery runs with acks_late and retries, so every writer can run twice."""
        photo = self._photo()

        first = record_event(self.profile, "photo_upload", target=photo, wiki=self.wiki)
        second = record_event(self.profile, "photo_upload", target=photo, wiki=self.wiki)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(ReputationEvent.objects.for_profile(self.profile).count(), 1)

    def test_a_first_ever_contributor_is_marked_for_the_sweep(self) -> None:
        """The staleness flag has to survive there being no totals row yet.

        Marking was a filter().update(), which silently matches nothing for a
        profile that has never earned anything - so the very first event from a
        brand-new account would never have been picked up by the sweep that
        looks for stale rows. That account is precisely the one this system
        exists to measure.
        """
        self.assertFalse(ProfileReputation.objects.for_profile(self.profile).exists())

        record_event(self.profile, "photo_upload", target=self._photo(), wiki=self.wiki)

        record = ProfileReputation.objects.get(profile=self.profile)
        self.assertTrue(record.is_stale)
        self.assertIn(record, ProfileReputation.objects.stale())

    def test_an_unregistered_rule_is_ignored_rather_than_raising(self) -> None:
        """Bookkeeping must never be able to fail the contribution it describes."""
        self.assertIsNone(record_event(self.profile, "no_such_rule", target=self._photo()))


class ScoringTests(TestCase):
    """Turning a recorded row into a value."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        self.profile = baker.make(User).profile
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)

    def _photo(self, **kwargs) -> Image:
        defaults = {
            "profile": self.profile,
            "wiki": self.wiki,
            "source": ImageSource.UPLOAD,
            "media_type": MediaKind.PHOTO,
            "image": "pin_images/x.png",
            "taken_at": None,
            "latitude": None,
            "longitude": None,
        }
        return baker.make(Image, **{**defaults, **kwargs})

    def _record_and_score(self, photo: Image) -> Decimal | None:
        event = record_event(self.profile, "photo_upload", target=photo, wiki=self.wiki)
        return score_event(event)

    def test_the_first_photo_on_a_wiki_beats_the_second(self) -> None:
        """Value follows how badly the target needed it, not the action type."""
        first = self._record_and_score(self._photo())
        second = self._record_and_score(self._photo())

        self.assertIsNotNone(first)
        self.assertGreater(first, second)

    def test_a_materialised_external_photo_earns_its_voter_nothing(self) -> None:
        """`Image.profile` is the voter on a materialised row, not the photographer.

        Scoring one would pay a user for somebody else's work, and a bulk
        import would pay the importer for hundreds of them.
        """
        external = self._photo(source=ImageSource.WIKIMEDIA)

        event = record_event(self.profile, "photo_upload", target=external, wiki=self.wiki)
        value = score_event(event)

        self.assertIsNone(value)
        event.refresh_from_db()
        self.assertTrue(event.retracted)

    def test_metadata_is_a_bonus_and_its_absence_is_never_a_penalty(self) -> None:
        """EXIF extraction is skipped when the uploader has track_pin_visits off.

        A penalty for missing metadata would quietly pay those users less for
        having a privacy setting enabled, so the bonus has to be additive.

        Two profiles on two wikis, because need and decay are both stateful:
        each photo has to be the first of its kind for its wiki *and* the first
        of its rule for its uploader, or the comparison measures those instead
        of the metadata.
        """
        other_profile = baker.make(User).profile
        other_wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)

        bare_photo = self._photo()
        rich_photo = baker.make(
            Image,
            profile=other_profile,
            wiki=other_wiki,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/y.png",
            taken_at=timezone.now(),
            latitude=Decimal("41.5"),
            longitude=Decimal("-73.9"),
        )

        bare = score_event(record_event(self.profile, "photo_upload", target=bare_photo, wiki=self.wiki))
        rich = score_event(record_event(other_profile, "photo_upload", target=rich_photo, wiki=other_wiki))

        self.assertGreater(bare, 0, "a photo with no metadata must still be worth something")
        expected_bonus = coefficients.QUALITY_HAS_CAPTURE_DATE + coefficients.QUALITY_HAS_REAL_GPS
        self.assertEqual(rich - bare, expected_bonus)

    def test_a_wiki_edit_is_scored_by_fields_changed_not_rows_written(self) -> None:
        """One Suggest-Edits submit spanning six fields writes a single WikiEdit."""
        one = baker.make(WikiEdit, wiki=self.wiki, editor=self.profile, changes={"description": ["a", "b"]}, reverted=False)
        three = baker.make(
            WikiEdit,
            wiki=self.wiki,
            editor=self.profile,
            changes={"description": ["a", "b"], "name": ["c", "d"], "year_built": ["1900", "1901"]},
            reverted=False,
        )

        one_value = score_event(record_event(self.profile, "wiki_field_edit", target=one, wiki=self.wiki))
        three_value = score_event(record_event(self.profile, "wiki_field_edit", target=three, wiki=self.wiki))

        self.assertIsNotNone(one_value)
        self.assertGreater(three_value, one_value)

    def test_repeating_one_activity_in_a_period_decays(self) -> None:
        """A full point, then half, then a quarter - varied use beats bursts."""
        values = [self._record_and_score(self._photo()) for _ in range(3)]

        self.assertTrue(all(v is not None for v in values))
        self.assertGreater(values[0], values[1])
        self.assertGreater(values[1], values[2])

    def test_one_wiki_cannot_be_farmed_past_its_period_cap(self) -> None:
        """Bounds how much one profile can extract from one target."""
        for _ in range(40):
            self._record_and_score(self._photo())

        earned = ReputationEvent.objects.for_profile(self.profile).for_wiki(self.wiki).total_value()

        self.assertLessEqual(earned, coefficients.PER_WIKI_PERIOD_CAP)


class RetractionTests(TestCase):
    """Withdrawing and restoring a contribution's value."""

    def setUp(self) -> None:
        super().setUp()
        register_builtin_rules()
        self.profile = baker.make(User).profile
        self.wiki = baker.make(Wiki, location=baker.make(Location), officially_created=True)
        photo = baker.make(
            Image,
            profile=self.profile,
            wiki=self.wiki,
            source=ImageSource.UPLOAD,
            media_type=MediaKind.PHOTO,
            image="pin_images/x.png",
        )
        self.event = record_event(self.profile, "photo_upload", target=photo, wiki=self.wiki)
        score_event(self.event)

    def test_retraction_is_reversible(self) -> None:
        """A revert-of-a-revert clears WikiEdit.reverted, so this must undo too."""
        self.assertTrue(retract_event(self.event, reason="reverted"))
        self.assertNotIn(self.event, ReputationEvent.objects.counting())

        self.assertTrue(restore_event(self.event))
        self.assertIn(self.event, ReputationEvent.objects.counting())

    def test_retracting_twice_changes_nothing_the_second_time(self) -> None:
        """The write is a compare-and-swap, so a repeat is a no-op."""
        self.assertTrue(retract_event(self.event, reason="reverted"))
        self.assertFalse(retract_event(self.event, reason="reverted"))

    def test_retraction_lowers_the_total_but_never_lifetime_earned(self) -> None:
        """Anything granting durable standing reads lifetime_earned.

        Otherwise reverting somebody's contributions becomes a way to take away
        wiki access they already had - the score as a weapon.
        """
        recompute_total(self.profile)
        before = ProfileReputation.objects.get(profile=self.profile)
        self.assertGreater(before.total, 0)

        retract_event(self.event, reason="reverted")
        recompute_total(self.profile)
        after = ProfileReputation.objects.get(profile=self.profile)

        self.assertEqual(after.total, Decimal("0"))
        self.assertEqual(after.lifetime_earned, before.lifetime_earned)
        self.assertGreater(after.lifetime_earned, 0)
