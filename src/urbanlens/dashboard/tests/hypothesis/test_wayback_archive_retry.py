"""A link the Wayback Machine could not archive is asked about again later (P308).

The archive task asked once: a lookup that timed out or met a 429 or 5xx, or a save that failed, was logged and
dropped, and nothing looked for links still without a snapshot. On dev, 86 links naming 63 URLs had none; asked
again, nine of ten sampled Wikipedia pages had one, and a save logged as failed had made its capture. The
deployment's own rate limit escaped the task as an error, so an import's links past the limit were dropped too.
"""

from __future__ import annotations

from datetime import timedelta
import itertools
import json
from unittest import mock

from django.conf import settings
from django.utils import timezone
from model_bakery import baker
import pytest
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.rate_limiter import (
    RateLimitExceededError,
    UpstreamThrottledError,
    _RateLimitedSession,
)

_GATEWAY = "urbanlens.dashboard.services.apis.locations.wayback_machine.WaybackMachineGateway"
_AVAILABILITY = "https://archive.org/wayback/available"
_SAVE = "https://web.archive.org/save/https://example.com/a"


def _snapshot(url: str) -> str:
    return f"https://web.archive.org/web/20260101000000/{url}"


def _found(url: str) -> dict:
    return {"archived_snapshots": {"closest": {"url": _snapshot(url)}}}


def _http_error(status: int, headers: dict[str, str] | None = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.HTTPError(str(status), response=response)


def _later(delta: timedelta) -> mock._patch:
    return mock.patch("django.utils.timezone.now", return_value=timezone.now() + delta)


class _ArchiveCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.wiki = baker.make("dashboard.Wiki")

    def _link(self, url: str, *, model: type[PinLink | WikiLink] = PinLink, age: timedelta = timedelta(days=1)):
        owner = {"pin": baker.make(Pin, profile=self.profile)} if model is PinLink else {"wiki": self.wiki}
        link = baker.make(model, url=url, wayback_url="", **owner)
        model.objects.filter(pk=link.pk).update(created=timezone.now() - age)
        return link

    def _assert_archived(self, *links: PinLink | WikiLink) -> None:
        for link in links:
            link.refresh_from_db()
            self.assertEqual(link.wayback_url, _snapshot(link.url))


class AFailedAttemptIsAskedAgainTests(_ArchiveCase):
    def test_a_link_whose_lookup_failed_is_archived_by_a_later_sweep(self) -> None:
        link = self._link("https://example.com/a")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=requests.Timeout("slow")):
            self.assertFalse(tasks.archive_pin_link_to_wayback(link.pk))

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links()
            asked.assert_not_called()
            with _later(timedelta(hours=2)):
                tasks.sweep_unarchived_links()

        asked.assert_called_once_with(link.url)
        self._assert_archived(link)

    def test_a_failed_save_is_asked_again(self) -> None:
        link = self._link("https://example.com/a")
        with (
            mock.patch(f"{_GATEWAY}.get_availability", return_value={"archived_snapshots": {}}),
            mock.patch(f"{_GATEWAY}.save_url", side_effect=_http_error(520)),
        ):
            self.assertFalse(tasks.archive_pin_link_to_wayback(link.pk))

        with _later(timedelta(hours=2)), mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found):
            tasks.sweep_unarchived_links()

        self._assert_archived(link)

    def test_every_link_naming_the_url_waits_together(self) -> None:
        """The URL is asked about once, when it comes due, however many links name it."""
        first = self._link("https://example.com/a")
        second = self._link("https://example.com/a", model=WikiLink)
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=requests.ConnectionError("down")):
            tasks.archive_pin_link_to_wayback(first.pk)

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links()
            asked.assert_not_called()
            with _later(timedelta(hours=2)):
                tasks.sweep_unarchived_links()

        asked.assert_called_once_with(first.url)
        self._assert_archived(first, second)

    def test_the_waits_grow_and_the_url_is_given_up_after_the_last(self) -> None:
        link = self._link("https://example.com/a")
        start = timezone.now()
        asked_at: list = []

        def fail(url: str) -> dict:
            asked_at.append(timezone.now())
            raise requests.Timeout("slow")

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=fail):
            tasks.archive_pin_link_to_wayback(link.pk)
            for step in [timedelta(hours=hour) for hour in range(1, 48)] + [
                timedelta(days=day) for day in range(2, 400)
            ]:
                with mock.patch("django.utils.timezone.now", return_value=start + step):
                    tasks.sweep_unarchived_links()

        gaps = [later - earlier for earlier, later in itertools.pairwise(asked_at)]
        self.assertGreaterEqual(len(asked_at), 4)
        self.assertEqual(gaps, sorted(gaps))
        self.assertLess(gaps[0], timedelta(hours=3))
        self.assertGreater(asked_at[-1] - asked_at[0], timedelta(days=14))
        self.assertLess(asked_at[-1] - start, timedelta(days=120), "given up, not asked about for ever")


class TheArchiveRefusingForNowTests(_ArchiveCase):
    """A refusal for now says nothing about the URL: it is not held against it, and the sweep stops asking."""

    def test_a_429_is_not_held_against_the_url_and_ends_the_sweep(self) -> None:
        links = [self._link(f"https://example.com/{n}") for n in range(3)]
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_http_error(429)) as asked:
            tasks.sweep_unarchived_links()
        self.assertEqual(asked.call_count, 1)

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links()

        self.assertEqual(asked.call_count, 3)
        self._assert_archived(*links)

    def test_a_503_naming_a_wait_ends_the_sweep(self) -> None:
        for n in range(3):
            self._link(f"https://example.com/{n}")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_http_error(503, {"Retry-After": "120"})) as asked:
            tasks.sweep_unarchived_links()

        self.assertEqual(asked.call_count, 1)

    def test_a_bare_503_is_held_against_its_url_not_the_sweep(self) -> None:
        """The Archive failing one page, which a sweep that stopped at it would ask about first, every time."""
        failing = self._link("https://example.com/failing", age=timedelta(days=5))
        healthy = self._link("https://example.com/healthy")

        def answer(url: str) -> dict:
            if url == failing.url:
                raise _http_error(503)
            return _found(url)

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=answer) as asked:
            tasks.sweep_unarchived_links()
            tasks.sweep_unarchived_links()

        self._assert_archived(healthy)
        self.assertEqual([call.args[0] for call in asked.call_args_list].count(failing.url), 1)

    def test_the_deployments_own_limit_is_not_an_error_and_ends_the_sweep(self) -> None:
        links = [self._link(f"https://example.com/{n}") for n in range(3)]
        refused = RateLimitExceededError("wayback_machine")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=refused) as asked:
            self.assertFalse(tasks.archive_pin_link_to_wayback(links[0].pk))
            tasks.sweep_unarchived_links()
        self.assertEqual(asked.call_count, 2)

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found):
            tasks.sweep_unarchived_links()

        self._assert_archived(*links)

    def test_an_imports_chunk_stops_asking_once_the_archive_refuses(self) -> None:
        links = [self._link(f"https://example.com/{n}", age=timedelta(0)) for n in range(3)]
        refused = RateLimitExceededError("wayback_machine")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=refused) as asked:
            result = tasks.archive_pin_links_to_wayback([link.pk for link in links])

        self.assertEqual(asked.call_count, 1)
        self.assertEqual(result, dict.fromkeys((link.pk for link in links), False))
        with _later(timedelta(hours=1)), mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found):
            tasks.sweep_unarchived_links()
        self._assert_archived(*links)


class ALinkAddedLaterTests(_ArchiveCase):
    """A link added to a URL already waiting, or given up on, keeps to the URL's state rather than asking at once."""

    def test_a_link_added_while_its_url_waits_waits_with_it(self) -> None:
        first = self._link("https://example.com/a")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=requests.Timeout("slow")):
            tasks.archive_pin_link_to_wayback(first.pk)
        added = self._link("https://example.com/a", model=WikiLink, age=timedelta(0))

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            self.assertFalse(tasks.archive_wiki_link_to_wayback(added.pk))
            with _later(timedelta(minutes=30)):
                tasks.sweep_unarchived_links()
            asked.assert_not_called()
            with _later(timedelta(hours=2)):
                tasks.sweep_unarchived_links()

        asked.assert_called_once_with(first.url)
        self._assert_archived(first, added)

    def test_a_link_added_to_a_url_given_up_on_is_not_sent(self) -> None:
        long_url = "https://example.com/" + "a" * 1970
        self._link(long_url)
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found):
            tasks.sweep_unarchived_links()
        added = self._link(long_url, model=WikiLink, age=timedelta(0))

        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            self.assertFalse(tasks.archive_wiki_link_to_wayback(added.pk))
            with _later(timedelta(days=1)):
                tasks.sweep_unarchived_links()

        asked.assert_not_called()


class WhichLinksTheSweepTakesTests(_ArchiveCase):
    def test_a_just_added_link_is_left_to_its_own_task(self) -> None:
        self._link("https://example.com/a", age=timedelta(0))
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links()
        asked.assert_not_called()

    def test_a_link_whose_task_never_ran_is_taken_up(self) -> None:
        link = self._link("https://example.com/a", model=WikiLink, age=timedelta(hours=1))
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found):
            tasks.sweep_unarchived_links()
        self._assert_archived(link)

    def test_a_snapshot_another_link_holds_is_given_without_asking(self) -> None:
        baker.make(
            WikiLink, wiki=self.wiki, url="https://example.com/a", wayback_url=_snapshot("https://example.com/a")
        )
        waiting = self._link("https://example.com/a")
        with (
            mock.patch(f"{_GATEWAY}.get_availability") as asked,
            mock.patch(f"{_GATEWAY}.save_url") as saved,
        ):
            tasks.sweep_unarchived_links()
        asked.assert_not_called()
        saved.assert_not_called()
        self._assert_archived(waiting)

    def test_a_sweep_asks_about_a_bounded_number_of_urls(self) -> None:
        for n in range(4):
            self._link(f"https://example.com/{n}")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links(limit=2)
        self.assertEqual(asked.call_count, 2)

    def test_a_url_linked_many_times_takes_one_place_in_the_batch(self) -> None:
        many = [self._link("https://example.com/a", age=timedelta(days=2)) for _ in range(3)]
        other = self._link("https://example.com/b")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links(limit=2)
        self.assertEqual(sorted(call.args[0] for call in asked.call_args_list), [many[0].url, other.url])
        self._assert_archived(*many, other)

    def test_a_link_never_to_be_sent_does_not_hold_a_place(self) -> None:
        """A share link and the site's own page are refused for good, not again by every sweep."""
        self._link(
            "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz?usp=sharing", age=timedelta(days=3)
        )
        self._link("https://urbanlens.org/dashboard/map/", model=WikiLink, age=timedelta(days=3))
        eligible = self._link("https://example.com/a")
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            for _ in range(3):
                tasks.sweep_unarchived_links(limit=1)
        asked.assert_called_once_with(eligible.url)

    def test_a_snapshot_too_long_to_store_is_not_asked_for_again(self) -> None:
        self._link("https://example.com/" + "a" * 1970)
        with mock.patch(f"{_GATEWAY}.get_availability", side_effect=_found) as asked:
            tasks.sweep_unarchived_links()
            with _later(timedelta(days=60)):
                tasks.sweep_unarchived_links()
        self.assertEqual(asked.call_count, 1)


class TheSweepIsScheduledTests(SimpleTestCase):
    def test_beat_runs_the_sweep(self) -> None:
        scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}
        self.assertIn(tasks.sweep_unarchived_links.name, scheduled)


def _response(status_code: int, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, headers=headers or {}, ok=200 <= status_code < 300)
    response.json.return_value = {}
    response.text = json.dumps({})
    return response


def _session(*responses: mock.Mock) -> _RateLimitedSession:
    session = _RateLimitedSession("wayback_machine")
    session._session = mock.Mock()
    session._session.request.side_effect = list(responses)
    return session


class TheArchivesThrottleIsSharedTests(TestCase):
    """A 429 from the Archive holds every process off for the wait it named, without a request."""

    def test_a_429_holds_off_the_next_lookup(self) -> None:
        _session(_response(429, {"Retry-After": "120"})).get(_AVAILABILITY, params={"url": "https://example.com/a"})
        second = _session()

        with pytest.raises(UpstreamThrottledError) as caught:
            second.get(_AVAILABILITY, params={"url": "https://example.com/b"})

        second._session.request.assert_not_called()
        self.assertGreaterEqual(caught.value.retry_after, 119)
        self.assertLessEqual(caught.value.retry_after, 120)

    def test_a_429_naming_no_wait_still_holds_off(self) -> None:
        _session(_response(429)).get(_SAVE)

        with pytest.raises(UpstreamThrottledError):
            _session().get(_SAVE)

    def test_a_save_throttle_leaves_lookups_open(self) -> None:
        """Save Page Now is throttled on web.archive.org; the availability lookup is archive.org's."""
        _session(_response(429)).get(_SAVE)
        lookup = _session(_response(200))

        lookup.get(_AVAILABILITY, params={"url": "https://example.com/a"})

        lookup._session.request.assert_called_once()

    def test_a_failed_save_holds_nothing_off(self) -> None:
        """A 520 is the Archive failing to fetch that one page."""
        _session(_response(520)).get(_SAVE)
        again = _session(_response(200))

        again.get(_SAVE)

        again._session.request.assert_called_once()
