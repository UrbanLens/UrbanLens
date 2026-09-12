"""Revision history builds every revision, then paginates the list it built.

N21 H46, and a second door the finding did not name. Both the pin-article and
the wiki-article revision endpoints do `list(article.revisions...)` and build a
row dict per revision *before* handing the result to `paginated_response` - so
the page-size limit bounds the response body and nothing else. Each row carries
`size_delta`, which is `len(self.content)`, so every revision's complete source
(200,000 characters is the field's own ceiling) is read out of the database and
held in memory to produce one integer per row; and `masked_editor_name` calls
`resolve_visible_identities` - a helper written to take a whole batch - once per
revision with a single-item list.

`paginated_response`'s own docstring already describes this anti-pattern and
offers `row_builder` as the fix: "Passing a queryset plus this is strictly
better than pre-building a list of dicts and paginating that."

Stated as "more revisions must not cost more work for one page", measured in
model instantiations. A query count would read this as flat - it is one query
either way - and a byte budget would need recalibrating whenever a row grows a
field. Instantiations move with exactly the thing that is wrong here.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.instantiation_scaling import count_instantiations
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.wiki.articles import save_article
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import disable_throttling

BASE = "/dashboard/api/external/v1"

#: Long enough that loading every body is visibly wrong, short enough to seed fast.
_BODY = "x" * 2_000


class _RevisionCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        key, self.raw_key = generate_api_key(self.user, "Article client")
        # The wiki door needs its own scope; the pin door must not be reachable
        # with it, which test_external_api_pin_article already covers.
        ApiKey.objects.filter(pk=key.pk).update(scopes=list(ApiKeyScope.values))

        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.location = baker.make(Location, latitude=40.0015, longitude=-105.0015)
        self.wiki = baker.make(Wiki, location=self.location, name="Old Mill")
        # The viewer needs standing at the location for the wiki to resolve.
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.location_slug = self.location.ensure_slug()
        disable_throttling(self)

    @property
    def _headers(self) -> dict:
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_key}"}

    def _seed_pin_revisions(self, count: int) -> None:
        for index in range(count):
            save_article(editor=self.profile, content=f"{_BODY}{index}", pin=self.pin)

    def _seed_wiki_revisions(self, count: int) -> None:
        for index in range(count):
            save_article(editor=self.profile, content=f"{_BODY}{index}", wiki=self.wiki)

    def _objects_for(self, url: str) -> int:
        with count_instantiations() as counted:
            response = self.client.get(url, **self._headers)
        assert response.status_code == 200, (response.status_code, response.content[:300])  # nosec B101
        return counted.total


class OnePageDoesNotCostTheWholeHistoryTests(_RevisionCase):
    """The axis is revisions behind the page, not rows on it."""

    def test_the_pin_endpoint_does_not_build_every_revision(self) -> None:
        self._seed_pin_revisions(4)
        small = self._objects_for(f"{BASE}/pins/{self.pin.slug}/article/revisions/?page_size=2")
        self._seed_pin_revisions(16)
        large = self._objects_for(f"{BASE}/pins/{self.pin.slug}/article/revisions/?page_size=2")

        self.assertLessEqual(
            large - small,
            4,
            f"sixteen more revisions behind one page cost {large - small} more model objects",
        )

    def test_the_wiki_endpoint_has_the_same_problem(self) -> None:
        """The door the finding did not name; capping one would move the problem."""
        self._seed_wiki_revisions(4)
        small = self._objects_for(f"{BASE}/wikis/{self.location_slug}/article/revisions/?page_size=2")
        self._seed_wiki_revisions(16)
        large = self._objects_for(f"{BASE}/wikis/{self.location_slug}/article/revisions/?page_size=2")

        self.assertLessEqual(
            large - small,
            4,
            f"sixteen more wiki revisions behind one page cost {large - small} more model objects",
        )


class TheHistoryIsStillRightTests(_RevisionCase):
    """A cheaper page that reports the wrong deltas is not a fix."""

    def _rows(self, url: str) -> list[dict]:
        response = self.client.get(url, **self._headers)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()["results"]

    def test_size_delta_is_measured_against_the_true_neighbour(self) -> None:
        """Not the neighbour on the page: page 2's first row must not read as +N from nothing."""
        for length in (100, 150, 220, 400):
            save_article(editor=self.profile, content="y" * length, pin=self.pin)

        page_two = self._rows(f"{BASE}/pins/{self.pin.slug}/article/revisions/?page_size=2&page=2")

        # Newest-first: page two holds the 150 and the 100, oldest last.
        self.assertEqual([row["size_delta"] for row in page_two], [50, 100])

    def test_the_first_revision_reads_as_its_whole_length(self) -> None:
        save_article(editor=self.profile, content="z" * 77, pin=self.pin)

        rows = self._rows(f"{BASE}/pins/{self.pin.slug}/article/revisions/")

        self.assertEqual(rows[-1]["size_delta"], 77)

    def test_every_revision_is_still_reachable_by_paging(self) -> None:
        self._seed_pin_revisions(5)

        seen = []
        for page in (1, 2, 3):
            seen.extend(
                row["id"]
                for row in self._rows(f"{BASE}/pins/{self.pin.slug}/article/revisions/?page_size=2&page={page}")
            )

        self.assertEqual(sorted(seen), sorted(ArticleRevision.objects.values_list("pk", flat=True)))

    def test_the_editor_name_is_still_resolved(self) -> None:
        save_article(editor=self.profile, content="w" * 40, pin=self.pin)

        rows = self._rows(f"{BASE}/pins/{self.pin.slug}/article/revisions/")

        self.assertEqual(rows[0]["editor"], str(self.profile))
