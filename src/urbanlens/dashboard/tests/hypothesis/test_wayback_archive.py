"""Tests for tasks.archive_link_to_wayback."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.wayback_machine import is_own_site_url
from urbanlens.dashboard.tasks import archive_link_to_wayback

_GATEWAY = "urbanlens.dashboard.services.apis.locations.wayback_machine.WaybackMachineGateway"
_SAVED = "https://web.archive.org/web/20240101000000/https://example.com/"


class IsOwnSiteUrlTests(SimpleTestCase):
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
        """ "noturbanlens.org" is a different registrable domain, not a subdomain."""
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
        link = baker.make(
            PinLink, pin=self.pin, url="https://example.com", wayback_url="https://web.archive.org/existing"
        )
        with mock.patch(f"{_GATEWAY}.get_availability") as get_availability:
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        get_availability.assert_not_called()

    def test_uses_existing_snapshot_when_available(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with (
            mock.patch(
                f"{_GATEWAY}.get_availability",
                return_value={"archived_snapshots": {"closest": {"url": "https://web.archive.org/snap"}}},
            ),
            mock.patch(f"{_GATEWAY}.save_url") as save_url,
        ):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertTrue(result)
        save_url.assert_not_called()
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "https://web.archive.org/snap")

    def test_a_snapshot_url_that_is_not_a_storable_link_is_dropped(self) -> None:
        """Wrapping a near-cap link in the archive prefix pushes it past the column; that must not fail the task."""
        long_url = "https://example.com/" + "a" * 1970
        link = baker.make(PinLink, pin=self.pin, url=long_url, wayback_url="")
        snapshot = f"https://web.archive.org/web/20230101000000/{long_url}"
        with mock.patch(
            f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {"closest": {"url": snapshot}}}
        ):
            result = archive_link_to_wayback("PinLink", link.pk)
        self.assertFalse(result)
        link.refresh_from_db()
        self.assertEqual(link.wayback_url, "")

    def test_saves_a_new_snapshot_when_none_exists(self) -> None:
        link = baker.make(PinLink, pin=self.pin, url="https://example.com", wayback_url="")
        with (
            mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {}}),
            mock.patch(
                f"{_GATEWAY}.save_url",
                return_value={"archived_url": "https://web.archive.org/fresh", "status_code": 200},
            ),
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


class OneUrlIsArchivedOnceTests(TestCase):
    """A URL linked from a pin and its wiki, or from many pins, is looked up at the Wayback Machine once."""

    _SNAPSHOT = "https://web.archive.org/web/20240101000000/https://example.com/a"

    def setUp(self) -> None:
        self.pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        self.wiki = baker.make("dashboard.Wiki")

    def test_a_snapshot_another_link_holds_is_reused_without_asking(self) -> None:
        baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url=self._SNAPSHOT)
        waiting = baker.make(WikiLink, wiki=self.wiki, url="https://example.com/a", wayback_url="")

        with mock.patch(f"{_GATEWAY}.get_availability") as get_availability, mock.patch(f"{_GATEWAY}.save_url") as save:
            self.assertTrue(archive_link_to_wayback("WikiLink", waiting.pk))

        get_availability.assert_not_called()
        save.assert_not_called()
        waiting.refresh_from_db()
        self.assertEqual(waiting.wayback_url, self._SNAPSHOT)

    def test_a_snapshot_found_is_given_to_every_link_waiting_on_the_url(self) -> None:
        first = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="")
        others = [
            baker.make(WikiLink, wiki=self.wiki, url="https://example.com/a", wayback_url=""),
            baker.make(
                PinLink, pin=baker.make(Pin, profile=baker.make("auth.User").profile), url="https://example.com/a"
            ),
        ]
        unrelated = baker.make(PinLink, pin=self.pin, url="https://example.com/b", wayback_url="")
        found = {"archived_snapshots": {"closest": {"url": self._SNAPSHOT}}}

        with mock.patch(f"{_GATEWAY}.get_availability", return_value=found) as get_availability:
            self.assertTrue(archive_link_to_wayback("PinLink", first.pk))
            for other in others:
                archive_link_to_wayback(type(other).__name__, other.pk)

        self.assertEqual(get_availability.call_count, 1)
        for link in (first, *others):
            link.refresh_from_db()
            self.assertEqual(link.wayback_url, self._SNAPSHOT)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.wayback_url, "")

    def test_a_link_whose_url_is_being_archived_is_left_to_that_task(self) -> None:
        """The task already asking gives its snapshot to this link too, so this one does not ask again."""
        first = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="")
        second = baker.make(WikiLink, wiki=self.wiki, url="https://example.com/a", wayback_url="")
        asked: list[str] = []

        def availability_while_the_other_runs(url: str) -> dict:
            asked.append(url)
            self.assertFalse(archive_link_to_wayback("WikiLink", second.pk))
            return {"archived_snapshots": {"closest": {"url": self._SNAPSHOT}}}

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=availability_while_the_other_runs):
            self.assertTrue(archive_link_to_wayback("PinLink", first.pk))

        self.assertEqual(asked, ["https://example.com/a"])
        second.refresh_from_db()
        self.assertEqual(second.wayback_url, self._SNAPSHOT)

    def test_a_failed_lookup_frees_the_url_for_the_next_task(self) -> None:
        first = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="")
        second = baker.make(WikiLink, wiki=self.wiki, url="https://example.com/a", wayback_url="")
        found = {"archived_snapshots": {"closest": {"url": self._SNAPSHOT}}}

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=[requests.RequestException("boom"), found]):
            self.assertFalse(archive_link_to_wayback("PinLink", first.pk))
            self.assertTrue(archive_link_to_wayback("WikiLink", second.pk))

    def test_an_unavailable_cache_archives_without_the_lock(self) -> None:
        """The lock only saves a duplicate lookup; a cache outage must not stop archiving, as it did not before it."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        link = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="")
        found = {"archived_snapshots": {"closest": {"url": self._SNAPSHOT}}}

        with (
            mock.patch("urbanlens.dashboard.services.core.locks.cache.add", side_effect=RedisConnectionError("down")),
            mock.patch(f"{_GATEWAY}.get_availability", return_value=found),
        ):
            self.assertTrue(archive_link_to_wayback("PinLink", link.pk))

        link.refresh_from_db()
        self.assertEqual(link.wayback_url, self._SNAPSHOT)

    def test_each_pin_whose_link_gains_a_snapshot_is_marked_changed(self) -> None:
        """The external API's sync feed pages by ``Pin.updated``; a link's ``save`` used to move it through a signal."""
        other_pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        first = baker.make(PinLink, pin=self.pin, url="https://example.com/a", wayback_url="")
        baker.make(PinLink, pin=other_pin, url="https://example.com/a", wayback_url="")
        untouched = baker.make(Pin, profile=self.pin.profile)
        baker.make(PinLink, pin=untouched, url="https://example.com/b", wayback_url="")
        stale = timezone.now() - timedelta(days=1)
        Pin.objects.filter(pk__in=[self.pin.pk, other_pin.pk, untouched.pk]).update(updated=stale)
        found = {"archived_snapshots": {"closest": {"url": self._SNAPSHOT}}}

        with mock.patch(f"{_GATEWAY}.get_availability", return_value=found):
            archive_link_to_wayback("PinLink", first.pk)

        changed = dict(
            Pin.objects.filter(pk__in=[self.pin.pk, other_pin.pk, untouched.pk]).values_list("pk", "updated")
        )
        self.assertGreater(changed[self.pin.pk], stale)
        self.assertGreater(changed[other_pin.pk], stale)
        self.assertEqual(changed[untouched.pk], stale)


class ShareLinksAreNotSentToTheArchiveTests(TestCase):
    """Save Page Now publishes what it captures, so a URL that works as a password would be published with it."""

    _SHARE_LINKS = (
        "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz?usp=sharing",
        "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit",
        "https://www.google.com/maps/d/viewer?mid=1AbCdEfGhIjKlMnOpQ",
        "https://maps.app.goo.gl/AbCdEfGhIjKlMn",
        "https://photos.app.goo.gl/AbCdEfGhIjKlMn",
        "https://www.dropbox.com/scl/fi/abcdef123456/plans.pdf?rlkey=abc123&dl=0",
        "https://1drv.ms/f/s!AbCdEfGhIjKlMn",
        "https://www.icloud.com/sharedalbum/#B0AbCdEfGhIjKlMn",
        "https://bucket.s3.amazonaws.com/plan.pdf?X-Amz-Credential=AKIA&X-Amz-Signature=abc123",
        "https://example.com/files/plan.pdf?token=abc123",
        "https://photos.example.com/share/AbCdEfGhIjKlMnOpQrStUv",
        "https://example.com/callback#access_token=abc123",
    )

    def setUp(self) -> None:
        self.pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        self.wiki = baker.make("dashboard.Wiki")

    def test_a_share_link_is_not_sent_to_the_archive(self) -> None:
        for url in self._SHARE_LINKS:
            for model, owner in ((PinLink, {"pin": self.pin}), (WikiLink, {"wiki": self.wiki})):
                link = baker.make(model, url=url, wayback_url="", **owner)
                with (
                    self.subTest(url=url, model=model.__name__),
                    mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {}}) as asked,
                    mock.patch(f"{_GATEWAY}.save_url", return_value={"archived_url": _SAVED}) as saved,
                ):
                    self.assertFalse(archive_link_to_wayback(model.__name__, link.pk))
                    asked.assert_not_called()
                    saved.assert_not_called()
                    link.refresh_from_db()
                    self.assertEqual(link.wayback_url, "")

    def test_a_link_without_a_secret_is_still_archived(self) -> None:
        """Anti-vacuity: the refusal is for the secret, not for a query string or a file host's neighbour."""
        for url in (
            "https://example.com/article?id=3&page=2",
            "https://www.google.com/maps/place/Hudson+River+State+Hospital/@41.73,-73.92,17z",
            "https://notdropbox.com/plans.pdf?dl=0",
        ):
            link = baker.make(PinLink, pin=self.pin, url=url, wayback_url="")
            with (
                self.subTest(url=url),
                mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {}}),
                mock.patch(f"{_GATEWAY}.save_url", return_value={"archived_url": _SAVED}) as saved,
            ):
                self.assertTrue(archive_link_to_wayback("PinLink", link.pk))
                saved.assert_called_once_with(url)


class ListSharedLinkSnapshotsTests(TestCase):
    """The snapshots already published from share links, for removal requests to the Internet Archive."""

    def test_it_lists_share_link_snapshots_and_nothing_else(self) -> None:
        pin = baker.make(Pin, profile=baker.make("auth.User").profile)
        wiki = baker.make("dashboard.Wiki")
        shared = baker.make(
            PinLink, pin=pin, url="https://drive.google.com/file/d/1AbC/view", wayback_url=f"{_SAVED}drive"
        )
        shared_wiki = baker.make(
            WikiLink, wiki=wiki, url="https://example.com/plan.pdf?token=abc", wayback_url=f"{_SAVED}token"
        )
        baker.make(PinLink, pin=pin, url="https://example.com/article", wayback_url=f"{_SAVED}article")
        baker.make(PinLink, pin=pin, url="https://www.dropbox.com/s/AbCdEf/plan.pdf", wayback_url="")
        out = StringIO()

        call_command("list_shared_link_snapshots", stdout=out)

        listed = out.getvalue()
        self.assertIn(f"PinLink #{shared.pk}\t{_SAVED}drive", listed)
        self.assertIn(f"WikiLink #{shared_wiki.pk}\t{_SAVED}token", listed)
        self.assertNotIn("article", listed)
        self.assertIn("2 snapshot(s)", listed)
