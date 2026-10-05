"""Only crime counts as crime on the incident panels.

REData's ``/incidents/`` vocabulary carries fire and EMS responses (``fire``, ``medical``) beside police categories.
Counted, a block next to a fire station read as the most dangerous in town.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.redata_incidents import IncidentHistoryPanelSource, PoliceIncidentsPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import (
    CRIME_INCIDENT_CATEGORIES,
    INCIDENT_CATEGORY_LABELS,
)

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_incidents_gateway.RedataIncidentsGateway"


def _pin():
    location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


def _rows(*categories: str) -> list[dict]:
    return [
        {"category": category, "occurred_at": f"2026-01-{index + 1:02d}"} for index, category in enumerate(categories)
    ]


class CrimeVocabularyTests(TestCase):
    def test_fire_and_ems_responses_are_not_crime(self) -> None:
        self.assertNotIn("fire", CRIME_INCIDENT_CATEGORIES)
        self.assertNotIn("medical", CRIME_INCIDENT_CATEGORIES)
        self.assertNotIn("traffic", CRIME_INCIDENT_CATEGORIES)

    def test_every_crime_category_has_a_label(self) -> None:
        self.assertLessEqual(CRIME_INCIDENT_CATEGORIES, set(INCIDENT_CATEGORY_LABELS))

    def test_fire_and_ems_have_their_own_labels(self) -> None:
        self.assertNotEqual(INCIDENT_CATEGORY_LABELS.get("fire", "Other"), "Other")
        self.assertNotEqual(INCIDENT_CATEGORY_LABELS.get("medical", "Other"), "Other")


class IncidentPanelsCountCrimeOnlyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin = _pin()

    def test_the_recent_panel_counts_crime_only(self) -> None:
        context = PoliceIncidentsPanelSource().render_context(
            self.pin, {"incidents": _rows("fire", "medical", "medical", "burglary")}
        )

        assert context is not None
        self.assertTrue(context["chips"][0].startswith("1 "), context["chips"])
        self.assertFalse(any("Fire" in row["value"] or "Medical" in row["value"] for row in context["meta"]))

    def test_the_history_panel_counts_crime_only(self) -> None:
        context = IncidentHistoryPanelSource().render_context(
            self.pin, {"incidents": _rows("fire", "theft", "medical")}
        )

        assert context is not None
        self.assertTrue(context["chips"][0].startswith("1 "), context["chips"])

    def test_fire_and_ems_alone_hide_the_panels(self) -> None:
        for source in (PoliceIncidentsPanelSource(), IncidentHistoryPanelSource()):
            with self.subTest(source=source.key):
                self.assertIsNone(source.render_context(self.pin, {"incidents": _rows("fire", "medical")}))

    def test_both_panels_ask_redata_for_crime_only(self) -> None:
        """Filtered upstream, ``limit`` bounds crime rows rather than whatever the fire service logged most recently."""
        for source in (PoliceIncidentsPanelSource(), IncidentHistoryPanelSource()):
            with self.subTest(source=source.key), mock.patch(_GATEWAY) as gateway_cls:
                gateway_cls.return_value.get_incidents.return_value = LocationContextEnvelope(
                    count=0, complete=True, results=[]
                )
                source.fetch_envelope(40.5, -74.5)

                _, kwargs = gateway_cls.return_value.get_incidents.call_args
                self.assertEqual(set(kwargs["categories"]), set(CRIME_INCIDENT_CATEGORIES))

    def test_a_full_page_is_not_presented_as_the_total(self) -> None:
        source = PoliceIncidentsPanelSource()
        context = source.render_context(self.pin, {"incidents": _rows(*["theft"] * source.row_limit)})

        assert context is not None
        self.assertTrue(context["chips"][0].startswith(f"{source.row_limit}+ "), context["chips"])
