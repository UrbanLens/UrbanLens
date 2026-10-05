"""Wiki routes reached by a Location's uuid answer at its slug, and ``/location/<slug>/`` leads to the wiki."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.shortcuts import resolve_url
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

SLUG = "hudson-river-state-hospital"


class _VisibleWikiFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = Location.objects.create(latitude=41.7, longitude=-73.9, slug=SLUG)
        self.old = str(self.location.uuid)
        self.wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)


class UuidWikiUrlRedirectTests(_VisibleWikiFixture):
    def test_the_wiki_page_by_uuid_moves_permanently_to_its_slug(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.old]))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("location.wiki", args=[SLUG]))

    def test_the_rest_of_the_path_and_the_query_string_survive(self) -> None:
        response = self.client.get(
            reverse("location.wiki.article.revision", args=[self.old, 123]) + "?compare=4&x=a%20b"
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"], reverse("location.wiki.article.revision", args=[SLUG, 123]) + "?compare=4&x=a%20b"
        )

    def test_head_is_redirected_like_get(self) -> None:
        response = self.client.head(reverse("location.wiki.history", args=[self.old]))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("location.wiki.history", args=[SLUG]))

    def test_the_canonical_url_is_served_not_redirected(self) -> None:
        response = self.client.get(reverse("location.wiki.history", args=[SLUG]))

        self.assertEqual(response.status_code, 200)

    def test_a_location_still_addressed_by_its_uuid_is_served_in_place(self) -> None:
        Location.objects.filter(pk=self.location.pk).update(slug=self.old)

        response = self.client.get(reverse("location.wiki.history", args=[self.old]))

        self.assertEqual(response.status_code, 200)

    def test_a_post_to_the_old_url_is_answered_in_place(self) -> None:
        """A 301 would turn the POST into a GET and drop the body, so the vote has to land where it was sent."""
        response = self.client.post(reverse("location.wiki.stat_vote", args=[self.old, "danger"]), {"value": "4"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WikiStatVote.objects.filter(wiki=self.wiki, profile=self.profile, field="danger").exists())

    def test_a_location_named_by_a_provider_later_redirects_from_its_uuid(self) -> None:
        """A provider's name arriving is the path that moves a live Location off its uuid slug."""
        child_location = Location.objects.create(latitude=41.701, longitude=-73.901)
        child_uuid = str(child_location.uuid)
        self.assertEqual(child_location.slug, child_uuid)
        baker.make(Wiki, location=child_location, name="Powerhouse", parent_wiki=self.wiki)
        baker.make(Pin, profile=self.profile, location=child_location)
        child_location.official_name = "Powerhouse"
        child_location.official_name_source = "cris"
        child_location.save(update_fields=["official_name", "official_name_source", "updated"])
        self.assertEqual(child_location.slug, "powerhouse")

        response = self.client.get(reverse("location.wiki", args=[child_uuid]))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("location.wiki", args=["powerhouse"]))


class UuidWikiUrlLeakTests(_VisibleWikiFixture):
    """The redirect names the slug, so it must only ever be issued to someone the wiki itself would serve."""

    def _as_stranger(self) -> None:
        self.client.force_login(baker.make(User))

    def test_a_viewer_who_cannot_see_the_wiki_gets_the_same_404_as_for_nothing(self) -> None:
        self._as_stranger()

        hidden = self.client.get(reverse("location.wiki", args=[self.old]))
        missing = self.client.get(reverse("location.wiki", args=[str(uuid.uuid4())]))

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertNotIn("Location", hidden)
        self.assertNotContains(hidden, SLUG, status_code=404)

    def test_a_location_without_a_wiki_is_not_redirected(self) -> None:
        bare = Location.objects.create(latitude=41.8, longitude=-73.8, slug="no-wiki-here")
        baker.make(Pin, profile=self.profile, location=bare)

        response = self.client.get(reverse("location.wiki", args=[str(bare.uuid)]))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Location", response)

    def test_an_anonymous_visitor_is_sent_to_log_in_without_the_slug(self) -> None:
        self.client.logout()

        response = self.client.get(reverse("location.wiki", args=[self.old]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(resolve_url(settings.LOGIN_URL)))
        self.assertNotIn(SLUG, response["Location"])

    def test_the_redirect_is_never_stored_for_a_later_viewer(self) -> None:
        """A 301 is cacheable by default; a stored one would replay to the next account in this browser, unasked."""
        moved = self.client.get(reverse("location.wiki", args=[self.old]))
        led = self.client.get(reverse("location.detail", args=[self.old]))

        for response in (moved, led):
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("private", response["Cache-Control"])


NEW_SLUG = "hudson-river-psychiatric-center"


class _MovedSlugFixture(_VisibleWikiFixture):
    """The Location's slug changed after links to the old one were handed out."""

    def setUp(self) -> None:
        super().setUp()
        self.location.slug = NEW_SLUG
        self.location.save(update_fields=["slug", "updated"])


class FormerSlugRedirectTests(_MovedSlugFixture):
    def test_the_wiki_page_by_its_former_slug_moves_permanently_to_the_current_one(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[SLUG]))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], reverse("location.wiki", args=[NEW_SLUG]))

    def test_the_rest_of_the_path_and_the_query_string_survive(self) -> None:
        response = self.client.get(reverse("location.wiki.article.revision", args=[SLUG, 123]) + "?compare=4")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"], reverse("location.wiki.article.revision", args=[NEW_SLUG, 123]) + "?compare=4"
        )

    def test_the_redirect_is_never_stored_for_a_later_viewer(self) -> None:
        moved = self.client.get(reverse("location.wiki", args=[SLUG]))
        led = self.client.get(reverse("location.detail", args=[SLUG]))

        for response in (moved, led):
            self.assertIn("no-store", response["Cache-Control"])
            self.assertIn("private", response["Cache-Control"])

    def test_the_bare_location_url_by_its_former_slug_leads_to_the_current_wiki(self) -> None:
        response = self.client.get(reverse("location.detail", args=[SLUG]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("location.wiki", args=[NEW_SLUG]))

    def test_a_post_to_the_former_slug_is_answered_in_place(self) -> None:
        response = self.client.post(reverse("location.wiki.stat_vote", args=[SLUG, "danger"]), {"value": "4"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(WikiStatVote.objects.filter(wiki=self.wiki, profile=self.profile, field="danger").exists())

    def test_the_current_slug_is_served_not_redirected(self) -> None:
        self.assertEqual(self.client.get(reverse("location.wiki.history", args=[NEW_SLUG])).status_code, 200)


class FormerSlugLeakTests(_MovedSlugFixture):
    """The redirect names the current slug, so only someone the wiki itself would serve may receive it."""

    def test_a_viewer_who_cannot_see_the_wiki_gets_the_same_404_as_for_nothing(self) -> None:
        self.client.force_login(baker.make(User))

        for route in ("location.wiki", "location.detail", "location.wiki.history"):
            hidden = self.client.get(reverse(route, args=[SLUG]))
            missing = self.client.get(reverse(route, args=["no-such-place-anywhere"]))

            self.assertEqual(hidden.status_code, 404)
            self.assertEqual(missing.status_code, 404)
            self.assertNotIn("Location", hidden)
            self.assertNotContains(hidden, NEW_SLUG, status_code=404)
            self.assertEqual(hidden.get("Cache-Control"), missing.get("Cache-Control"))

    def test_an_anonymous_visitor_is_sent_to_log_in_without_the_current_slug(self) -> None:
        self.client.logout()

        response = self.client.get(reverse("location.wiki", args=[SLUG]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(resolve_url(settings.LOGIN_URL)))
        self.assertNotIn(NEW_SLUG, response["Location"])

    def test_a_former_slug_now_held_by_another_location_serves_that_one(self) -> None:
        holder = Location.objects.create(latitude=41.9, longitude=-73.7)
        Location.objects.filter(pk=holder.pk).update(slug=SLUG)
        baker.make(Wiki, location=holder, name="Another Place")
        baker.make(Pin, profile=self.profile, location=holder)

        response = self.client.get(reverse("location.wiki.history", args=[SLUG]))

        self.assertEqual(response.status_code, 200)


class BareLocationUrlTests(_VisibleWikiFixture):
    def test_the_location_url_leads_to_its_wiki(self) -> None:
        response = self.client.get(reverse("location.detail", args=[SLUG]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("location.wiki", args=[SLUG]))

    def test_the_location_url_by_uuid_goes_straight_to_the_canonical_wiki(self) -> None:
        response = self.client.get(reverse("location.detail", args=[self.old]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("location.wiki", args=[SLUG]))

    def test_the_query_string_is_carried_to_the_wiki(self) -> None:
        response = self.client.get(reverse("location.detail", args=[SLUG]) + "?tab=photos")

        self.assertEqual(response["Location"], reverse("location.wiki", args=[SLUG]) + "?tab=photos")

    def test_no_query_string_takes_the_redirect_off_this_site(self) -> None:
        for query in ("?next=//elsewhere.test/", "?a=1&b=https://elsewhere.test", "?%2F%2Felsewhere.test"):
            with self.subTest(query=query):
                response = self.client.get(reverse("location.detail", args=[SLUG]) + query)

                self.assertTrue(
                    response["Location"].startswith(reverse("location.wiki", args=[SLUG])), response["Location"]
                )

    def test_the_location_url_is_reachable_at_its_plain_path(self) -> None:
        self.assertEqual(reverse("location.detail", args=[SLUG]), f"/dashboard/location/{SLUG}/")

    def test_a_viewer_who_cannot_see_the_wiki_gets_the_same_404_as_for_nothing(self) -> None:
        self.client.force_login(baker.make(User))

        hidden = self.client.get(reverse("location.detail", args=[SLUG]))
        missing = self.client.get(reverse("location.detail", args=["no-such-place"]))

        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertNotIn("Location", hidden)

    def test_a_location_without_a_wiki_is_a_404(self) -> None:
        bare = Location.objects.create(latitude=41.8, longitude=-73.8, slug="no-wiki-here")
        baker.make(Pin, profile=self.profile, location=bare)

        response = self.client.get(reverse("location.detail", args=["no-wiki-here"]))

        self.assertEqual(response.status_code, 404)

    def test_an_anonymous_visitor_is_sent_to_log_in(self) -> None:
        self.client.logout()

        response = self.client.get(reverse("location.detail", args=[SLUG]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(resolve_url(settings.LOGIN_URL)))
