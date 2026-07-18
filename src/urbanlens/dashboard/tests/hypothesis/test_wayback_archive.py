"""Tests for tasks.archive_link_to_wayback."""

from __future__ import annotations

from unittest import mock

from django.test import override_settings
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.wayback_machine import is_own_site_url
from urbanlens.dashboard.tasks import archive_link_to_wayback

_GATEWAY = "urbanlens.dashboard.services.apis.locations.wayback_machine.WaybackMachineGateway"


class IsOwnSiteUrlTests(TestCase):
    """is_own_site_url() gates which links archive_link_to_wayback will submit."""

    def test_matches_urbanlens_org_regardless_of_site_url(self) -> None:
        self.assertTrue(is_own_site_url("https://urbanlens.org/dashboard/map/"))

    def test_matches_urbanlens_org_subdomain(self) -> None:
        self.assertTrue(is_own_site_url("https://staging.urbanlens.org/dashboard/map/"))

    @override_settings(SITE_URL="https://my-selfhost.example.com")
    def test_matches_configured_site_url_domain(self) -> None:
        self.assertTrue(is_own_site_url("https://my-selfhost.example.com/dashboard/map/pin/abc/"))

    @override_settings(SITE_URL="https://my-selfhost.example.com")
    def test_matches_configured_site_url_subdomain(self) -> None:
        self.assertTrue(is_own_site_url("https://staging.my-selfhost.example.com/dashboard/map/"))

    @override_settings(SITE_URL="https://my-selfhost.example.com")
    def test_self_hosted_deployment_still_excludes_urbanlens_org(self) -> None:
        """A self-host runs under its own domain but must still never submit the canonical site's own URLs."""
        self.assertTrue(is_own_site_url("https://urbanlens.org/dashboard/map/"))

    def test_unrelated_domain_is_not_excluded(self) -> None:
        self.assertFalse(is_own_site_url("https://example.com/some-article"))

    def test_domain_that_merely_contains_urbanlens_org_as_a_substring_is_not_excluded(self) -> None:
        """"noturbanlens.org" is a different registrable domain, not a subdomain."""
        self.assertFalse(is_own_site_url("https://noturbanlens.org/"))

    def test_empty_or_unparseable_url_is_not_excluded(self) -> None:
        self.assertFalse(is_own_site_url(""))


class ArchiveLinkToWaybackTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile
        self.pin = baker.make(Pin, profile=self.profile)

    def test_unknown_link_model_returns_false(self) -> None:
        self.assertFalse(archive_link_to_wayback("SomethingElse", 1))

    def test_missing_link_returns_false(self) -> None:
        self.assertFalse(archive_link_to_wayback("PinLink", 999999))

    def test_link_that_already_has_a_wayback_url_is_skipped(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="https://web.archive.org/existing")
        with mock.patch(f"{_GATEWAY}.get_availability") as get_availability:
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        get_availability.assert_not_called()

    def test_uses_existing_snapshot_when_available(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with (
            mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {"closest": {"url": "https://web.archive.org/snap"}}}),
            mock.patch(f"{_GATEWAY}.save_url") as save_url,
        ):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertTrue(result)
        save_url.assert_not_called()
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "https://web.archive.org/snap")

    def test_saves_a_new_snapshot_when_none_exists(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with (
            mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {}}),
            mock.patch(f"{_GATEWAY}.save_url", return_value={"archived_url": "https://web.archive.org/fresh", "status_code": 200}),
        ):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertTrue(result)
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "https://web.archive.org/fresh")

    def test_request_failure_is_swallowed_and_returns_false(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=requests.RequestException("boom")):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "")

    def test_own_site_link_is_never_submitted(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://urbanlens.org/dashboard/map/pin/abc/", wayback_url="")
        with mock.patch(f"{_GATEWAY}.get_availability") as get_availability:
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        get_availability.assert_not_called()
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "")

    def test_no_snapshot_found_and_save_also_fails_leaves_wayback_url_blank(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with (
            mock.patch(f"{_GATEWAY}.get_availability", return_value={}),
            mock.patch(f"{_GATEWAY}.save_url", return_value={"archived_url": "", "status_code": 200}),
        ):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "")
