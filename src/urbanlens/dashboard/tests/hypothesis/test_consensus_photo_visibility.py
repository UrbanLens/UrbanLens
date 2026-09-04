"""A Consensus round must not show you a photo you are not allowed to see.

Being eligible for a wiki satisfies only the first of the two visibility gates.
Eligibility means "you have a visited pin at this place", which grants you the
wiki - the container. It says nothing about the second gate: whether the
uploader's ``photo_upload_visibility`` admits *you* to the photos on it.

The photo strategy read ``wiki.images`` with neither gate applied, so a photo
contributed under a FRIENDS-only setting could be put in front of any player who
had been to the place. It reached them as a full-size image to drop a pin on,
which is about as complete an exposure as the app has.

Two bugs sat on top of each other here: ``_photo_build_round`` used
``wiki.images.filter(...)``, which both skipped visibility *and* defeated the
prefetch that ``eligibility`` builds - the exact misuse its comment warns about
("only .all() reads the cache").
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from urbanlens.dashboard.models.consensus.model import ConsensusFieldKind
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.consensus import eligibility, fields


class ConsensusPhotoVisibilityTests(TestCase):
    def setUp(self) -> None:
        self.player_user = baker.make(User)
        self.player = self.player_user.profile
        self.uploader = baker.make(User).profile

        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.wiki = baker.make(Wiki, location=self.location)
        # Eligibility is "a visited pin at the place" - the container gate.
        baker.make(Pin, profile=self.player, location=self.location, parent_pin=None, last_visited=timezone.now())
        # The uploader has one too. Not decoration: wiki access is what let them
        # contribute in the first place, and it is also what gives the pair a
        # common pin, so the player's own viewer_photo_filter (courtesy-only,
        # defaulting to ANYTHING_IN_COMMON) does not quietly do the excluding and
        # make these tests pass for the wrong reason.
        baker.make(Pin, profile=self.uploader, location=self.location, parent_pin=None)

    def _photo(self, uploader_visibility: str) -> Image:
        Profile.objects.filter(pk=self.uploader.pk).update(photo_upload_visibility=uploader_visibility)
        return baker.make(
            Image, profile=self.uploader, wiki=self.wiki, image="pin_images/x.jpg", latitude=None, longitude=None
        )

    def _pool(self) -> list[Wiki]:
        return list(eligibility.eligible_wikis(self.player))

    def _offered_image_ids(self) -> set[int]:
        strategy = fields.get_strategy(ConsensusFieldKind.PHOTO_COORDINATES)
        assert strategy is not None
        ids = set()
        for wiki in strategy.find_missing(self._pool()):
            content = strategy.build_round(wiki)
            if content is not None and content.target_image is not None:
                ids.add(content.target_image.pk)
        return ids

    def test_a_photo_the_uploader_hid_is_never_offered(self) -> None:
        """The player has the wiki. They do not have this photo."""
        hidden = self._photo(VisibilityChoice.FRIENDS)

        self.assertNotIn(
            hidden.pk, self._offered_image_ids(), "a Consensus round offered a photo the uploader's settings exclude"
        )

    def test_a_visible_photo_is_still_offered(self) -> None:
        """Positive control - without it, breaking the feature would pass."""
        visible = self._photo(VisibilityChoice.ANYONE)

        self.assertIn(visible.pk, self._offered_image_ids(), "a photo the player may see stopped being offered")

    def test_the_players_own_photo_is_offered_whatever_they_set(self) -> None:
        Profile.objects.filter(pk=self.player.pk).update(photo_upload_visibility=VisibilityChoice.NO_ONE)
        mine = baker.make(
            Image, profile=self.player, wiki=self.wiki, image="pin_images/mine.jpg", latitude=None, longitude=None
        )

        self.assertIn(mine.pk, self._offered_image_ids())

    def test_a_wiki_whose_only_photo_is_hidden_is_not_a_photo_candidate(self) -> None:
        """find_missing must agree with build_round, or selection loops on a
        wiki it can never build a round for."""
        self._photo(VisibilityChoice.FRIENDS)
        strategy = fields.get_strategy(ConsensusFieldKind.PHOTO_COORDINATES)
        assert strategy is not None

        self.assertEqual(strategy.find_missing(self._pool()), [])
