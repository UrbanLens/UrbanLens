"""One account's own data must not grow the SQL text of the queries it runs."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.relevance import MediaRelevance
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import MapPinPayloadService

#: Far apart enough that any per-vote term in the statement is unmissable.
FEW_VOTES = 3
MANY_VOTES = 300


class TheStatementDoesNotGrowWithTheAccountTests(TestCase):
    """The map's SQL is the same text whatever the profile has voted on."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make(Location, latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)

    def _vote_irrelevant(self, count: int) -> None:
        """Mark *count* distinct gallery items as not relevant.

        Args:
            count: How many marks to create.
        """
        existing = MediaRelevance.objects.filter(profile=self.profile).count()
        MediaRelevance.objects.bulk_create(
            [
                MediaRelevance(
                    profile=self.profile,
                    location=self.location,
                    source="wikimedia",
                    item_key=f"{existing + index:040d}",
                    is_relevant=False,
                )
                for index in range(count)
            ],
        )

    def _payload_sql(self) -> str:
        """The statement the map sends to build one page of payloads.

        Returns:
            The longest statement the page issued, which is the payload query.
        """
        query = Pin.objects.filter(profile=self.profile).root_pins()
        with CaptureQueriesContext(connection) as captured:
            MapPinPayloadService(self.profile).page(query, limit=100)
        return max((entry["sql"] for entry in captured), key=len)

    def test_the_statement_is_the_same_length_at_any_vote_count(self) -> None:
        self._vote_irrelevant(FEW_VOTES)
        small = self._payload_sql()

        self._vote_irrelevant(MANY_VOTES - FEW_VOTES)
        large = self._payload_sql()

        self.assertEqual(
            len(large),
            len(small),
            f"the map's statement grew by {len(large) - len(small)} bytes when the profile cast "
            f"{MANY_VOTES - FEW_VOTES} more votes, so its size is set by the user's own history",
        )

    def test_no_vote_key_appears_in_the_statement(self) -> None:
        """The sharper form: a key in the text is a key that was inlined."""
        self._vote_irrelevant(FEW_VOTES)
        key = MediaRelevance.objects.filter(profile=self.profile).values_list("item_key", flat=True).first()

        self.assertNotIn(str(key), self._payload_sql())

    def test_the_votes_still_decide_the_fallback_photo(self) -> None:
        """Guards the above: a filter that stopped filtering would pass both."""
        from urbanlens.dashboard.models.images.model import Image, MediaKind
        from urbanlens.dashboard.models.images.relevance import media_item_key

        url = "https://example.invalid/rejected.jpg"
        baker.make(
            Image,
            pin=self.pin,
            profile=self.profile,
            media_type=MediaKind.PHOTO,
            thumbnail="thumbs/rejected.jpg",
            media_item_key=media_item_key(url),
        )
        query = Pin.objects.filter(profile=self.profile).root_pins()

        before = MapPinPayloadService(self.profile).page(query, limit=100).pins[0]["cover_photo_url"]
        self.assertTrue(before, "the seeded photo was not being used as a fallback, so this proves nothing")

        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.location,
            source="wikimedia",
            item_key=media_item_key(url),
            is_relevant=False,
        )

        after = MapPinPayloadService(self.profile).page(query, limit=100).pins[0]["cover_photo_url"]
        self.assertIsNone(after, "a photo the profile marked irrelevant is still being used as its cover")
