"""Tests for the external API's global-search surface.

Search is the highest-risk read in this API. Every other endpoint answers "give
me the thing I named"; this one answers "give me everything that matches", which
means a single provider whose access scoping is wrong leaks rows the caller
never had a handle on, and does it inside a 200 that looks perfectly healthy.

Three properties get the most attention here, because each of them fails
*silently* when it fails:

1. **Per-provider scoping.** ``search:read`` is a floor, not a master key. A
   credential that holds it and nothing else must get pins-free, photo-free,
   DM-free results, with the dropped sections named in ``omitted_types``. The
   direct-message case is the sharp one and has its own class: the provider
   returns plaintext excerpts of message bodies, and ``messages:read`` lives in
   ``OAUTH2_ONLY_SCOPES`` so that a leaked PAT can never reach them. A search
   endpoint that ran the default provider chain would walk straight around that.
2. **No ``url`` on the wire.** ``SearchResult.url`` is a web path. Shipping it
   to a JSON client produces links that 404 or return an HTML login page, so the
   response carries ``object_slug``/``object_uuid`` instead, and the assertion
   that ``url`` is absent is made against every result of every type.
3. **A short query is a 200.** A search box fires per keystroke; the first
   character must not be an error.

The engine-level tests at the bottom cover the ``types=``/``limit=`` overrides
this endpoint needed, including the fail-closed reading of an empty restriction -
the one place where "no types" could plausibly have been read as "all types".
"""

from __future__ import annotations

from datetime import timedelta
import os

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from hypothesis import HealthCheck, given, settings as hypothesis_settings, strategies as st
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers_search import parse_result_types
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.global_search.results import RESULT_TYPES

AccessToken = get_access_token_model()

#: A grant covering every domain the search endpoint can reach *except*
#: messages, which no PAT may ever hold (``OAUTH2_ONLY_SCOPES``).
_ALL_PAT_SEARCH_SCOPES = [
    ApiKeyScope.SEARCH_READ.value,
    ApiKeyScope.PINS_READ.value,
    ApiKeyScope.PHOTOS_READ.value,
    ApiKeyScope.WIKI_READ.value,
    ApiKeyScope.TRIPS_READ.value,
    ApiKeyScope.VISITS_READ.value,
    ApiKeyScope.SAFETY_READ.value,
]


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying a credential as a bearer token.

    Args:
        raw_key: The raw API key or OAuth2 access token.

    Returns:
        Extra kwargs for the Django test client.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _SearchApiTestCase(TestCase):
    """Shared fixture: a key owner with broadly-scoped search access, and a bystander."""

    def setUp(self) -> None:
        """Create the key owner, a bystander, and a search-scoped API key."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other_profile = Profile.objects.get(user=self.other_user)
        self.raw_key = self._key_with_scopes(_ALL_PAT_SEARCH_SCOPES)
        # Location carries a unique constraint on (latitude, longitude) - a
        # coordinate pair *is* a place, and two rows for one pair would split the
        # community wiki attached to it. Every baked location therefore needs its
        # own coordinates; this counter supplies them.
        self._location_seq = 0

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value.

        Args:
            scopes: Raw scope strings to write onto the key row.

        Returns:
            The raw bearer key.
        """
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _oauth_token(self, scope: str) -> str:
        """Mint a first-party OAuth2 access token for the key owner.

        Args:
            scope: Space-separated scope string.

        Returns:
            The raw token value.
        """
        token = AccessToken.objects.create(
            user=self.user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=scope,
        )
        return token.token

    def _search(self, raw_key: str | None = None, **params: object):
        """GET the search endpoint with *params* as the query string.

        Args:
            raw_key: Credential to authenticate with; defaults to the fixture's.
            **params: Query parameters (``q``, ``types``, ``limit``).

        Returns:
            The test client response.
        """
        return self.client.get(reverse("external_api:search"), params, **_bearer(raw_key or self.raw_key))

    def _location(self, **kwargs):
        """Bake a Location at coordinates no other fixture row has used.

        Args:
            **kwargs: Overrides passed to the Location bake.

        Returns:
            The created location.
        """
        self._location_seq += 1
        kwargs.setdefault("latitude", f"39.{100 + self._location_seq}")
        kwargs.setdefault("longitude", "-84.51")
        kwargs.setdefault("locality", "Cincinnati")
        return baker.make("dashboard.Location", **kwargs)

    def _make_pin(self, profile: Profile | None = None, **kwargs):
        """Create a pin with a real Location behind it.

        Args:
            profile: Owner; defaults to the fixture's key owner.
            **kwargs: Overrides passed to the Pin bake.

        Returns:
            The created pin.
        """
        location = kwargs.pop("location", None) or self._location()
        return baker.make("dashboard.Pin", profile=profile or self.profile, location=location, **kwargs)

    def _group(self, body: dict, slug: str) -> dict | None:
        """Find one group in a response body by result-type slug.

        Args:
            body: The parsed response JSON.
            slug: A ``RESULT_TYPES`` slug.

        Returns:
            The group dict, or None when the section is absent.
        """
        return next((group for group in body["groups"] if group["type"] == slug), None)


class GlobalSearchEnvelopeTests(_SearchApiTestCase):
    """The response shape, and the query-length contract a search box depends on."""

    def setUp(self) -> None:
        """Add one findable pin to the key owner's map."""
        super().setUp()
        self.pin = self._make_pin(name="Willow Grove Mill", description="Rusty turbines in the basement")

    def test_returns_every_documented_envelope_key(self) -> None:
        """A client can render the whole payload without probing for optional keys."""
        response = self._search(q="willow grove")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("query", "total", "used_fallback", "filter_chips", "errors", "omitted_types", "groups"):
            self.assertIn(key, body)

    def test_finds_the_owners_pin_and_groups_it(self) -> None:
        """A matching pin comes back inside a labelled ``pins`` section."""
        body = self._search(q="willow grove").json()
        group = self._group(body, "pins")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual(group["label"], RESULT_TYPES["pins"].label)
        self.assertIn("Willow Grove Mill", [result["title"] for result in group["results"]])
        self.assertEqual(body["total"], sum(len(section["results"]) for section in body["groups"]))

    def test_another_users_pin_is_never_returned(self) -> None:
        """Access scoping is the provider's job, and it must survive this surface."""
        self._make_pin(profile=self.other_profile, name="Willow Grove Mill")
        body = self._search(q="willow grove").json()
        group = self._group(body, "pins")
        assert group is not None
        # Exactly one hit, not two: the bystander's identically-named pin must
        # not ride along on the key owner's search.
        self.assertEqual([result["title"] for result in group["results"]].count("Willow Grove Mill"), 1)

    def test_single_character_query_is_200_with_no_results(self) -> None:
        """A search box sends every keystroke - the first one is not an error."""
        response = self._search(q="w")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["groups"], [])

    def test_blank_and_absent_queries_are_200_with_no_results(self) -> None:
        """An empty box and no box at all behave identically."""
        for response in (self._search(q=""), self._search()):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], 0)

    def test_whitespace_only_query_is_200_with_no_results(self) -> None:
        """Whitespace is not two characters of query, however long it is."""
        body = self._search(q="     ").json()
        self.assertEqual(body["total"], 0)

    def test_query_is_echoed_back(self) -> None:
        """A client rendering responses out of order can discard stale ones."""
        self.assertEqual(self._search(q="willow grove").json()["query"], "willow grove")

    def test_limit_caps_results_per_section(self) -> None:
        """``limit`` bounds a section, so a focused search can be shallow on purpose."""
        for index in range(4):
            self._make_pin(name=f"Willow Grove {index}")
        group = self._group(self._search(q="willow grove", types="pins", limit=2).json(), "pins")
        assert group is not None
        self.assertEqual(len(group["results"]), 2)

    def test_limit_outside_the_allowed_range_is_a_400_in_the_shared_envelope(self) -> None:
        """A malformed parameter *is* an error - unlike a short query."""
        response = self._search(q="willow", limit=9999)
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "Invalid request.")
        self.assertIn("limit", body["fields"])

    def test_types_restricts_the_sections_searched(self) -> None:
        """Asking for one type returns that type only."""
        baker.make("dashboard.Trip", creator=self.profile, name="Willow Grove weekend")
        body = self._search(q="willow grove", types="trips").json()
        self.assertEqual({group["type"] for group in body["groups"]}, {"trips"})

    def test_unknown_types_are_dropped_not_rejected(self) -> None:
        """A newer client naming a type this build lacks still gets the rest."""
        body = self._search(q="willow grove", types="pins,quantumfoam").json()
        self.assertEqual(self._search(q="willow grove", types="pins,quantumfoam").status_code, 200)
        self.assertEqual({group["type"] for group in body["groups"]}, {"pins"})

    def test_wholly_unknown_types_search_nothing_rather_than_everything(self) -> None:
        """A typo'd filter must not silently widen into an unfiltered search.

        The fail-open shape of this bug is the reason it has a test: reading
        "restricted to nothing recognized" as "no restriction" would answer
        ``?types=pinz`` with results from all ten domains.
        """
        body = self._search(q="willow grove", types="pinz").json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["groups"], [])


class GlobalSearchIdentifierTests(_SearchApiTestCase):
    """Results carry API-usable identifiers, and never the web ``url``."""

    def _all_results(self, body: dict) -> list[dict]:
        """Flatten every result across every group.

        Args:
            body: The parsed response JSON.

        Returns:
            All result dicts in the payload.
        """
        return [result for group in body["groups"] for result in group["results"]]

    def test_no_result_of_any_type_carries_a_url(self) -> None:
        """``SearchResult.url`` is a web path; a JSON client cannot follow it.

        Asserted across every populated section at once rather than per
        provider, so a provider added later is covered without anyone
        remembering to extend this test.
        """
        pin = self._make_pin(name="Anchor Mill")
        baker.make("dashboard.Trip", creator=self.profile, name="Anchor Mill weekend")
        baker.make("dashboard.PinVisit", pin=pin, visited_at=timezone.now(), notes="Anchor Mill service tunnel")
        baker.make(
            "dashboard.Image", profile=self.profile, caption="Anchor Mill turbine hall", image="photos/anchor.jpg"
        )

        results = self._all_results(self._search(q="anchor mill").json())
        self.assertTrue(results, "fixture produced no results to assert against")
        for result in results:
            self.assertNotIn("url", result)
            self.assertIn("object_slug", result)
            self.assertIn("object_uuid", result)

    def test_pin_result_is_addressable_by_slug_and_uuid(self) -> None:
        """Both identifiers are present, and both resolve the same pin."""
        pin = self._make_pin(name="Anchor Mill")
        group = self._group(self._search(q="anchor mill", types="pins").json(), "pins")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], pin.slug)
        self.assertEqual(result["object_uuid"], str(pin.uuid))

    def test_pin_detail_accepts_the_slug_the_search_handed_back(self) -> None:
        """The identifier is not decorative - it addresses the pin detail route.

        This is the assertion that would have caught shipping ``url``: a value
        that merely *looks* like an identifier passes every shape test and then
        404s the first time a client tries to open a result.
        """
        pin = self._make_pin(name="Anchor Mill")
        raw = self._key_with_scopes([*_ALL_PAT_SEARCH_SCOPES])
        group = self._group(self._search(q="anchor mill", types="pins").json(), "pins")
        assert group is not None
        slug = group["results"][0]["object_slug"]
        response = self.client.get(reverse("external_api:pins.detail", args=[slug]), **_bearer(raw))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["uuid"], str(pin.uuid))

    def test_photo_result_is_uuid_addressed_with_no_slug(self) -> None:
        """Photos have no slug anywhere; the uuid is the whole handle."""
        image = baker.make(
            "dashboard.Image", profile=self.profile, caption="Anchor Mill turbine hall", image="photos/anchor.jpg"
        )
        group = self._group(self._search(q="anchor mill", types="photos").json(), "photos")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], "")
        self.assertEqual(result["object_uuid"], str(image.uuid))

    def test_visit_result_names_its_pin_by_slug_and_itself_by_uuid(self) -> None:
        """A visit is a sub-resource: the slug opens the pin, the uuid says which visit."""
        pin = self._make_pin(name="Anchor Mill")
        visit = baker.make("dashboard.PinVisit", pin=pin, visited_at=timezone.now(), notes="Anchor Mill service tunnel")
        group = self._group(self._search(q="service tunnel", types="visits").json(), "visits")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], pin.slug)
        self.assertEqual(result["object_uuid"], str(visit.uuid))

    def test_wiki_result_is_addressed_by_its_locations_slug(self) -> None:
        """Every wiki route - web and API - keys off the location, not Wiki.slug."""
        location = self._location()
        wiki = baker.make("dashboard.Wiki", location=location, name="Anchor Mill", created_by=self.profile)
        # A pin, because creating a wiki is not one of wiki_access's four clauses
        # and no longer stands in for access here. This test is about how a wiki
        # result is addressed, so it just needs one the searcher can legitimately
        # see; without the pin the page would 404 and search rightly omits it.
        baker.make("dashboard.Pin", profile=self.profile, location=location, parent_pin=None)
        location.refresh_from_db()
        group = self._group(self._search(q="anchor mill", types="wikis").json(), "wikis")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], location.slug)
        self.assertNotEqual(result["object_slug"], wiki.slug)
        self.assertEqual(result["object_uuid"], str(wiki.uuid))

    def test_trip_result_carries_the_trip_slug(self) -> None:
        """Trips are slug-addressed on both surfaces."""
        trip = baker.make("dashboard.Trip", creator=self.profile, name="Anchor Mill weekend")
        group = self._group(self._search(q="anchor mill", types="trips").json(), "trips")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], trip.slug)
        self.assertEqual(result["object_uuid"], str(trip.uuid))


class GlobalSearchScopeTests(_SearchApiTestCase):
    """``search:read`` is a floor. Every provider is gated on its own domain scope."""

    def setUp(self) -> None:
        """Populate one findable row in several different domains."""
        super().setUp()
        self.pin = self._make_pin(name="Anchor Mill")
        self.image = baker.make(
            "dashboard.Image", profile=self.profile, caption="Anchor Mill turbine hall", image="photos/anchor.jpg"
        )
        self.trip = baker.make("dashboard.Trip", creator=self.profile, name="Anchor Mill weekend")

    def test_no_search_scope_at_all_is_403(self) -> None:
        """The floor scope is still required - this endpoint is not unscoped."""
        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        self.assertEqual(self._search(raw_key=raw, q="anchor mill").status_code, 403)

    def test_bare_search_read_reaches_no_domain_at_all(self) -> None:
        """``search:read`` alone is not a master key over every provider.

        The permissive failure here is total: a view that checked only its own
        ``required_scopes`` and then ran ``default_providers()`` would answer
        this request with pins, photos, trips, visits, check-ins *and* direct
        messages, for a credential its owner granted none of those to.
        """
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value])
        body = self._search(raw_key=raw, q="anchor mill").json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["groups"], [])
        self.assertEqual(set(body["omitted_types"]), set(RESULT_TYPES))

    def test_pins_scope_admits_pins_and_nothing_else(self) -> None:
        """One domain scope opens exactly its own sections."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value])
        body = self._search(raw_key=raw, q="anchor mill").json()
        self.assertEqual({group["type"] for group in body["groups"]}, {"pins"})
        self.assertNotIn("pins", body["omitted_types"])
        self.assertIn("photos", body["omitted_types"])
        self.assertIn("trips", body["omitted_types"])

    def test_missing_photo_scope_drops_the_photo_section(self) -> None:
        """A denied section is absent and named, never empty and unexplained."""
        raw = self._key_with_scopes(
            [ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value, ApiKeyScope.TRIPS_READ.value]
        )
        body = self._search(raw_key=raw, q="anchor mill").json()
        self.assertIsNone(self._group(body, "photos"))
        self.assertIn("photos", body["omitted_types"])

    def test_denied_section_is_never_a_403_for_the_whole_call(self) -> None:
        """Partial fulfilment: a narrowly-scoped integration still gets a usable search."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.TRIPS_READ.value])
        response = self._search(raw_key=raw, q="anchor mill")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({group["type"] for group in response.json()["groups"]}, {"trips"})

    def test_requesting_a_denied_type_is_200_with_that_type_omitted(self) -> None:
        """Explicitly asking for a section you may not read is empty, not forbidden."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value])
        response = self._search(raw_key=raw, q="anchor mill", types="photos")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["omitted_types"], ["photos"])

    def test_omitted_types_is_narrowed_to_what_the_caller_asked_about(self) -> None:
        """A client that asked only for pins is not told about eight other scopes."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value])
        body = self._search(raw_key=raw, q="anchor mill", types="pins").json()
        self.assertEqual(body["omitted_types"], [])


class GlobalSearchCrossDomainScopeTests(_SearchApiTestCase):
    """``articles`` and ``comments`` are each one provider spanning more than one domain.

    ``ArticleSearchProvider`` returns the caller's own private pin articles
    *and* community wiki articles from a single queryset; ``CommentSearchProvider``
    does the same across pin, wiki and trip comment threads. Gating either
    section on just one of the domains it touches would let that one scope
    reach content from the others - these tests are the regression coverage
    for that gap.
    """

    def setUp(self) -> None:
        """Bake one pin article, one wiki (with its own article and comment), and one trip comment - all sharing a findable word."""
        super().setUp()
        self.pin = self._make_pin(name="Anchor Mill")
        self.pin_article = baker.make("dashboard.Article", pin=self.pin, content="private basement blueprint notes")
        self.wiki = baker.make(
            "dashboard.Wiki", location=self._location(), created_by=self.profile, name="Anchor Mill Wiki"
        )
        self.wiki_article = baker.make("dashboard.Article", wiki=self.wiki, content="public wiki blueprint notes")
        self.wiki_comment = baker.make(
            "dashboard.Comment", wiki=self.wiki, profile=self.profile, text="blueprint notes in the comments"
        )
        self.trip = baker.make("dashboard.Trip", creator=self.profile, name="Anchor trip")
        self.trip_comment = baker.make(
            "dashboard.TripComment", trip=self.trip, author=self.profile, text="blueprint notes on the trip"
        )

    def test_wiki_scope_alone_does_not_reach_private_pin_articles(self) -> None:
        """``wiki:read`` without ``pins:read`` must not open a section that includes private pin articles."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.WIKI_READ.value])
        body = self._search(raw_key=raw, q="blueprint").json()
        self.assertIsNone(self._group(body, "articles"))
        self.assertIn("articles", body["omitted_types"])

    def test_pins_scope_alone_does_not_reach_wiki_articles(self) -> None:
        """``pins:read`` without ``wiki:read`` must not open a section that includes wiki articles."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value])
        body = self._search(raw_key=raw, q="blueprint").json()
        self.assertIsNone(self._group(body, "articles"))
        self.assertIn("articles", body["omitted_types"])

    def test_pins_and_wiki_scope_together_reach_articles(self) -> None:
        """Holding both domain scopes is what actually opens the section."""
        raw = self._key_with_scopes(
            [ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value, ApiKeyScope.WIKI_READ.value]
        )
        body = self._search(raw_key=raw, q="blueprint").json()
        self.assertIsNotNone(self._group(body, "articles"))
        self.assertNotIn("articles", body["omitted_types"])

    def test_pins_scope_alone_does_not_reach_wiki_or_trip_comments(self) -> None:
        """``pins:read`` without ``wiki:read``/``trips:read`` must not open a section including those comments."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.PINS_READ.value])
        body = self._search(raw_key=raw, q="blueprint").json()
        self.assertIsNone(self._group(body, "comments"))
        self.assertIn("comments", body["omitted_types"])

    def test_every_comment_domain_scope_together_reaches_comments(self) -> None:
        """Holding pins, wiki and trips together is what actually opens the section."""
        raw = self._key_with_scopes(
            [
                ApiKeyScope.SEARCH_READ.value,
                ApiKeyScope.PINS_READ.value,
                ApiKeyScope.WIKI_READ.value,
                ApiKeyScope.TRIPS_READ.value,
            ],
        )
        body = self._search(raw_key=raw, q="blueprint").json()
        self.assertIsNotNone(self._group(body, "comments"))
        self.assertNotIn("comments", body["omitted_types"])


class GlobalSearchDirectMessageScopeTests(_SearchApiTestCase):
    """Direct messages: the boundary a bearer key must never cross.

    ``DirectMessageSearchProvider`` returns plaintext excerpts of message
    bodies, and ``messages:read`` is in ``OAUTH2_ONLY_SCOPES`` for exactly that
    reason - an API key is a long-lived bearer secret that ends up in CI configs
    and screenshots, and a leaked one must not become a DM reader. These tests
    exist because a search endpoint is the least obvious way to breach that: its
    author is thinking about relevance ranking, and ``search:read`` sounds like
    it already covers "search".
    """

    def setUp(self) -> None:
        """Create a plaintext DM between the key owner and the bystander."""
        super().setUp()
        baker.make(
            "dashboard.DirectMessage",
            sender=self.other_profile,
            recipient=self.profile,
            body="meet me at the anchor mill loading dock",
        )

    def test_pat_with_search_read_gets_no_message_results(self) -> None:
        """The headline case: a bare ``search:read`` key sees no DM content."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value])
        body = self._search(raw_key=raw, q="loading dock").json()
        self.assertIsNone(self._group(body, "messages"))
        self.assertEqual(body["total"], 0)
        self.assertIn("messages", body["omitted_types"])

    def test_pat_holding_every_pat_grantable_scope_still_gets_no_messages(self) -> None:
        """Widening a key across every other domain does not open this one."""
        body = self._search(q="loading dock").json()
        self.assertIsNone(self._group(body, "messages"))
        self.assertIn("messages", body["omitted_types"])

    def test_pat_with_messages_read_written_onto_the_row_is_still_refused(self) -> None:
        """The rule is about the credential *kind*, not about what the row says.

        A hand-edited row, a bad migration, or a future scope picker with a bug
        could all put ``messages:read`` on an ApiKey. ``credential_grants``
        refuses it anyway, and this asserts the search endpoint inherits that
        rather than re-deriving membership from the stored list.
        """
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.MESSAGES_READ.value])
        body = self._search(raw_key=raw, q="loading dock").json()
        self.assertIsNone(self._group(body, "messages"))
        self.assertIn("messages", body["omitted_types"])

    def test_pat_asking_for_types_messages_gets_an_empty_result_not_an_excerpt(self) -> None:
        """Naming the section explicitly does not route around the gate."""
        raw = self._key_with_scopes([ApiKeyScope.SEARCH_READ.value, ApiKeyScope.MESSAGES_READ.value])
        response = self._search(raw_key=raw, q="loading dock", types="messages")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["omitted_types"], ["messages"])
        # No section at all, so no excerpt of the body can have been rendered.
        # Asserted on `groups` rather than on the raw response text because the
        # payload legitimately echoes `query` back, and the query here is the
        # caller's own words - not something read out of anyone's messages.
        self.assertEqual(body["groups"], [])

    def test_oauth2_token_with_messages_read_does_reach_them(self) -> None:
        """The gate is a boundary, not a wall - a consented OAuth2 client passes it.

        Without this, every assertion above would still hold if the provider
        were simply broken, and the suite would be proving nothing.
        """
        raw = self._oauth_token(f"{ApiKeyScope.SEARCH_READ.value} {ApiKeyScope.MESSAGES_READ.value}")
        body = self._search(raw_key=raw, q="loading dock").json()
        group = self._group(body, "messages")
        self.assertIsNotNone(group)
        assert group is not None
        self.assertIn("loading dock", group["results"][0]["snippet"])

    def test_oauth2_message_result_is_addressed_by_the_peer_profile_slug(self) -> None:
        """A DM hit opens a conversation, which this API keys by the counterpart's slug."""
        raw = self._oauth_token(f"{ApiKeyScope.SEARCH_READ.value} {ApiKeyScope.MESSAGES_READ.value}")
        group = self._group(self._search(raw_key=raw, q="loading dock").json(), "messages")
        assert group is not None
        result = group["results"][0]
        self.assertEqual(result["object_slug"], self.other_profile.ensure_slug())
        # DirectMessage extends the plain DashboardModel and has no uuid; the
        # field is present-and-null rather than absent, so a client can rely on
        # the key existing on every result.
        self.assertIsNone(result["object_uuid"])


class GlobalSearchThrottleWiringTests(_SearchApiTestCase):
    """The endpoint carries its own bucket on top of the standard three."""

    def test_search_stacks_its_dedicated_throttle(self) -> None:
        """One search is up to ten providers of work, so it is metered separately."""
        from urbanlens.dashboard.external_api.throttling import (
            ExternalApiBurstThrottle,
            ExternalApiReadThrottle,
            ExternalApiWriteThrottle,
            GlobalSearchThrottle,
        )
        from urbanlens.dashboard.external_api.views_search import GlobalSearchView

        self.assertEqual(
            GlobalSearchView.throttle_classes,
            [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, GlobalSearchThrottle],
        )

    def test_search_is_classified_as_a_read(self) -> None:
        """``search:read`` ends in ':read', so it must not burn the write budget."""
        from urbanlens.dashboard.external_api.throttling import TIER_READ, request_tier
        from urbanlens.dashboard.external_api.views_search import GlobalSearchView

        self.assertEqual(request_tier(GlobalSearchView, "GET"), TIER_READ)

    def test_every_result_type_declares_a_scope_requirement(self) -> None:
        """A provider with no entry would be omitted forever - or, worse, ungated.

        ``filter_sources_by_grants`` only knows about the sections it is handed,
        so a result type added to ``RESULT_TYPES`` without a matching entry here
        would never appear in the mapping at all, and the view's chain filter
        (``provider.slug in grants``) would silently drop it with no explanation
        in ``omitted_types``.
        """
        from urbanlens.dashboard.external_api.views_search import SEARCH_SECTION_SCOPES

        self.assertEqual(list(SEARCH_SECTION_SCOPES), list(RESULT_TYPES))
        for slug, scopes in SEARCH_SECTION_SCOPES.items():
            self.assertIn(ApiKeyScope.SEARCH_READ, scopes, f"{slug} must also require the endpoint's floor scope")
            self.assertGreaterEqual(len(scopes), 2, f"{slug} must require a domain scope beyond the floor")


class ResultTypeParsingTests(TestCase):
    """``parse_result_types`` - the pure function behind the ``types`` parameter."""

    def test_absent_parameter_means_no_restriction(self) -> None:
        """None is "search everything the credential allows"."""
        self.assertIsNone(parse_result_types(None))

    def test_blank_parameter_means_no_restriction(self) -> None:
        """``?types=`` carries no intent to honour."""
        self.assertIsNone(parse_result_types(""))
        self.assertIsNone(parse_result_types("  ,  "))

    def test_known_slugs_survive_and_unknown_ones_are_dropped(self) -> None:
        """A newer client's unrecognized type does not fail the whole search."""
        self.assertEqual(parse_result_types("pins, TRIPS ,nope"), frozenset({"pins", "trips"}))

    def test_wholly_unrecognized_value_restricts_to_nothing(self) -> None:
        """Never None - that would silently widen a typo into an unfiltered search."""
        self.assertEqual(parse_result_types("pinz"), frozenset())

    @hypothesis_settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(st.lists(st.text(max_size=12), max_size=6))
    def test_result_is_always_a_subset_of_the_known_types(self, parts: list[str]) -> None:
        """No input can conjure a section that has no provider behind it.

        The view maps this straight onto the provider chain, so a slug that
        escaped the intersection would either be inert or - if it ever collided
        with a future key - select a provider the caller never asked for.
        """
        parsed = parse_result_types(",".join(parts))
        if parsed is not None:
            self.assertLessEqual(parsed, frozenset(RESULT_TYPES))


class EngineTypeRestrictionTests(TestCase):
    """The ``types``/``limit`` overrides the API surface needed from the engine."""

    def setUp(self) -> None:
        """Give one profile a findable pin and a findable trip."""
        self.user = baker.make(User)
        self.profile = self.user.profile
        # Two unique constraints shape every pin fixture in this module: one
        # Location per (latitude, longitude), and one Pin per (location,
        # profile). Each extra pin therefore needs its own coordinates.
        self._location_seq = 0
        self._make_pin("Anchor Mill")
        baker.make("dashboard.Trip", creator=self.profile, name="Anchor Mill weekend")

    def _make_pin(self, name: str):
        """Create one of the profile's pins at unused coordinates.

        Args:
            name: The pin's name.

        Returns:
            The created pin.
        """
        self._location_seq += 1
        location = baker.make(
            "dashboard.Location", latitude=f"39.{100 + self._location_seq}", longitude="-84.51", locality="Cincinnati"
        )
        return baker.make("dashboard.Pin", profile=self.profile, location=location, name=name)

    def _slugs(self, response) -> set[str]:
        """The result-type slugs present in a search response.

        Args:
            response: A ``SearchResponse``.

        Returns:
            The set of populated section slugs.
        """
        return {group.meta.slug for group in response.groups}

    def test_no_types_searches_every_provider(self) -> None:
        """The default is unchanged by the new parameter."""
        response = GlobalSearchEngine().search(self.profile, "anchor mill")
        self.assertEqual(self._slugs(response), {"pins", "trips"})

    def test_explicit_types_override_the_parsers_inference(self) -> None:
        """A caller's stated intent beats a keyword guess from the query text."""
        response = GlobalSearchEngine().search(self.profile, "pins anchor mill", types={"trips"})
        self.assertEqual(self._slugs(response), {"trips"})

    def test_empty_types_searches_nothing_rather_than_everything(self) -> None:
        """The fail-open trap: empty must not be read as ``parsed.types``'s "all".

        ``ParsedQuery.types`` uses empty-means-all, which is right for an
        inference and disastrous for an explicit restriction. Assigning the
        caller's empty set into ``parsed.types`` would turn a fully-denied
        credential's search into an unrestricted one.
        """
        response = GlobalSearchEngine().search(self.profile, "anchor mill", types=set())
        self.assertEqual(response.total, 0)
        self.assertEqual(response.groups, [])

    def test_explicit_types_survive_the_plain_text_fallback(self) -> None:
        """The fallback drops *inferred* structure, not the caller's own filter.

        "... in Nowhere" parses a place that matches no location, so the first
        pass finds nothing and the engine retries with the place words demoted
        back to plain text. That retry must stay inside the requested type: it
        rebuilds a fresh ``ParsedQuery`` (deliberately clearing the parser's
        inferences), and a restriction folded into that object instead of
        threaded past it would be cleared along with them - handing a
        pins-only search a trip.
        """
        self._make_pin("Anchor Mill in Nowhere")
        baker.make("dashboard.Trip", creator=self.profile, name="Anchor Mill in Nowhere")
        response = GlobalSearchEngine().search(self.profile, "anchor mill in Nowhere", types={"pins"})
        self.assertTrue(response.used_fallback)
        self.assertEqual(self._slugs(response), {"pins"})

    def test_empty_provider_chain_is_honoured_rather_than_defaulted(self) -> None:
        """The view relies on this to express "this credential may read nothing"."""
        response = GlobalSearchEngine([]).search(self.profile, "anchor mill")
        self.assertEqual(response.total, 0)

    def test_limit_caps_each_section(self) -> None:
        """Sections are capped by the caller's limit, not the shape-derived default."""
        for index in range(4):
            self._make_pin(f"Anchor Mill {index}")
        response = GlobalSearchEngine().search(self.profile, "anchor mill", types={"pins"}, limit=2)
        self.assertEqual(len(response.groups[0].results), 2)
