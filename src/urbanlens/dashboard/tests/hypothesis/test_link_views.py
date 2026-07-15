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
