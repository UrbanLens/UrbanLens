"""Tests for Consensus photo eligibility/capture (services.consensus.fields' PHOTO_COORDINATES strategy, services.consensus.photos).

An Image only ever becomes a ``PHOTO_COORDINATES`` round candidate through
``wiki.images`` - i.e. only once explicitly attached to that specific wiki
(``Image.wiki`` set) - never merely because a player who can see the wiki
also owns some other private photo. This is the same privacy invariant
SpotGuessr's own photo selection enforces (a prior bug, fixed in commit
``afc7ee8b``, leaked private pin photos into other players' game sessions
when this gate was missing).
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.consensus.model import (
    ConsensusFieldKind,
    ConsensusRound,
    ConsensusSession,
    ConsensusSessionStatus,
)
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus.fields import get_strategy
from urbanlens.dashboard.services.consensus.photos import record_in_round_upload


def _make_profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class PhotoCoordinatesEligibilityTests(TestCase):
    def setUp(self) -> None:
        self.strategy = get_strategy(ConsensusFieldKind.PHOTO_COORDINATES)
        self.wiki = baker.make(Wiki, location=baker.make(Location))

    def test_an_unattached_private_image_is_never_a_candidate(self) -> None:
        profile = _make_profile()
        baker.make(Image, wiki=None, profile=profile, latitude=None, longitude=None)
        self.assertNotIn(self.wiki, self.strategy.find_missing([self.wiki]))

    def test_an_image_attached_to_a_different_wiki_is_never_a_candidate_here(self) -> None:
        other_wiki = baker.make(Wiki, location=baker.make(Location))
        baker.make(Image, wiki=other_wiki, latitude=None, longitude=None)
        self.assertNotIn(self.wiki, self.strategy.find_missing([self.wiki]))

    def test_an_image_attached_to_this_wiki_with_no_coordinates_is_a_candidate(self) -> None:
        baker.make(Image, wiki=self.wiki, latitude=None, longitude=None)
        self.assertIn(self.wiki, self.strategy.find_missing([self.wiki]))

    def test_an_image_that_already_has_coordinates_is_not_a_missing_candidate(self) -> None:
        baker.make(Image, wiki=self.wiki, latitude="42.650000", longitude="-73.760000")
        self.assertNotIn(self.wiki, self.strategy.find_missing([self.wiki]))

    def test_an_image_that_already_has_coordinates_is_a_known_candidate_for_check_rounds(self) -> None:
        baker.make(Image, wiki=self.wiki, latitude="42.650000", longitude="-73.760000")
        self.assertIn(self.wiki, self.strategy.find_known([self.wiki]))


class RecordInRoundUploadTests(TestCase):
    def test_upload_attaches_the_photo_to_the_rounds_wiki_when_previously_unattached(self) -> None:
        profile = _make_profile()
        wiki = baker.make(Wiki, location=baker.make(Location))
        session = ConsensusSession.objects.create(host_profile=profile, status=ConsensusSessionStatus.ACTIVE)
        round_ = ConsensusRound.objects.create(
            session=session, sequence_index=0, wiki=wiki, field_kind=ConsensusFieldKind.PHOTO_COORDINATES
        )
        image = baker.make(Image, wiki=None, profile=profile)

        record_in_round_upload(round_, image, profile)

        image.refresh_from_db()
        self.assertEqual(image.wiki_id, wiki.pk)

    def test_upload_never_reassigns_a_photo_already_attached_elsewhere(self) -> None:
        profile = _make_profile()
        original_wiki = baker.make(Wiki, location=baker.make(Location))
        round_wiki = baker.make(Wiki, location=baker.make(Location))
        session = ConsensusSession.objects.create(host_profile=profile, status=ConsensusSessionStatus.ACTIVE)
        round_ = ConsensusRound.objects.create(
            session=session, sequence_index=0, wiki=round_wiki, field_kind=ConsensusFieldKind.PHOTO_COORDINATES
        )
        image = baker.make(Image, wiki=original_wiki, profile=profile)

        record_in_round_upload(round_, image, profile)

        image.refresh_from_db()
        self.assertEqual(image.wiki_id, original_wiki.pk)
