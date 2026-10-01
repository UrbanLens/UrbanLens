"""A search row matched through several of its aliases, notes, labels or members is one result, found without DISTINCT.

Joining a pin to its aliases, labels, notes and wiki aliases at once and de-duplicating afterwards cost Postgres 73 ms
to plan for an account with ten pins and 134 ms for one with 17,720 - longer than running it. Each to-many match is a
semi-join instead, so the statement's shape no longer depends on how many relations a provider searches.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import Model
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.keyword import ImageKeyword
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search import providers
from urbanlens.dashboard.services.global_search.parser import parse_query

TERM = "quokka"


class SearchMatchesWithoutDistinctTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.profile = baker.make(User).profile
        self.made = 0

    def _unique(self, stem: str) -> str:
        self.made += 1
        return f"{TERM}{stem}{self.made}"

    def _pin(self) -> Pin:
        self.made += 1
        location = Location.objects.create(latitude=10 + self.made * 0.01, longitude=20 + self.made * 0.01)
        return baker.make(Pin, profile=self.profile, location=location, name=f"Spot {self.made}", description="")

    def _two_labels(self, target: Pin | Image) -> None:
        for _ in range(2):
            target.labels.add(Label.objects.create(name=self._unique("label"), kind=KIND_TAG))

    def assertEachOnceWithoutDistinct(
        self,
        provider: providers.SearchProvider,
        *models: type[Model],
        text: str = TERM,
        expected: int = 2,
        limit: int = 2,
    ) -> None:
        with CaptureQueriesContext(connection) as captured:
            results = provider.search(self.profile, parse_query(text), limit)
        identities = [(result.url, result.object_uuid) for result in results]
        self.assertEqual(len(identities), expected, identities)
        self.assertEqual(len(set(identities)), len(identities), "one row came back once per related row it matched")
        prefixes = tuple(f'SELECT DISTINCT "{model._meta.db_table}".' for model in models)
        self.assertEqual([query["sql"] for query in captured.captured_queries if query["sql"].startswith(prefixes)], [])

    def test_pins_matched_through_aliases_labels_notes_and_wiki_aliases(self) -> None:
        for _ in range(2):
            pin = self._pin()
            wiki = baker.make(Wiki, location=pin.location, name=f"Place {self.made}", description="")
            for _ in range(2):
                baker.make(PinAlias, pin=pin, name=self._unique("alias"))
                baker.make(PinNote, pin=pin, text=f"{TERM} note")
                baker.make(WikiAlias, wiki=wiki, name=self._unique("wikialias"))
            self._two_labels(pin)

        self.assertEachOnceWithoutDistinct(providers.PinSearchProvider(), Pin)

    def test_pins_carrying_several_of_the_asked_labels(self) -> None:
        rooftop = Label.objects.create(name="rooftop", kind=KIND_TAG)
        basement = Label.objects.create(name="basement", kind=KIND_TAG)
        for _ in range(2):
            self._pin().labels.add(rooftop, basement)

        self.assertEachOnceWithoutDistinct(providers.PinSearchProvider(), Pin, text="label:rooftop label:basement")

    def test_pins_that_have_several_labels(self) -> None:
        for _ in range(2):
            self._two_labels(self._pin())

        self.assertEachOnceWithoutDistinct(providers.PinSearchProvider(), Pin, text="has:labels")

    def test_visited_pins_that_have_several_labels(self) -> None:
        for _ in range(2):
            pin = self._pin()
            Pin.objects.filter(pk=pin.pk).update(last_visited=timezone.now())
            self._two_labels(pin)
        self._pin()

        self.assertEachOnceWithoutDistinct(providers.PinSearchProvider(), Pin, text="is:visited", limit=3)

    def test_photos_matched_through_keywords_and_labels(self) -> None:
        for _ in range(2):
            self.made += 1
            image = Image.objects.create(
                image=SimpleUploadedFile(f"p{self.made}.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
                profile=self.profile,
                caption=f"Photo {self.made}",
            )
            for _ in range(2):
                baker.make(ImageKeyword, image=image, keyword=self._unique("keyword"))
            self._two_labels(image)

        self.assertEachOnceWithoutDistinct(providers.PhotoSearchProvider(), Image)

    def test_wikis_matched_through_aliases(self) -> None:
        for _ in range(2):
            wiki = baker.make(Wiki, location=self._pin().location, name=f"Place {self.made}", description="")
            for _ in range(2):
                baker.make(WikiAlias, wiki=wiki, name=self._unique("alias"))

        self.assertEachOnceWithoutDistinct(providers.WikiSearchProvider(), Wiki)

    def test_articles_matched_through_their_pins_aliases(self) -> None:
        for _ in range(2):
            pin = self._pin()
            for _ in range(2):
                baker.make(PinAlias, pin=pin, name=self._unique("alias"))
            Article.objects.create(pin=pin, content="Nothing to see.")

        self.assertEachOnceWithoutDistinct(providers.ArticleSearchProvider(), Article)

    def test_trips_with_several_members_matched_through_activities_and_comments(self) -> None:
        for _ in range(2):
            self.made += 1
            trip = baker.make(Trip, creator=self.profile, name=f"Outing {self.made}", description="")
            for _ in range(2):
                baker.make(TripMembership, trip=trip, profile=baker.make(User).profile)
                baker.make(TripActivity, trip=trip, title=f"{TERM} stop", notes="")
                baker.make(TripComment, trip=trip, text=f"{TERM} remark")

        self.assertEachOnceWithoutDistinct(providers.TripSearchProvider(), Trip)

    def test_safety_checkins_matched_through_messages(self) -> None:
        for _ in range(2):
            self.made += 1
            checkin = baker.make(SafetyCheckin, profile=self.profile, title=f"Plan {self.made}", plan_details="")
            for _ in range(2):
                baker.make(SafetyCheckinMessage, checkin=checkin, body=f"{TERM} update")

        self.assertEachOnceWithoutDistinct(providers.SafetySearchProvider(), SafetyCheckin)

    def test_visits(self) -> None:
        for _ in range(2):
            baker.make(PinVisit, pin=self._pin(), notes=f"{TERM} visit")

        self.assertEachOnceWithoutDistinct(providers.VisitSearchProvider(), PinVisit)

    def test_comments_and_trip_comments(self) -> None:
        for _ in range(2):
            baker.make(Comment, profile=self.profile, pin=self._pin(), text=f"{TERM} comment")
        trip = baker.make(Trip, creator=self.profile)
        baker.make(TripMembership, trip=trip, profile=self.profile)
        baker.make(TripComment, trip=trip, author=self.profile, text=f"{TERM} trip comment")

        self.assertEachOnceWithoutDistinct(
            providers.CommentSearchProvider(), Comment, TripComment, expected=3, limit=10
        )
