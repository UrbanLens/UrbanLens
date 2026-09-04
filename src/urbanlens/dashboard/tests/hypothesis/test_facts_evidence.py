"""Integration tests for the Facts evidence write path and its call-site hooks.

Every test patches ``tasks.recompute_fact_confidence.delay`` so evidence
creation never touches a real Celery broker - confidence recomputation
itself is exercised directly via ``services.facts.confidence.recompute``
(see ``RecomputeIntegrationTests``), the same "call the task function
directly instead of via .delay()" pattern used elsewhere in this test suite.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusAnswer,
    ConsensusFieldKind,
    ConsensusProfile,
    ConsensusRound,
    ConsensusSession,
)
from urbanlens.dashboard.models.facts.model import Fact, FactEvidence, FactSourceKind, FactStatus, FactSubjectType
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.facts import confidence, evidence


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class FactSaveInvariantTests(TestCase):
    def test_zero_subjects_raises(self) -> None:
        with self.assertRaises(ValueError):
            Fact.objects.create(key="wiki_name", data_type="text")

    def test_two_subjects_raises(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        location = baker.make(Location)
        with self.assertRaises(ValueError):
            Fact.objects.create(key="wiki_name", data_type="text", wiki=wiki, location=location)

    def test_subject_type_is_derived_from_the_set_fk(self) -> None:
        wiki = baker.make(Wiki, location=baker.make(Location))
        fact = Fact.objects.create(key="wiki_name", data_type="text", wiki=wiki)
        self.assertEqual(fact.subject_type, FactSubjectType.WIKI)


class RecordEvidenceTests(TestCase):
    def setUp(self) -> None:
        # record_evidence enqueues through safely_enqueue_task (broker-outage
        # tolerant) rather than calling .delay directly - patch the seam it
        # actually uses.
        self.enqueue_mock = self.enterContext(
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task")
        )
        self.wiki = baker.make(Wiki, location=baker.make(Location))

    def test_creates_the_fact_and_evidence_on_first_call(self) -> None:
        result = evidence.record_evidence(
            key="wiki_name", value="Old Mill", source_kind=FactSourceKind.WIKI_EDIT, wiki=self.wiki
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.get_value(), "Old Mill")
        fact = Fact.objects.get(wiki=self.wiki, key="wiki_name")
        self.assertEqual(FactEvidence.objects.filter(fact=fact).count(), 1)

    def test_reuses_the_existing_fact_on_later_evidence(self) -> None:
        evidence.record_evidence(
            key="wiki_name", value="Old Mill", source_kind=FactSourceKind.WIKI_EDIT, wiki=self.wiki
        )
        evidence.record_evidence(
            key="wiki_name", value="Old Mill Sanatorium", source_kind=FactSourceKind.WIKI_EDIT, wiki=self.wiki
        )
        self.assertEqual(Fact.objects.filter(wiki=self.wiki, key="wiki_name").count(), 1)
        self.assertEqual(FactEvidence.objects.filter(fact__wiki=self.wiki, fact__key="wiki_name").count(), 2)

    def test_an_unregistered_key_creates_nothing(self) -> None:
        result = evidence.record_evidence(
            key="not_a_real_key", value="x", source_kind=FactSourceKind.WIKI_EDIT, wiki=self.wiki
        )
        self.assertIsNone(result)
        self.assertEqual(Fact.objects.count(), 0)

    def test_queues_a_confidence_recompute(self) -> None:
        from urbanlens.dashboard.tasks import recompute_fact_confidence

        evidence.record_evidence(
            key="wiki_name", value="Old Mill", source_kind=FactSourceKind.WIKI_EDIT, wiki=self.wiki
        )
        fact = Fact.objects.get(wiki=self.wiki, key="wiki_name")
        self.enqueue_mock.assert_called_once_with(recompute_fact_confidence, fact.pk)


class RecordPhotoCoordinateEvidenceTests(TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))

    def test_logs_an_anonymous_point_observation(self) -> None:
        image = baker.make(Image)
        point = Point(-73.76, 42.65, srid=4326)

        result = evidence.record_photo_coordinate_evidence(image.pk, point)

        self.assertIsNotNone(result)
        self.assertEqual(result.source_kind, FactSourceKind.PLAYER_ANONYMOUS)
        self.assertIsNone(result.submitter_id)
        fact = Fact.objects.get(image=image, key="photo_coordinates")
        self.assertEqual(fact.subject_type, FactSubjectType.IMAGE)

    def test_a_missing_image_is_a_silent_no_op(self) -> None:
        result = evidence.record_photo_coordinate_evidence(999_999, Point(0, 0, srid=4326))
        self.assertIsNone(result)


class RecordConsensusAnswerEvidenceTests(TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        self.profile = _make_profile()
        self.wiki = baker.make(Wiki, location=baker.make(Location))
        self.session = ConsensusSession.objects.create(host_profile=self.profile)

    def _make_round(self, field_kind: str, **kwargs) -> ConsensusRound:
        return ConsensusRound.objects.create(
            session=self.session, sequence_index=0, wiki=self.wiki, field_kind=field_kind, **kwargs
        )

    def test_logs_a_text_answer_trust_weighted_by_the_submitters_consensus_profile(self) -> None:
        round_ = self._make_round(ConsensusFieldKind.WIKI_NAME)
        answer = ConsensusAnswer.objects.create(round=round_, profile=self.profile, text_value="Old Mill")

        result = evidence.record_consensus_answer_evidence(round_, answer)

        self.assertIsNotNone(result)
        self.assertEqual(result.get_value(), "Old Mill")
        self.assertEqual(result.source_kind, FactSourceKind.PLAYER_ATTRIBUTED)
        self.assertEqual(result.submitter_id, self.profile.pk)
        # Fresh ConsensusProfile: trust_alpha=trust_beta=2.0, so trust_score=0.5.
        self.assertAlmostEqual(result.submitter_trust_snapshot, 0.5)
        self.assertEqual(result.consensus_round_id, round_.pk)
        fact = Fact.objects.get(wiki=self.wiki, key="wiki_name")
        self.assertEqual(fact.subject_type, FactSubjectType.WIKI)

    def test_wiki_alias_rounds_are_excluded_from_facts(self) -> None:
        round_ = self._make_round(ConsensusFieldKind.WIKI_ALIAS)
        answer = ConsensusAnswer.objects.create(round=round_, profile=self.profile, text_value="The Mill")

        result = evidence.record_consensus_answer_evidence(round_, answer)

        self.assertIsNone(result)
        self.assertEqual(Fact.objects.count(), 0)

    def test_photo_coordinates_without_a_target_image_is_a_no_op(self) -> None:
        round_ = self._make_round(ConsensusFieldKind.PHOTO_COORDINATES)
        answer = ConsensusAnswer.objects.create(
            round=round_, profile=self.profile, guess_point=Point(-73.76, 42.65, srid=4326)
        )

        result = evidence.record_consensus_answer_evidence(round_, answer)

        self.assertIsNone(result)

    def test_photo_coordinates_with_a_target_image_logs_against_the_image(self) -> None:
        image = baker.make(Image, wiki=self.wiki)
        round_ = self._make_round(ConsensusFieldKind.PHOTO_COORDINATES, target_image=image)
        answer = ConsensusAnswer.objects.create(
            round=round_, profile=self.profile, guess_point=Point(-73.76, 42.65, srid=4326)
        )

        result = evidence.record_consensus_answer_evidence(round_, answer)

        self.assertIsNotNone(result)
        fact = Fact.objects.get(image=image, key="photo_coordinates")
        self.assertEqual(fact.subject_type, FactSubjectType.IMAGE)


class RecordWikiEditEvidenceTests(TestCase):
    """Exercises the models.wiki_edit.signals post_save hook, not just the function in isolation."""

    def setUp(self) -> None:
        self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        self.profile = _make_profile()
        self.wiki = baker.make(Wiki, location=baker.make(Location))

    def test_a_manual_edit_logs_evidence_for_mapped_fields(self) -> None:
        WikiEdit.objects.create(
            wiki=self.wiki, editor=self.profile, changes={"name": {"from": "Old", "to": "New Name"}}
        )

        fact = Fact.objects.get(wiki=self.wiki, key="wiki_name")
        row = FactEvidence.objects.get(fact=fact)
        self.assertEqual(row.get_value(), "New Name")
        self.assertEqual(row.source_kind, FactSourceKind.WIKI_EDIT)
        self.assertEqual(row.submitter_id, self.profile.pk)

    def test_unmapped_fields_are_skipped(self) -> None:
        WikiEdit.objects.create(
            wiki=self.wiki, editor=self.profile, changes={"bounding_box": {"from": None, "to": "POLYGON(...)"}}
        )
        self.assertEqual(Fact.objects.count(), 0)

    def test_consensus_sourced_edits_are_not_double_logged(self) -> None:
        """services.consensus.session._finish_round already logs this answer directly - the signal must skip it."""
        session = ConsensusSession.objects.create(host_profile=self.profile)
        round_ = ConsensusRound.objects.create(
            session=session, sequence_index=0, wiki=self.wiki, field_kind=ConsensusFieldKind.WIKI_NAME
        )

        WikiEdit.objects.create(
            wiki=self.wiki,
            editor=self.profile,
            changes={"name": {"from": "Old", "to": "New Name"}},
            consensus_round=round_,
        )

        self.assertEqual(Fact.objects.count(), 0)

    def test_edits_with_no_editor_are_skipped(self) -> None:
        WikiEdit.objects.create(wiki=self.wiki, editor=None, changes={"name": {"from": "Old", "to": "New Name"}})
        self.assertEqual(Fact.objects.count(), 0)


class RecomputeIntegrationTests(TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))
        self.wiki = baker.make(Wiki, location=baker.make(Location))

    def _log(
        self, value: str, *, count: int, source_kind: str = FactSourceKind.ADMIN, source_name: str = "admin"
    ) -> Fact:
        for _ in range(count):
            evidence.record_evidence(
                key="wiki_indoor_outdoor", value=value, source_kind=source_kind, source_name=source_name, wiki=self.wiki
            )
        return Fact.objects.get(wiki=self.wiki, key="wiki_indoor_outdoor")

    def test_below_minimum_evidence_leaves_the_fact_unresolved(self) -> None:
        fact = self._log("inside", count=2)

        confidence.recompute(fact.pk)

        fact.refresh_from_db()
        self.assertEqual(fact.status, FactStatus.UNCONFIRMED)
        self.assertIsNone(fact.get_value())
        self.assertEqual(fact.evidence_count, 2)

    def test_strong_agreement_confirms_the_fact(self) -> None:
        fact = self._log("inside", count=5)

        confidence.recompute(fact.pk)

        fact.refresh_from_db()
        self.assertEqual(fact.status, FactStatus.CONFIRMED)
        self.assertEqual(fact.get_value(), "inside")
        self.assertGreaterEqual(fact.confidence, confidence.CONFIRM_THRESHOLD)

    def test_an_even_split_is_contested(self) -> None:
        self._log("inside", count=3)
        fact = self._log("outside", count=3)

        confidence.recompute(fact.pk)

        fact.refresh_from_db()
        self.assertEqual(fact.status, FactStatus.CONTESTED)

    def test_a_confirmed_value_does_not_flip_on_a_single_weak_challenger(self) -> None:
        fact = self._log("inside", count=5)
        confidence.recompute(fact.pk)
        fact.refresh_from_db()
        self.assertEqual(fact.status, FactStatus.CONFIRMED)

        evidence.record_evidence(
            key="wiki_indoor_outdoor",
            value="outside",
            source_kind=FactSourceKind.PLAYER_ANONYMOUS,
            source_name="spotguessr_photo_guess",
            wiki=self.wiki,
        )
        confidence.recompute(fact.pk)

        fact.refresh_from_db()
        self.assertEqual(fact.get_value(), "inside")

    def test_a_missing_fact_is_a_silent_no_op(self) -> None:
        confidence.recompute(999_999)
