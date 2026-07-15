"""Tests for tasks.archive_link_to_wayback."""

from __future__ import annotations

from unittest import mock

import requests
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.tasks import archive_link_to_wayback

_GATEWAY = "urbanlens.dashboard.services.apis.locations.wayback_machine.WaybackMachineGateway"


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
