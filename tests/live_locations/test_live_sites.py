"""The live-locations suite's own machinery, which runs without REData: relevance, gating and the client's retries."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

from live_sites import Answer, InconclusiveError, LiveRedata, Site, load_sites
import pytest
import requests

if TYPE_CHECKING:
    from collections.abc import Iterator


def _site(**overrides: Any) -> Site:
    fields: dict[str, Any] = {
        "key": "athens",
        "name": "Athens Lunatic Asylum",
        "wikipedia": "Athens Lunatic Asylum",
        "place": "Athens, Ohio",
        "state": "OH",
        "lat": 39.32,
        "lng": -82.1,
        "built": 1874,
        "status": "repurposed",
        "aliases": ("The Ridges", "Athens State Hospital"),
    }
    return Site(**{**fields, **overrides})


class TestMentions:
    def test_a_name_inside_another_word_is_not_a_mention(self) -> None:
        anna = _site(name="Anna State Hospital", wikipedia="Anna State Hospital", place="Anna, Illinois", aliases=())

        assert not anna.mentions("Savannah state hospital closes")

    def test_the_town_alone_is_not_a_mention(self) -> None:
        assert not _site().mentions("Athens, Greece: the Acropolis reopens")

    def test_a_shared_word_with_a_person_is_not_a_mention(self) -> None:
        warren = _site(name="Warren State Hospital", wikipedia="Warren State Hospital", place="Warren, Pennsylvania")

        assert not warren.mentions("Warren Buffett visits a state-of-the-art hospital")

    def test_a_name_with_punctuation_and_case_changes_is_a_mention(self) -> None:
        assert _site().mentions("Tour of the ATHENS LUNATIC-ASYLUM, 1890")

    def test_an_alias_is_a_mention(self) -> None:
        assert _site().mentions(None, "Ghost stories from Athens State Hospital")

    def test_a_name_without_an_institution_word_needs_the_town(self) -> None:
        assert not _site().mentions("A hike along the ridges")
        assert _site().mentions("The Ridges, Athens: a history")

    def test_a_name_other_campuses_share_needs_its_qualifier_or_town(self) -> None:
        central = _site(
            key="central-in",
            name="Central State Hospital",
            wikipedia="Central State Hospital (Indiana)",
            place="Indianapolis, Indiana",
            aliases=(),
        )

        assert not central.mentions("Central State Hospital in Milledgeville, Georgia")
        assert central.mentions("Central State Hospital in Indianapolis")
        assert central.mentions("Indiana's Central State Hospital")

    def test_every_catalogue_site_mentions_its_own_wikipedia_title(self) -> None:
        for site in load_sites():
            assert site.mentions(f"{site.wikipedia} - {site.place}"), site.key


def _load_conftest() -> Any:
    spec = importlib.util.spec_from_file_location("live_locations_conftest", Path(__file__).with_name("conftest.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Item:
    def __init__(self, check: str | None) -> None:
        self.markers: list[Any] = []
        self._check = check

    def get_closest_marker(self, name: str) -> Any:
        return mock.Mock(args=(self._check,)) if name == "live_check" and self._check else None

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


class TestGate:
    def test_only_live_checks_are_skipped_without_the_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UL_LIVE_LOCATIONS", raising=False)
        live, other = _Item("parcel"), _Item(None)

        _load_conftest().pytest_collection_modifyitems(mock.Mock(), [live, other])

        assert any(getattr(marker, "name", "") == "skip" for marker in live.markers)
        assert other.markers == []


def _response(status: int, body: Any, headers: dict[str, str] | None = None) -> mock.Mock:
    response = mock.Mock(spec=requests.Response, status_code=status, headers=headers or {}, text="")
    response.json.return_value = body
    return response


class TestLiveRedataGet:
    @pytest.fixture
    def client(self) -> LiveRedata:
        return LiveRedata("https://redata.example.test", "key", max_wait_seconds=60)

    @pytest.fixture
    def get(self, client: LiveRedata) -> Iterator[mock.Mock]:
        with mock.patch.object(client.session, "get") as get, mock.patch("live_sites.time.sleep"):
            yield get

    @pytest.mark.parametrize(
        ("body", "headers"),
        [
            ({"error": "source_rate_limited", "message": "busy"}, {}),
            ({"error": "source_error", "message": "upstream timed out"}, {}),
            ({"error": "anything"}, {"Retry-After": "2"}),
        ],
    )
    def test_a_503_that_says_ask_again_is_retried(
        self, client: LiveRedata, get: mock.Mock, body: dict[str, str], headers: dict[str, str]
    ) -> None:
        get.side_effect = [_response(503, body, headers), _response(200, {"results": []})]

        assert client.get("parcels/").status == 200

    def test_still_busy_when_the_wait_runs_out_is_inconclusive(self, client: LiveRedata, get: mock.Mock) -> None:
        client.max_wait_seconds = 0
        get.return_value = _response(503, {"error": "source_error"})

        with pytest.raises(InconclusiveError):
            client.get("parcels/")

    def test_a_server_error_is_not_remembered(self, client: LiveRedata, get: mock.Mock) -> None:
        get.side_effect = [_response(500, {"error": "boom"}), _response(200, {"results": []})]

        assert client.get("parcels/").status == 500
        assert client.get("parcels/").status == 200

    def test_an_answer_is_remembered(self, client: LiveRedata, get: mock.Mock) -> None:
        get.return_value = _response(200, {"results": []})

        first = client.get("parcels/", lat=1)

        assert client.get("parcels/", lat=1) is first
        assert isinstance(first, Answer)
        assert get.call_count == 1
