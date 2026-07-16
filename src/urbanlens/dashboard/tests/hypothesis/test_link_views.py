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

    def test_empty_row_shows_placeholder_text(self) -> None:
        """The pin links row now lives in its own always-visible card (the header's
        "Add a link" button is the entry point regardless of link count), so it no
        longer needs to hide itself when empty - matches the wiki page's row now."""
        response = self.client.get(reverse("pin.links", args=[self.pin.slug]))
        self.assertContains(response, "No links yet.")

    def test_row_shows_the_new_link_once_added(self) -> None:
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "https://example.com/story"})
        self.assertContains(response, "example.com")
        self.assertNotContains(response, "No links yet.")

    def test_row_shows_the_placeholder_again_once_the_last_link_is_deleted(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.id]))
        self.assertContains(response, "No links yet.")


class PinDetailsPageLinksCardTests(TestCase):
    """Links moved out of the Details card into their own standalone card -
    see docs/prompts.txt's resolution note for why."""

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
        # deduplicated_identity_fields reads Location.place_name, which is
        # cache-only (no live Google Places call from a plain property access)
        # - set here just so has_place_name()/place_name have something to show.
        self.pin.location.cached_place_name = "Old Mill"
        self.client.force_login(self.user)

    def test_links_card_renders_with_its_own_heading(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertContains(response, 'class="pin-links-card card card--secondary"')
        self.assertContains(response, "<span>Links</span>")

    def test_add_link_header_button_shown_when_no_links(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertContains(response, 'class="btn btn--icon-sm" title="Add a link"')

    def test_add_link_header_button_stays_shown_once_a_link_exists(self) -> None:
        """Unlike the old Details-card version, the Links card header button is
        always the entry point now - no more disappearing once a link exists
        (that was the old row's own inline toggle's job; it no longer renders one)."""
        baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertContains(response, 'class="btn btn--icon-sm" title="Add a link"')

    def test_links_card_renders_after_the_details_card(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        content = response.content.decode()
        details_pos = content.index('class="pin-details location-details card"')
        links_card_pos = content.index('class="pin-links-card card card--secondary"')
        self.assertLess(details_pos, links_card_pos)

    def test_add_link_header_button_opens_the_dialog(self) -> None:
        """Regression guard: this used to inline-reveal the row's own add-form,
        whose Cancel button only hid the form (not the row), leaving a stray
        icon visible where the inputs had been - see _pin_link_add_dialog.html."""
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertContains(response, "document.getElementById('pin-link-add-dialog').showModal()")

    def test_row_never_renders_its_own_inline_form(self) -> None:
        """The row's inline add-toggle/form only exists for the wiki page now -
        on the pin page the Links card header button is the sole entry point,
        regardless of link count."""
        baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        self.assertNotContains(response, 'class="pin-link-add-form"')

    def test_links_card_header_badge_hidden_when_empty(self) -> None:
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn('id="pin-links-count-badge" hidden', content)

    def test_links_card_header_badge_shows_count(self) -> None:
        baker.make(PinLink, pin=self.pin, url="https://example.com/a")
        baker.make(PinLink, pin=self.pin, url="https://example.com/b")
        response = self.client.get(reverse("pin.overview", args=[self.pin.slug]))
        content = response.content.decode()
        self.assertIn('id="pin-links-count-badge">2</span>', content)

    def test_links_card_header_badge_updates_via_oob_swap_on_add(self) -> None:
        """Regression guard: the header badge lives in a different partial
        (pin_overview_partial.html) than the row that actually swaps on add/
        delete (_pin_links_row.html) - without an OOB fragment carrying the
        new count, the badge would go stale until a full page reload."""
        response = self.client.post(reverse("pin.links", args=[self.pin.slug]), {"url": "https://example.com/story"})
        content = response.content.decode()
        self.assertIn('id="pin-links-count-badge" hx-swap-oob="true">1</span>', content)


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

    def test_empty_row_shows_placeholder_text(self) -> None:
        response = self.client.get(reverse("location.wiki.links", args=[self.location.slug]))
        self.assertContains(response, "No links yet.")

    def test_still_uses_its_own_inline_add_form_not_the_pin_pages_dialog(self) -> None:
        """The wiki page's inline reveal-form UX is unaffected by the pin page's
        add-link-dialog conversion - use_dialog is unset here."""
        response = self.client.get(reverse("location.wiki.links", args=[self.location.slug]))
        self.assertContains(response, 'class="pin-link-add-form"')
        self.assertNotContains(response, "pin-link-add-dialog")
