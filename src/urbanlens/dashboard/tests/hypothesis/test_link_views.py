"""Tests for the link HTMX views: add/delete a Pin's or Wiki's external links."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class PinLinkViewTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Old Mill")
        self.client.force_login(self.user)

    def test_add_link_creates_row(self) -> None:
        response = self.client.post(
            reverse("pin.links", args=[self.pin.slug]),
            {"name": "Local News", "url": "https://example.com/story"},
        )
        self.assertEqual(response.status_code, 200)
        link = self.pin.links.get(url="https://example.com/story")
        self.assertEqual(link.name, "Local News")

    def test_add_link_without_name_is_allowed(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "https://example.com/story"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.pin.links.filter(url="https://example.com/story").exists())

    def test_add_link_without_url_returns_400(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"name": "No URL"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pin.links.count(), 0)

    def test_add_link_with_invalid_url_returns_400(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "not-a-url"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pin.links.count(), 0)

    def test_add_link_with_non_http_scheme_returns_400(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "javascript:alert(1)"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.pin.links.count(), 0)

    def test_delete_link_removes_row(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.pin.links.filter(pk=link.pk).exists())

    def test_add_link_requires_pin_ownership(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        response = self.client.post(reverse("pin.links", args=[other_pin.slug]), {"url": "https://example.com"})
        self.assertEqual(response.status_code, 404)

    def test_delete_link_requires_pin_ownership(self) -> None:
        other_user = baker.make("auth.User")
        other_pin = baker.make(Pin, profile=other_user.profile)
        other_link = baker.make(PinLink, pin=other_pin, url="https://example.com/a")
        response = self.client.delete(reverse("pin.link.delete", args=[other_pin.slug, other_link.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinLink.objects.filter(pk=other_link.pk).exists())

    def test_empty_row_is_hidden_and_has_no_visible_placeholder_text(self) -> None:
        """Pin links: an empty row stays hidden - a header icon button is the entry
        point instead (pin_overview_partial.html), unlike the wiki page's row."""
        response = self.client.get(reverse("pin.links", args=[self.pin.slug]))
        self.assertContains(response, "hidden")
        self.assertNotContains(response, "No links yet.")

    def test_row_becomes_visible_once_a_link_is_added(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "https://example.com/story"})
        content = response.content.decode()
        # The outer row div itself must not carry the `hidden` attribute anymore.
        self.assertNotRegex(content, r'id="pin-links-row"[^>]*\bhidden\b')
        self.assertContains(response, "example.com")

    def test_row_hides_again_once_the_last_link_is_deleted(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.id]))
        content = response.content.decode()
        self.assertRegex(content, r'id="pin-links-row"[^>]*\bhidden\b')
        self.assertNotContains(response, "No links yet.")


class PinDetailsPageDetailsCardTests(TestCase):
    """The Details card's "Add a link" header button and description position."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, name="Old Mill", description="A creaky old mill.")
        # pin.overview's GET path geocodes an address-less location on the fly
        # (_ensure_location_address) - give it a route so that's skipped, since
        # tests block outbound network access.
        self.pin.location.route = "Test St"
        self.pin.location.save(update_fields=["route"])
        # deduplicated_identity_fields reads Location.place_name, which falls
        # through to a live Google Places lookup when no GooglePlace is cached
        # yet - setting this (fetch_if_missing=False under the hood) avoids
        # that network call.
        self.pin.location.cached_place_name = "Old Mill"
        self.client.force_login(self.user)

    def test_add_link_header_button_shown_when_no_links(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertContains(response, 'class="btn btn--icon-sm" title="Add a link"')

    def test_add_link_header_button_hidden_once_a_link_exists(self) -> None:
        """The header button (distinct from the row's own inline "+" toggle, which
        shares the same title text) only makes sense as an entry point for the
        empty case - once there's a link, the row's own control takes over."""
        baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertNotContains(response, 'class="btn btn--icon-sm" title="Add a link"')

    def test_description_renders_after_the_links_row(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        content = response.content.decode()
        description_pos = content.index("A creaky old mill.")
        links_row_pos = content.index('id="pin-links-row"')
        self.assertLess(links_row_pos, description_pos)


class LocationLinkViewTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="41.4", longitude="-73.4")
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)  # required for resolve_visible_wiki
        self.client.force_login(self.user)

    def test_add_link_creates_row_with_created_by(self) -> None:
        response = self.client.post(
            reverse("location.wiki.links", args=[self.location.slug]),
            {"name": "History Society", "url": "https://example.org/history"},
        )
        self.assertEqual(response.status_code, 200)
        link = self.wiki.links.get(url="https://example.org/history")
        self.assertEqual(link.created_by_id, self.profile.pk)

    def test_delete_link_removes_row(self) -> None:
        link = baker.make(WikiLink, wiki=self.wiki, url="https://example.org/a")
        response = self.client.delete(reverse("location.wiki.link.delete", args=[self.location.slug, link.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.wiki.links.filter(pk=link.pk).exists())

    def test_requires_the_requester_to_have_this_location_pinned(self) -> None:
        outsider = baker.make("auth.User")
        self.client.force_login(outsider)
        response = self.client.post(reverse("location.wiki.links", args=[self.location.slug]), {"url": "https://example.org"})
        self.assertEqual(response.status_code, 404)

    def test_empty_row_still_shows_placeholder_text_unlike_the_pin_page(self) -> None:
        """The wiki page never opted into hide_when_empty - its row stays always-visible."""
        response = self.client.get(reverse("location.wiki.links", args=[self.location.slug]))
        self.assertContains(response, "No links yet.")
        content = response.content.decode()
        self.assertNotRegex(content, r'id="wiki-links-row"[^>]*\bhidden\b')
