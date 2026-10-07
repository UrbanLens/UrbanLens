"""The live-locations suite's own machinery, which runs without REData: relevance, gating and the client's retries."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from unittest import mock

from live_sites import Answer, InconclusiveError, LiveRedata, Site, eligible_only_note, load_sites, settled
import pytest
import requests
import test_redata_endpoints as endpoints

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


def _catalogued(key: str) -> Site:
    return next(site for site in load_sites() if site.key == key)


class TestCatalogue:
    def test_no_key_or_nrhp_reference_is_used_twice(self) -> None:
        sites = load_sites()
        references = [site.nrhp for site in sites if site.nrhp]

        assert len({site.key for site in sites}) == len(sites)
        assert len(set(references)) == len(references)

    def test_an_nrhp_reference_is_eight_digits(self) -> None:
        assert [site.key for site in load_sites() if site.nrhp and not re.fullmatch(r"\d{8}", site.nrhp)] == []

    @pytest.mark.parametrize(
        ("key", "nrhp"),
        [
            ("central-state-hospital-indiana", "72000011"),
            ("eastern-state-hospital-washington", "97001084"),
            ("kalamazoo-regional-psychiatric-hospital", "72000624"),
            ("mendota-mental-health-institute", "88002183"),
            ("minnesota-security-hospital", "86002117"),
        ],
    )
    def test_a_campus_listed_as_a_building_or_district_carries_that_reference(self, key: str, nrhp: str) -> None:
        assert _catalogued(key).nrhp == nrhp

    @pytest.mark.parametrize("key", ["jacksonville-developmental-center", "central-state-hospital-virginia"])
    def test_a_listing_the_register_removed_is_not_expected(self, key: str) -> None:
        site = _catalogued(key)

        assert site.nrhp == ""
        assert "removed" in site.known_issues["register"]

    def test_a_campus_whose_kirkbride_was_razed_but_whose_buildings_stand_is_standing(self) -> None:
        assert _catalogued("mendocino-state-hospital").standing


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


class TestNewsQueries:
    def test_a_name_other_campuses_share_is_searched_with_its_town_and_state(self) -> None:
        assert _catalogued("central-state-hospital-kentucky").news_queries == (
            '"Central State Hospital" Anchorage Kentucky',
        )
        assert _catalogued("western-state-hospital-kentucky").news_queries == (
            '"Western State Hospital" Hopkinsville Kentucky',
        )

    def test_a_name_that_says_it_is_an_institution_is_searched_as_it_is(self) -> None:
        assert _site().news_queries == (
            '"Athens Lunatic Asylum"',
            '"The Ridges" Athens Ohio',
            '"Athens State Hospital"',
        )

    def test_a_wikipedia_disambiguator_is_never_searched(self) -> None:
        queries = [query for site in load_sites() for query in site.news_queries]

        assert [query for query in queries if "(" in query] == []

    def test_every_name_that_needs_a_qualifier_is_searched_with_its_place(self) -> None:
        for site in load_sites():
            place = site.place.replace(",", "")
            for query in site.news_queries:
                phrase = query.split('"')[1]
                assert query.endswith(f'" {place}') == site.needs_qualifier(phrase), (site.key, query)

    def test_a_name_two_labels_share_is_searched_once(self) -> None:
        for site in load_sites():
            assert len(site.news_queries) == len({_normalized_query(query) for query in site.news_queries}), site.key


def _normalized_query(query: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", query.lower()))


class _SearchStub(LiveRedata):
    """Answers a news search from a table of query to result titles, and remembers what was asked."""

    def __init__(self, titles: dict[str, list[str]]) -> None:
        super().__init__("https://redata.example.test", "key")
        self.titles = titles
        self.asked: list[str] = []

    def get(self, path: str, **params: Any) -> Answer:
        assert path == "search/news/"
        self.asked.append(params["q"])
        return Answer(200, {"results": [{"title": title} for title in self.titles.get(params["q"], [])]}, 0.0)


class TestNewsCheck:
    def test_a_shared_name_is_never_searched_without_its_town(self) -> None:
        site = _catalogued("central-state-hospital-kentucky")
        stub = _SearchStub(
            {
                '"Central State Hospital"': ["Central State Hospital in Milledgeville closes wards"],
                '"Central State Hospital" Anchorage Kentucky': [
                    "Central State Hospital in Anchorage, Kentucky: a history"
                ],
            }
        )

        endpoints.test_news_coverage_of_the_campus_is_found(stub, site)

        assert stub.asked == ['"Central State Hospital" Anchorage Kentucky']

    def test_news_about_another_campus_of_the_name_fails_the_check(self) -> None:
        site = _catalogued("western-state-hospital-kentucky")
        stub = _SearchStub(
            {'"Western State Hospital" Hopkinsville Kentucky': ["Western State Hospital in Lakewood, Washington"]}
        )

        with pytest.raises(pytest.fail.Exception, match="names the campus"):
            endpoints.test_news_coverage_of_the_campus_is_found(stub, site)


class TestRegisterListings:
    @pytest.fixture
    def trenton(self) -> Site:
        return _catalogued("trenton-psychiatric-hospital")

    @staticmethod
    def _district_row(name: str, district: str) -> dict[str, Any]:
        return {
            "provider": "nj_shpo",
            "external_id": "1",
            "name": name,
            "attributes": {"HD_NAME": district, "STATUS": "ELIGIBLE_HD"},
        }

    def test_a_campus_recorded_as_a_district_on_each_building_is_listed(self, trenton: Site) -> None:
        rows = [
            self._district_row("Main Hospital Building", "Trenton Psychiatric Hospital Historic District"),
            self._district_row("Support Building", "Trenton Psychiatric Hospital Historic District"),
            self._district_row("A House on Hermitage Avenue", "Hermitage Avenue Historic District"),
        ]

        assert trenton.register_listings(rows) == rows[:2]

    def test_a_row_is_still_listed_by_its_own_name(self, trenton: Site) -> None:
        row = {"external_id": "1", "name": "Trenton Psychiatric Hospital Main Building", "attributes": {}}

        assert trenton.register_listings([row]) == [row]

    @pytest.mark.parametrize("attributes", [None, "", [], {}, {"HD_NAME": None, "STATUS": 3}])
    def test_a_row_with_nothing_in_its_attributes_is_read_by_name_alone(self, trenton: Site, attributes: Any) -> None:
        assert trenton.register_listings([{"name": "Main Hospital Building", "attributes": attributes}]) == []

    def test_a_district_nested_in_the_attributes_is_read(self, trenton: Site) -> None:
        row = {
            "name": "Main Hospital Building",
            "attributes": {"layer": {"names": ["Trenton Psychiatric Hospital District"]}},
        }

        assert trenton.register_listings([row]) == [row]

    def test_an_attribute_that_names_the_campus_but_not_a_district_is_not_a_listing(self) -> None:
        osawatomie = _catalogued("osawatomie-state-hospital")
        survey = {"name": "Power Plant", "attributes": {"HISTORIC_NAME": "Osawatomie State Hospital Power Plant"}}

        assert osawatomie.register_listings([survey]) == []

    def test_a_district_of_another_campus_with_the_same_name_is_not_listed(self) -> None:
        indiana = _catalogued("central-state-hospital-indiana")
        georgia = {
            "name": "Powell Building",
            "attributes": {"HD_NAME": "Central State Hospital Historic District, Milledgeville, Georgia"},
        }
        here = {
            "name": "Powell Building",
            "attributes": {"HD_NAME": "Central State Hospital Historic District, Indianapolis"},
        }

        assert indiana.register_listings([georgia, here]) == [here]

    def test_the_nrhp_reference_in_external_id_or_in_an_attribute_lists_the_campus(self) -> None:
        mendota = _catalogued("mendota-mental-health-institute")
        by_id = {"external_id": "88002183", "name": "Wisconsin Memorial Hospital Historic District"}
        by_attribute = {"external_id": "x1", "name": "Cottage 5", "attributes": {"NR_REF": 88002183}}
        elsewhere = {"external_id": "x2", "name": "Cottage 6", "attributes": {"NR_REF": 74000076}}

        assert mendota.register_listings([by_id, by_attribute, elsewhere]) == [by_id, by_attribute]

    @pytest.mark.parametrize("status", ["NOT_ELIGIBLE_HD", "DELISTED_HD", "Removed", "not eligible"])
    def test_a_record_the_register_withdrew_is_not_a_listing_however_it_names_the_campus(
        self, trenton: Site, status: str
    ) -> None:
        by_name = {"name": "Trenton Psychiatric Hospital", "status": status}
        by_district = {
            "name": "Main Hospital Building",
            "attributes": {"HD_NAME": "Trenton Psychiatric Hospital Historic District", "STATUS": status},
        }
        by_number = _catalogued("mendota-mental-health-institute").register_listings(
            [{"external_id": "88002183", "name": "Wisconsin Memorial Hospital Historic District", "status": status}]
        )

        assert trenton.register_listings([by_name, by_district]) == []
        assert by_number == []

    def test_an_eligible_district_counts_by_the_status_in_the_row_or_in_its_attributes(self, trenton: Site) -> None:
        in_row = {
            "name": "Main Hospital Building",
            "status": "ELIGIBLE_HD",
            "attributes": {"HD_NAME": "Trenton Psychiatric Hospital Historic District"},
        }
        in_attributes = self._district_row("Support Building", "Trenton Psychiatric Hospital Historic District")

        assert trenton.register_listings([in_row, in_attributes]) == [in_row, in_attributes]


class TestEligibleOnlyNote:
    def test_rows_that_are_all_eligible_districts_say_the_campus_is_not_listed(self) -> None:
        rows = [
            TestRegisterListings._district_row(
                "Main Hospital Building", "Trenton Psychiatric Hospital Historic District"
            ),
            {"name": "Support Building", "status": "ELIGIBLE_HD", "attributes": {}},
        ]

        note = eligible_only_note(rows)

        assert note == "matched only eligible, not listed, register rows: 2 with status ELIGIBLE_HD"

    def test_one_listing_among_them_says_nothing(self) -> None:
        rows = [
            TestRegisterListings._district_row(
                "Main Hospital Building", "Trenton Psychiatric Hospital Historic District"
            ),
            {"name": "Trenton Psychiatric Hospital", "status": "Listed", "attributes": {}},
        ]

        assert eligible_only_note(rows) == ""

    def test_a_row_with_no_status_is_not_called_eligible(self) -> None:
        assert eligible_only_note([{"name": "Trenton Psychiatric Hospital"}]) == ""
        assert eligible_only_note([]) == ""


class TestRegisterCheck:
    def test_a_campus_found_only_in_the_attributes_passes(self) -> None:
        trenton = _catalogued("trenton-psychiatric-hospital")
        row = TestRegisterListings._district_row(
            "Main Hospital Building", "Trenton Psychiatric Hospital Historic District"
        )
        stub = mock.Mock()
        stub.get.return_value = Answer(200, {"results": [row]}, 0.0)
        recorded: list[tuple[str, object]] = []

        endpoints.test_a_historic_register_lists_the_campus(stub, trenton, lambda *pair: recorded.append(pair))

        assert recorded == [("register", "matched only eligible, not listed, register rows: 1 with status ELIGIBLE_HD")]

    def test_a_campus_with_a_listing_records_nothing(self) -> None:
        mendota = _catalogued("mendota-mental-health-institute")
        row = {"external_id": "88002183", "name": "Wisconsin Memorial Hospital Historic District", "status": "Listed"}
        stub = mock.Mock()
        stub.get.return_value = Answer(200, {"results": [row]}, 0.0)
        recorded: list[tuple[str, object]] = []

        endpoints.test_a_historic_register_lists_the_campus(stub, mendota, lambda *pair: recorded.append(pair))

        assert recorded == []

    def test_a_register_with_no_row_for_the_campus_fails(self) -> None:
        trenton = _catalogued("trenton-psychiatric-hospital")
        stub = mock.Mock()
        stub.get.return_value = Answer(200, {"results": [{"name": "Main Hospital Building", "attributes": {}}]}, 0.0)

        with pytest.raises(AssertionError, match="none of 1 cultural resources"):
            endpoints.test_a_historic_register_lists_the_campus(stub, trenton, lambda *pair: None)

    def test_what_a_passing_check_recorded_reaches_the_report(self) -> None:
        report = mock.Mock(user_properties=[("register", "matched only eligible, not listed, register rows: 1")])

        assert _load_conftest()._notes(report) == "register: matched only eligible, not listed, register rows: 1"
        assert _load_conftest()._notes(mock.Mock(user_properties=[])) == ""


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
            ({"error": "rate_limited", "message": "busy"}, {}),
            ({"error": "key_budget_exhausted", "message": "this key's share is spent"}, {}),
            ({"error": "source_error", "message": "upstream timed out"}, {}),
            ({"error": "anything"}, {"Retry-After": "2"}),
        ],
    )
    def test_a_503_that_says_ask_again_is_retried(
        self, client: LiveRedata, get: mock.Mock, body: dict[str, str], headers: dict[str, str]
    ) -> None:
        get.side_effect = [_response(503, body, headers), _response(200, {"results": []})]

        assert client.get("parcels/").status == 200

    @pytest.mark.parametrize("error", ["rate_limited", "key_budget_exhausted"])
    def test_a_spent_budget_whichever_it_was_is_inconclusive_not_an_answer(
        self, client: LiveRedata, get: mock.Mock, error: str
    ) -> None:
        client.max_wait_seconds = 0
        get.return_value = _response(503, {"error": error})

        with pytest.raises(InconclusiveError):
            client.get("places/")

    def test_still_busy_when_the_wait_runs_out_is_inconclusive(self, client: LiveRedata, get: mock.Mock) -> None:
        client.max_wait_seconds = 0
        get.return_value = _response(503, {"error": "source_error"})

        with pytest.raises(InconclusiveError):
            client.get("parcels/")

    def test_a_server_error_is_not_remembered(self, client: LiveRedata, get: mock.Mock) -> None:
        get.side_effect = [_response(500, {"error": "boom"}), _response(200, {"results": []})]

        assert client.get("parcels/").status == 500
        assert client.get("parcels/").status == 200

    def test_a_503_redata_decided_is_remembered(self, client: LiveRedata, get: mock.Mock) -> None:
        get.return_value = _response(503, {"error": "no_data_found", "message": "Every configured source ..."})

        first = client.get("parcels/lookup/", lat=1)

        assert first.status == 503
        assert client.get("parcels/lookup/", lat=1) is first
        assert get.call_count == 1

    def test_a_call_still_refused_when_its_wait_ran_out_is_not_asked_again(
        self, client: LiveRedata, get: mock.Mock
    ) -> None:
        client.max_wait_seconds = 0
        get.return_value = _response(502, "<html>Bad Gateway</html>")

        for _ in range(3):
            with pytest.raises(InconclusiveError, match="502"):
                client.get("parcels/1/buildings/")
        assert get.call_count == 1

    def test_an_answer_is_remembered(self, client: LiveRedata, get: mock.Mock) -> None:
        get.return_value = _response(200, {"results": []})

        first = client.get("parcels/", lat=1)

        assert client.get("parcels/", lat=1) is first
        assert isinstance(first, Answer)
        assert get.call_count == 1


class TestSettledReport:
    def test_a_failed_subtest_fails_the_pipeline_check(self) -> None:
        results = {
            "hrsh": {
                "pipeline": {"outcome": "passed", "detail": ""},
                "pipeline: building wikis": {"outcome": "failed", "detail": "3 of 20"},
                "pipeline: build date": {"outcome": "known issue", "detail": ""},
            }
        }

        assert settled(results)["hrsh"]["pipeline"]["outcome"] == "failed"

    def test_a_known_issue_alone_leaves_it_passed(self) -> None:
        results = {
            "athens": {
                "pipeline": {"outcome": "passed", "detail": ""},
                "pipeline: build date": {"outcome": "known issue", "detail": ""},
            }
        }

        assert settled(results)["athens"]["pipeline"]["outcome"] == "passed"


class TestUnansweredProviders:
    @pytest.mark.parametrize("status", ["rate_limited", "key_budget_exhausted", "unavailable", "not_cached"])
    def test_a_provider_that_was_not_heard_from_is_named(self, status: str) -> None:
        providers = [{"provider": "heard", "status": "ok"}, {"provider": "silent", "status": status}]

        answer = Answer(200, {"complete": False, "results": [], "providers": providers}, 0.1)

        assert answer.unanswered_providers == ["silent"]

    def test_a_provider_that_answered_is_not(self) -> None:
        answer = Answer(200, {"results": [], "providers": [{"provider": "heard", "status": "ok"}]}, 0.1)

        assert answer.unanswered_providers == []
