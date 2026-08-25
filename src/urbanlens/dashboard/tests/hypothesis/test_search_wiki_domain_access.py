"""Global search and map autocomplete must grant wiki-adjacent access through the same
domain rule the wiki page itself enforces, not a narrower exact-Location pin match.

See docs/GOALS_CODE_AUDIT.md ("Discovery: browse and search parity" / "Wiki access"). Before
this fix, WikiSearchProvider, ArticleSearchProvider, CommentSearchProvider,
PhotoSearchProvider, and services.map_pins.autocomplete.search_local each reimplemented wiki
visibility with ``location__pins__profile=profile`` - an exact Location-row match - instead of
calling services.wiki.wiki_access.visible_wiki_location_ids, the single canonical domain-aware
rule already proven in test_wiki_access_boundary_mates.py (a pin on a *different* Location row
of the same real-world place, e.g. a different building on the same parcel, still counts). A
user who could browse straight to a wiki via that boundary-mate pin could not find it, its
article, its comments, or its photos by searching - and the map search bar under-served it too.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.map_pins.autocomplete import search_local
from urbanlens.dashboard.services.places import resolution

from .test_places_campus import make_place, square as _square


def _group_titles(response, slug: str) -> list[str]:
    for group in response.groups:
        if group.meta.slug == slug:
            return [r.title for r in group.results]
    return []


class _BoundaryMateFixture(TestCase):
    """Shared setup: a wiki at one Location, and a profile whose only pin is on a DIFFERENT
    Location row of the same place - the boundary-mate scenario. Wiki content is attributed to
    a third profile, never ``self.profile``, so the only way ``self.profile`` can reach it is
    through the domain-access rule under test, not an incidental ``profile=`` match."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.content_owner = baker.make(User).profile

        self.parcel = make_place(PlaceKind.PARCEL, _square(-74.0, 40.0, 0.003), name="Boundary-mate Parcel")
        self.wiki_location = Location.objects.create(latitude=40.0, longitude=-74.0, official_name="Wiki Spot")
        resolution.resolve_location_place(self.wiki_location)
        self.wiki = baker.make(Wiki, location=self.wiki_location, place=self.parcel, name="Boundary Wiki")

        self.mate_location = Location.objects.create(latitude=40.0005, longitude=-74.0005)
        resolution.resolve_location_place(self.mate_location)
        self.assertNotEqual(self.mate_location.pk, self.wiki_location.pk)
        self.mate_pin = baker.make(Pin, profile=self.profile, location=self.mate_location)

        self.far_location = Location.objects.create(latitude=41.0, longitude=-73.0)
        resolution.resolve_location_place(self.far_location)


class WikiSearchDomainAccessTests(_BoundaryMateFixture):
    def test_finds_wiki_via_a_boundary_mate_pin(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "Boundary Wiki")
        self.assertIn("Boundary Wiki", _group_titles(response, "wikis"))

    def test_a_profile_with_no_pin_on_the_place_does_not_find_it(self) -> None:
        stranger = baker.make(User).profile
        baker.make(Pin, profile=stranger, location=self.far_location)

        response = GlobalSearchEngine().search(stranger, "Boundary Wiki")

        self.assertNotIn("Boundary Wiki", _group_titles(response, "wikis"))


class ArticleSearchDomainAccessTests(_BoundaryMateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.article = baker.make("dashboard.Article", wiki=self.wiki, content="A distinctive paragraph about turbines.")

    def test_finds_wiki_article_via_a_boundary_mate_pin(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "distinctive paragraph turbines")
        self.assertTrue(_group_titles(response, "articles"))

    def test_a_profile_with_no_pin_on_the_place_does_not_find_it(self) -> None:
        stranger = baker.make(User).profile
        response = GlobalSearchEngine().search(stranger, "distinctive paragraph turbines")
        self.assertFalse(_group_titles(response, "articles"))


class CommentSearchDomainAccessTests(_BoundaryMateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.comment = baker.make("dashboard.Comment", wiki=self.wiki, profile=self.content_owner, text="a very distinctive comment string")

    def test_finds_wiki_comment_via_a_boundary_mate_pin(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "distinctive comment string")
        self.assertTrue(_group_titles(response, "comments"))

    def test_a_profile_with_no_pin_on_the_place_does_not_find_it(self) -> None:
        stranger = baker.make(User).profile
        response = GlobalSearchEngine().search(stranger, "distinctive comment string")
        self.assertFalse(_group_titles(response, "comments"))


class PhotoSearchDomainAccessTests(_BoundaryMateFixture):
    """Unlike the Wiki/Article/Comment siblings above, a photo is gated by
    ``ImageQuerySet.visible_to`` on top of the domain rule this file is about,
    and that gate has two independent halves that both must open before a
    stranger's photo is findable at all:

    1. Container reach - the photo must be shared *into* a wiki the viewer can
       reach (``_shared_within_reach_of``). Merely sitting at a domain-visible
       Location is not sharing; see ``test_..._not_shared_to_the_wiki_...``.
    2. Uploader consent - the uploader's ``photo_upload_visibility`` AND the
       viewer's own ``viewer_photo_filter`` must *each* independently admit
       the other (``ImageQuerySet._allowed_uploader_ids``). ``self.profile``
       and ``content_owner`` share no pin/friend/trip, so the default
       ANYTHING_IN_COMMON setting fails this gate on both sides regardless of
       wiki reach; see ``test_..._without_open_visibility_...``.

    Both must be satisfied for the positive case below, and each is tested in
    isolation so a future change to either gate fails the right test.
    """

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.profile.model import VisibilityChoice

        self.profile.viewer_photo_filter = VisibilityChoice.ANYONE
        self.profile.save(update_fields=["viewer_photo_filter"])
        self.content_owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.content_owner.save(update_fields=["photo_upload_visibility"])
        self.image = baker.make(
            "dashboard.Image",
            location=self.wiki_location,
            wiki=self.wiki,
            profile=self.content_owner,
            image="pin_images/boundary_mate_test.jpg",
            caption="a very distinctive photo caption",
        )

    def test_finds_a_wiki_photo_via_a_boundary_mate_pin(self) -> None:
        response = GlobalSearchEngine().search(self.profile, "distinctive photo caption")
        self.assertTrue(_group_titles(response, "photos"))

    def test_a_profile_with_no_pin_on_the_place_does_not_find_it(self) -> None:
        stranger = baker.make(User).profile
        response = GlobalSearchEngine().search(stranger, "distinctive photo caption")
        self.assertFalse(_group_titles(response, "photos"))

    def test_a_photo_only_at_the_location_not_shared_to_the_wiki_is_not_found(self) -> None:
        """Gate 1 in isolation. Same location, same open visibility settings
        as the photo in setUp - the only difference is the missing ``wiki=``
        FK - and that alone must still hide it: domain reach to a *place* is
        not the same as a deliberate contribution to its wiki."""
        # A nonce disjoint from setUp's "a very distinctive photo caption" -
        # sharing words with it would let this photo's absence hide behind a
        # fuzzy/trigram match on the *other*, correctly-visible photo.
        baker.make(
            "dashboard.Image",
            location=self.wiki_location,
            profile=self.content_owner,
            image="pin_images/boundary_mate_unshared.jpg",
            caption="zzqqxx-unshared-nonce",
        )

        response = GlobalSearchEngine().search(self.profile, "zzqqxx-unshared-nonce")

        self.assertFalse(_group_titles(response, "photos"))

    def test_a_photo_shared_to_the_wiki_without_open_visibility_is_not_found(self) -> None:
        """Gate 2 in isolation. A second photo, properly shared to the same
        wiki this test's domain access reaches, but its uploader left at the
        default ANYTHING_IN_COMMON setting with no pin/friend/trip in common
        with self.profile - despite self.profile's own filter already being
        wide open from setUp, the uploader's own restriction alone is enough
        to keep it hidden."""
        default_visibility_owner = baker.make(User).profile
        # Nonce, same reason as above.
        baker.make(
            "dashboard.Image",
            location=self.wiki_location,
            wiki=self.wiki,
            profile=default_visibility_owner,
            image="pin_images/boundary_mate_default_vis.jpg",
            caption="zzqqxx-defaultvis-nonce",
        )

        response = GlobalSearchEngine().search(self.profile, "zzqqxx-defaultvis-nonce")

        self.assertFalse(_group_titles(response, "photos"))


class AutocompleteWikiDomainAccessTests(_BoundaryMateFixture):
    def test_finds_wiki_via_a_boundary_mate_pin(self) -> None:
        results = search_local("Boundary Wiki", self.profile)
        self.assertTrue(any(r.title == "Boundary Wiki" for r in results))

    def test_a_profile_with_no_pin_on_the_place_does_not_find_it(self) -> None:
        stranger = baker.make(User).profile
        results = search_local("Boundary Wiki", stranger)
        self.assertFalse(any(r.title == "Boundary Wiki" for r in results))
