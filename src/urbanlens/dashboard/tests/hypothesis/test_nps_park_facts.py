"""The national-park panel reads the fields it has been caching all along.

REData's NPS catalog carries entrance fees, published operating hours, a
directions page and seasonal weather prose for every unit, and UrbanLens cached
all of it and displayed none. The hours case was the sharpest: the template
rendered "Standard hours vary - check NPS.gov" *whenever `standardHours` was
present* - that is, precisely when it did not have to say that.

For this app's subject the two that matter are "what does it cost to get in"
and "when is it open". Both are answered here, from NPS's own shapes: `cost` is
a string even for free entry, and `standardHours` is a seven-key mapping whose
values are free text ("9:00AM - 5:00PM", "All Day", "Closed").
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.builtin.nps import entrance_fee_summary, park_facts, standard_hours_summary


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


class ParkFactsTests(SimpleTestCase):
    def _facts(self, **data) -> dict[str, str]:
        return {row["label"]: row["value"] for row in park_facts(data)}

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
        rows = park_facts({"directions_url": "https://www.nps.gov/hutr/directions.htm"})

        self.assertEqual(
            rows, [{"label": "Directions", "value": "Getting there", "href": "https://www.nps.gov/hutr/directions.htm"}]
        )

    def test_hours_come_before_the_cross_reference_code(self) -> None:
        """Reading order is by usefulness; "HUTR" is a cross-reference, not a fact about visiting."""
        labels = [row["label"] for row in park_facts({"operating_hours": _hours(), "park_code": "HUTR"})]

        self.assertEqual(labels, ["Hours", "Park Code"])

    def test_a_sparse_park_yields_only_what_it_publishes(self) -> None:
        self.assertEqual(park_facts({"full_name": "Somewhere"}), [])

    def test_weather_prose_is_deliberately_not_a_fact_row(self) -> None:
        """The pin has a weather panel with the actual forecast; this is seasonal prose."""
        rows = park_facts({"weather_info": "Summers are hot and dry. Winters bring occasional snow."})

        self.assertEqual(rows, [])
