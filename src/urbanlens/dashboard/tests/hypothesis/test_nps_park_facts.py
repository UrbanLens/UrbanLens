"""The national-park panel reads the fields it has been caching all along."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.nps import (
    alert_facts,
    entrance_fee_summary,
    park_facts,
    standard_hours_summary,
)


def _hours(**overrides: str) -> list[dict]:
    week = dict.fromkeys(
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), "9:00AM - 5:00PM"
    )
    week.update(overrides)
    return [{"name": "Park Hours", "standardHours": week}]


class EntranceFeeTests(SimpleTestCase):
    def test_a_park_that_charges_nothing_says_so(self) -> None:
        """Most NPS units are free, and that is worth one word."""
        self.assertEqual(entrance_fee_summary([{"cost": "0.00", "title": "Entrance Fee - Free"}]), "Free")

    def test_a_single_fee_names_what_it_buys(self) -> None:
        self.assertEqual(
            entrance_fee_summary([{"cost": "35.00", "title": "Entrance Fee - Private Vehicle"}]),
            "$35.00 (Private Vehicle)",
        )

    def test_several_fees_report_the_cheapest_as_a_floor(self) -> None:
        fees = [
            {"cost": "35.00", "title": "Entrance Fee - Private Vehicle"},
            {"cost": "20.00", "title": "Entrance Fee - Per Person"},
            {"cost": "30.00", "title": "Entrance Fee - Motorcycle"},
        ]

        self.assertEqual(entrance_fee_summary(fees), "From $20.00 (Per Person)")

    def test_a_free_entry_among_paid_ones_is_not_reported_as_free(self) -> None:
        """ "Free" must mean free, not "one of the five options is"."""
        fees = [
            {"cost": "0.00", "title": "Entrance Fee - Under 16"},
            {"cost": "35.00", "title": "Entrance Fee - Private Vehicle"},
        ]

        self.assertEqual(entrance_fee_summary(fees), "From $0.00 (Under 16)")

    def test_no_published_fees_is_not_free(self) -> None:
        """A unit whose fees NPS has not published must not be advertised as costing nothing."""
        self.assertEqual(entrance_fee_summary([]), "")
        self.assertEqual(entrance_fee_summary(None), "")

    def test_an_unparseable_cost_is_skipped_not_guessed(self) -> None:
        self.assertEqual(entrance_fee_summary([{"cost": "varies", "title": "Entrance Fee - Group"}]), "")
        self.assertEqual(
            entrance_fee_summary([{"cost": "varies"}, {"cost": "10.00", "title": "Entrance Fee - Per Person"}]),
            "$10.00 (Per Person)",
        )

    def test_malformed_rows_do_not_raise(self) -> None:
        self.assertEqual(entrance_fee_summary(["not a dict", None, 7]), "")

    def test_a_fee_with_no_title_still_reports_its_cost(self) -> None:
        self.assertEqual(entrance_fee_summary([{"cost": "15.00"}]), "$15.00")


class StandardHoursTests(SimpleTestCase):
    def test_a_uniform_week_reads_as_one_line(self) -> None:
        self.assertEqual(standard_hours_summary(_hours()), "9:00AM - 5:00PM daily")

    def test_a_weekend_difference_is_grouped_not_listed_seven_times(self) -> None:
        summary = standard_hours_summary(_hours(saturday="Closed", sunday="Closed"))

        self.assertEqual(summary, "Mon-Fri: 9:00AM - 5:00PM; Sat-Sun: Closed")

    def test_a_single_odd_day_is_named_alone(self) -> None:
        summary = standard_hours_summary(_hours(wednesday="Closed"))

        self.assertEqual(summary, "Mon-Tue: 9:00AM - 5:00PM; Wed: Closed; Thu-Sun: 9:00AM - 5:00PM")

    def test_an_always_open_park_is_reported_verbatim(self) -> None:
        """ "All Day" is NPS's own wording for a park with no gate."""
        week = dict.fromkeys(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), "All Day")

        self.assertEqual(standard_hours_summary([{"standardHours": week}]), "All Day daily")

    def test_a_partially_published_week_says_nothing(self) -> None:
        """Collapsing an unknown day into a range would read as "closed that day"."""
        partial = _hours()
        del partial[0]["standardHours"]["thursday"]

        self.assertEqual(standard_hours_summary(partial), "")

    def test_only_the_parks_own_hours_are_read(self) -> None:
        """Later entries are individual visitor centres, not the unit."""
        entries = [
            *_hours(),
            {
                "name": "Visitor Center",
                "standardHours": dict.fromkeys(
                    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), "10:00AM - 4:00PM"
                ),
            },
        ]

        self.assertEqual(standard_hours_summary(entries), "9:00AM - 5:00PM daily")

    def test_malformed_input_does_not_raise(self) -> None:
        self.assertEqual(standard_hours_summary(None), "")
        self.assertEqual(standard_hours_summary([]), "")
        self.assertEqual(standard_hours_summary(["not a dict"]), "")
        self.assertEqual(standard_hours_summary([{"name": "Park Hours"}]), "")


class AlertFactsTests(SimpleTestCase):
    def test_an_active_danger_alert_shows_up(self) -> None:
        """The safety-critical case: a closure or hazard must actually surface."""
        alerts = [
            {
                "id": 1,
                "title": "Bridge out on North Rim Road",
                "category": "Danger",
                "url": "https://www.nps.gov/grca/alert1",
            }
        ]

        facts = alert_facts({"alerts": alerts}, show_facility_facets=True)

        self.assertEqual(
            facts,
            [
                {
                    "icon": "warning",
                    "text": "Danger: Bridge out on North Rim Road",
                    "href": "https://www.nps.gov/grca/alert1",
                }
            ],
        )

    def test_zero_alerts_renders_cleanly(self) -> None:
        self.assertEqual(alert_facts({"alerts": []}, show_facility_facets=True), [])

    def test_a_missing_alerts_key_renders_cleanly(self) -> None:
        self.assertEqual(alert_facts({}, show_facility_facets=True), [])

    def test_a_category_less_alert_still_shows_its_title(self) -> None:
        facts = alert_facts({"alerts": [{"title": "Seasonal road closure"}]}, show_facility_facets=True)

        self.assertEqual(facts, [{"icon": "warning", "text": "Seasonal road closure"}])

    def test_an_alert_with_no_url_omits_href(self) -> None:
        facts = alert_facts(
            {"alerts": [{"title": "Fire danger: extreme", "category": "Caution"}]}, show_facility_facets=True
        )

        self.assertNotIn("href", facts[0])

    def test_a_titleless_alert_is_skipped(self) -> None:
        self.assertEqual(alert_facts({"alerts": [{"category": "Information"}]}, show_facility_facets=True), [])

    def test_malformed_rows_do_not_raise(self) -> None:
        self.assertEqual(alert_facts({"alerts": ["not a dict", None, 7]}, show_facility_facets=True), [])

    def test_malformed_alerts_value_does_not_raise(self) -> None:
        self.assertEqual(alert_facts({"alerts": "not a list"}, show_facility_facets=True), [])
        self.assertEqual(alert_facts({"alerts": None}, show_facility_facets=True), [])

    def test_alerts_beyond_the_cap_are_dropped(self) -> None:
        alerts = [{"title": f"Alert {i}", "category": "Information"} for i in range(20)]

        facts = alert_facts({"alerts": alerts}, show_facility_facets=True)

        self.assertEqual(len(facts), 8)
        self.assertEqual(facts[0]["text"], "Information: Alert 0")

    def test_multiple_categories_preserve_redatas_own_order(self) -> None:
        """No severity ranking is invented here - REData's own order is shown as-is."""
        alerts = [
            {"title": "Fee waived today", "category": "Information"},
            {"title": "Entrance closed", "category": "Park Closure"},
        ]

        facts = alert_facts({"alerts": alerts}, show_facility_facets=True)

        self.assertEqual(
            [fact["text"] for fact in facts], ["Information: Fee waived today", "Park Closure: Entrance closed"]
        )


class ParkFactsTests(SimpleTestCase):
    def _facts(self, *, show_facility_facets: bool = True, **data) -> dict[str, str]:
        return {row["label"]: row["value"] for row in park_facts(data, show_facility_facets=show_facility_facets)}

    def test_the_previously_unread_fields_are_shown(self) -> None:
        facts = self._facts(
            designation="National Historic Site",
            states="AZ",
            entrance_fees=[{"cost": "0.00", "title": "Entrance Fee - Free"}],
            operating_hours=_hours(),
            directions_url="https://www.nps.gov/hutr/directions.htm",
            park_code="HUTR",
        )

        self.assertEqual(facts["Entry"], "Free")
        self.assertEqual(facts["Hours"], "9:00AM - 5:00PM daily")
        self.assertEqual(facts["Directions"], "Getting there")

    def test_the_directions_row_carries_the_link(self) -> None:
        rows = park_facts({"directions_url": "https://www.nps.gov/hutr/directions.htm"}, show_facility_facets=True)

        self.assertEqual(
            rows, [{"label": "Directions", "value": "Getting there", "href": "https://www.nps.gov/hutr/directions.htm"}]
        )

    def test_hours_come_before_the_cross_reference_code(self) -> None:
        """Reading order is by usefulness; "HUTR" is a cross-reference, not a fact about visiting."""
        labels = [
            row["label"]
            for row in park_facts({"operating_hours": _hours(), "park_code": "HUTR"}, show_facility_facets=True)
        ]

        self.assertEqual(labels, ["Hours", "Park Code"])

    def test_a_sparse_park_yields_only_what_it_publishes(self) -> None:
        self.assertEqual(park_facts({"full_name": "Somewhere"}, show_facility_facets=True), [])

    def test_weather_prose_is_deliberately_not_a_fact_row(self) -> None:
        """The pin has a weather panel with the actual forecast; this is seasonal prose."""
        rows = park_facts(
            {"weather_info": "Summers are hot and dry. Winters bring occasional snow."}, show_facility_facets=True
        )

        self.assertEqual(rows, [])

    def test_visitor_centers_and_campgrounds_summarize_as_count_and_names(self) -> None:
        facts = self._facts(
            visitor_centers=[{"name": "Old Faithful Visitor Center"}, {"name": "Canyon Visitor Education Center"}],
            campgrounds=[{"name": "Madison Campground"}],
        )

        self.assertEqual(facts["Visitor Centers"], "2 (Old Faithful Visitor Center, Canyon Visitor Education Center)")
        self.assertEqual(facts["Campgrounds"], "1 (Madison Campground)")

    def test_a_long_facility_list_collapses_the_tail_into_a_count(self) -> None:
        campgrounds = [{"name": f"Campground {i}"} for i in range(8)]

        facts = self._facts(campgrounds=campgrounds)

        self.assertEqual(
            facts["Campgrounds"],
            "8 (Campground 0, Campground 1, Campground 2, Campground 3, Campground 4, +3 more)",
        )

    def test_no_visitor_centers_or_campgrounds_omits_the_rows(self) -> None:
        rows = park_facts({"visitor_centers": [], "campgrounds": []}, show_facility_facets=True)

        self.assertEqual(rows, [])

    def test_facility_rows_come_after_hours_and_before_directions(self) -> None:
        labels = [
            row["label"]
            for row in park_facts(
                {
                    "operating_hours": _hours(),
                    "visitor_centers": [{"name": "Old Faithful Visitor Center"}],
                    "campgrounds": [{"name": "Madison Campground"}],
                    "directions_url": "https://www.nps.gov/yell/directions.htm",
                },
                show_facility_facets=True,
            )
        ]

        self.assertEqual(labels, ["Hours", "Visitor Centers", "Campgrounds", "Directions"])


class FacilityFacetsGateTests(SimpleTestCase):
    """``show_facility_facets=False`` hides alerts and visitor-centers/campgrounds - see P9's subscription-gating decision (2026-09-08): this pin is merely near the park, not inside it, so this section is nearby-area data, not data about the pin's own place."""

    def test_alerts_are_hidden_when_facets_are_not_visible(self) -> None:
        alerts = [{"title": "Bridge out", "category": "Danger", "url": "https://nps.gov/x/alert1"}]

        self.assertEqual(alert_facts({"alerts": alerts}, show_facility_facets=False), [])

    def test_alerts_are_not_even_read_when_facets_are_not_visible(self) -> None:
        """Malformed data in a section that will not render must not raise."""
        self.assertEqual(alert_facts({"alerts": "not a list, and it does not matter"}, show_facility_facets=False), [])

    def test_visitor_centers_and_campgrounds_are_hidden_when_facets_are_not_visible(self) -> None:
        rows = park_facts(
            {
                "visitor_centers": [{"name": "Old Faithful Visitor Center"}],
                "campgrounds": [{"name": "Madison Campground"}],
            },
            show_facility_facets=False,
        )

        self.assertEqual(rows, [])

    def test_routine_facts_stay_free_even_when_facility_facets_are_not_visible(self) -> None:
        """Designation/hours/entry/directions are the pre-existing free base card - unaffected."""
        rows = park_facts(
            {
                "designation": "National Historic Site",
                "operating_hours": _hours(),
                "entrance_fees": [{"cost": "0.00", "title": "Entrance Fee - Free"}],
                "directions_url": "https://www.nps.gov/hutr/directions.htm",
                "visitor_centers": [{"name": "Old Faithful Visitor Center"}],
            },
            show_facility_facets=False,
        )

        labels = [row["label"] for row in rows]
        self.assertEqual(labels, ["Designation", "Entry", "Hours", "Directions"])
        self.assertNotIn("Visitor Centers", labels)
