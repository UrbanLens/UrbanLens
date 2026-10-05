"""A root pin on a notable property gets its parcel, buildings, child pins, child wikis and site content without a visit.

Before, pin creation brought only the parcel and the wiki; the buildings, their pins and wikis and every site panel
waited for somebody to open the pin page, or for the hourly enrichment job and its cap of ten locations per run.

REData and Overpass are answered at the HTTP boundary (``hrsh_upstreams``), so the REData counts here are requests.
"""

from __future__ import annotations

from datetime import date
import json
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry
from django.core.cache import cache
from django.db import transaction
from django.template.loader import render_to_string
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.facts.model import FactEvidence
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.core.locks import acquire_lock
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins import bootstrap
from urbanlens.dashboard.services.pins.external_data import (
    gate_allows,
    get_panel_source,
    panel_readiness,
    schedule_panel_fetch,
)
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.pins.pin_restructure import enclosing_parcel
from urbanlens.dashboard.tests.hypothesis.building_fixtures import CAMPUS_LAT, CAMPUS_LNG, offset, parcel_square
from urbanlens.dashboard.tests.hypothesis.hrsh_upstreams import (
    ASSESSOR_YEAR,
    GARAGE_YEAR,
    LAUNDRY_YEAR,
    MAIN_YEAR,
    ON_PROPERTY_BUILDINGS,
    PARCEL_HALF_SIDE_M,
    HrshUpstreams,
    campus_buildings,
)
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin
from urbanlens.UrbanLens.settings.app import settings as app_settings

#: Everything a pin's creation sets off on its own: the bootstrap, the panel fetches it schedules, and the wiki work
#: the pin's save queues. The enqueues left out (Wikipedia/NPS prefetch, web search, CRIS's delayed fill passes, the
#: wiki's place-name enrichment) are recorded rather than run.
AUTOMATIC = (
    tasks.bootstrap_location,
    tasks.fetch_panel_source,
    tasks.ensure_wiki_for_location,
    tasks.ensure_wikis_for_locations,
    tasks.ensure_building_wikis,
)

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"

#: REData requests one bootstrap of the campus makes, whatever its building count: the structural four (prewarm,
#: parcel lookup, its boundaries, its buildings); property records' nine parcel sub-resources; CRIS's lookup and one
#: detail; the register lookup and the capability index; news twice (the shared search and the pin's own name); web
#: images; incidents; and the four archives REData searches. Twelve of them draw on REData's 1,000/hour lookup pool.
REDATA_CALLS_PER_BOOTSTRAP = 25


def _year(value: int) -> date:
    return date(value, 1, 1)


class _BootstrapCase(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the first-user site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.upstreams = HrshUpstreams()
        # New York's outline comes from Census TIGERweb, which is not part of this fake.
        patcher = mock.patch.object(
            CrisBuildingPanelSource, "geo_boundary", GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def create_pin(
        self, *, latitude: float = CAMPUS_LAT, longitude: float = CAMPUS_LNG, profile=None, inline=AUTOMATIC, **fields
    ) -> Pin:
        with (
            self.upstreams.serving(),
            tasks_run_inline(*inline) as self.enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            pin = create_pin_for_profile(
                profile or self.profile,
                name="Hudson River State Hospital",
                latitude=latitude,
                longitude=longitude,
                **fields,
            ).pin
        return Pin.objects.select_related("location__place", "profile").get(pk=pin.pk)

    def enqueued(self, task) -> list:
        return [call for call in self.enqueue.call_args_list if call.args and call.args[0] is task]

    def children(self, pin: Pin) -> list[Pin]:
        return list(pin.descendants().select_related("location__place"))


class CreationBootstrapsTheCampusTests(_BootstrapCase):
    """The end-to-end property: one create call, no page visit, the whole campus."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = self.create_pin()
        self.location = self.pin.location

    def test_the_top_parent_stands_on_the_parcel_redata_drew(self) -> None:
        parcel = enclosing_parcel(self.location.place)

        self.assertIsNotNone(parcel)
        self.assertEqual(parcel.kind, PlaceKind.PARCEL)
        expected = GEOSGeometry(json.dumps(parcel_square(PARCEL_HALF_SIDE_M)), srid=4326)
        self.assertAlmostEqual(parcel.geometry.area / expected.area, 1.0, places=2)
        self.assertEqual(Wiki.objects.get(location=self.location).place_id, parcel.pk)

    def test_every_building_on_the_property_gets_a_child_pin_on_its_own_footprint(self) -> None:
        children = self.children(self.pin)

        self.assertEqual(len(children), ON_PROPERTY_BUILDINGS)
        parcel = enclosing_parcel(self.location.place)
        for child in children:
            self.assertEqual(child.pin_type, PinType.BUILDING)
            place = child.location.place
            self.assertIsNotNone(place, f"{child} stands on no place")
            self.assertEqual(place.kind, PlaceKind.BUILDING)
            self.assertIsNotNone(place.geometry, f"{child}'s building has no footprint")
            self.assertEqual(enclosing_parcel(place).pk, parcel.pk)

    def test_each_child_pin_stands_on_a_child_wiki_nested_under_the_campus_wiki(self) -> None:
        campus_wiki = Wiki.objects.get(location=self.location)

        for child in self.children(self.pin):
            wiki = Wiki.objects.get_for_location(child.location)
            self.assertIsNotNone(wiki, f"{child} has no wiki")
            self.assertEqual(wiki.parent_wiki_id, campus_wiki.pk)
            self.assertEqual(wiki.place_id, child.location.place_id)

    def test_build_dates_come_from_the_building_records_where_they_have_one(self) -> None:
        dates = {child.name: child.date_built for child in self.children(self.pin)}

        self.assertEqual(dates["MAIN/ADMIN"], _year(MAIN_YEAR))
        self.assertEqual(dates["LAUNDRY"], _year(LAUNDRY_YEAR))
        self.assertEqual(dates["GARAGE"], _year(GARAGE_YEAR))
        self.assertIsNone(dates["CATHOLIC CHAPEL"])

    def test_the_root_pin_takes_the_year_of_the_building_it_stands_on(self) -> None:
        self.assertEqual(self.pin.date_built, _year(MAIN_YEAR))

    def test_the_wikis_carry_the_build_year_too(self) -> None:
        from urbanlens.dashboard.services.pins.build_dates import wiki_build_year

        campus_wiki = Wiki.objects.get(location=self.location)
        self.assertEqual(wiki_build_year(campus_wiki), MAIN_YEAR)
        laundry = Wiki.objects.get_for_location(Pin.objects.get(name="LAUNDRY").location)
        self.assertEqual(wiki_build_year(laundry), LAUNDRY_YEAR)

    def test_the_site_panels_are_cached(self) -> None:
        sources = [get_panel_source(key) for key in bootstrap.SITE_PANEL_KEYS]
        applicable = [source for source in sources if source is not None and gate_allows(source, self.pin)]

        readiness = panel_readiness(self.pin, applicable, require_content=False)

        self.assertTrue(
            {"property_records", "redata_historic_registers", "cris_building", "gdelt", "redata_incidents"}
            <= set(readiness)
        )
        self.assertEqual([key for key, ready in readiness.items() if not ready], [])
        self.assertTrue(
            panel_readiness(self.pin, [get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)])[PARCEL_BUILDINGS_CACHE_SOURCE]
        )

    def test_redata_is_asked_to_prewarm_the_point_once(self) -> None:
        self.assertEqual(self.upstreams.count("/locations/prewarm/", "POST"), 1)

    def test_a_page_visit_afterwards_fetches_nothing(self) -> None:
        before = self.upstreams.redata_total
        with self.upstreams.serving(), tasks_run_inline(tasks.fetch_panel_source) as enqueue:
            for key in (*bootstrap.SITE_PANEL_KEYS, PARCEL_BUILDINGS_CACHE_SOURCE, "boundary"):
                source = get_panel_source(key)
                if source is not None and gate_allows(source, self.pin) and not source.has_landed(self.pin):
                    schedule_panel_fetch(key, self.pin)

        self.assertEqual(self.upstreams.redata_total, before)
        self.assertFalse([call for call in enqueue.call_args_list if call.args[0] is tasks.fetch_panel_source])


class BuildingKnownOnlyToRedataTests(_BootstrapCase):
    """OSM does not map the building the pin stands on: the pin moves onto it only once REData's list names it."""

    def setUp(self) -> None:
        super().setUp()
        self.upstreams = HrshUpstreams(osm_main_building=False)
        self.pin = self.create_pin()

    def test_the_pin_ends_on_its_building_and_its_property_keeps_its_building_list(self) -> None:
        self.assertEqual(self.pin.location.place.kind, PlaceKind.BUILDING)
        self.assertTrue(
            panel_readiness(self.pin, [get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)])[PARCEL_BUILDINGS_CACHE_SOURCE]
        )
        self.assertEqual(len(self.children(self.pin)), ON_PROPERTY_BUILDINGS)
        self.assertEqual(self.pin.date_built, _year(MAIN_YEAR))


class RedataBudgetTests(_BootstrapCase):
    """P144: one REData key, 1,000 lookups an hour, shared by every environment."""

    def assert_structural_calls_once(self) -> None:
        self.assertEqual(self.upstreams.count("/locations/prewarm/", "POST"), 1)
        self.assertEqual(self.upstreams.count("/parcels/lookup/"), 1)
        self.assertEqual(self.upstreams.count("/parcels/{id}/boundaries/"), 1)
        self.assertEqual(self.upstreams.count("/parcels/{id}/buildings/"), 1)

    def test_a_bootstrap_costs_a_fixed_number_of_redata_requests(self) -> None:
        self.create_pin()

        self.assert_structural_calls_once()
        self.assertEqual(self.upstreams.redata_total, REDATA_CALLS_PER_BOOTSTRAP, dict(self.upstreams.calls))

    def test_a_campus_twice_the_size_costs_the_same(self) -> None:
        self.upstreams = HrshUpstreams(buildings=campus_buildings(extra=8))

        pin = self.create_pin()

        self.assertEqual(len(self.children(pin)), ON_PROPERTY_BUILDINGS + 8)
        self.assert_structural_calls_once()
        self.assertEqual(self.upstreams.redata_total, REDATA_CALLS_PER_BOOTSTRAP, dict(self.upstreams.calls))

    def test_the_building_wikis_queue_no_enrichment_of_their_own(self) -> None:
        """A mirrored building wiki already stands on its building, so it needs no place-name or boundary lookup."""
        pin = self.create_pin()
        campus_wiki = Wiki.objects.get(location=pin.location)

        enriched = {call.args[1] for call in self.enqueued(tasks.enrich_wiki_location)}

        self.assertTrue(Wiki.objects.filter(parent_wiki=campus_wiki).exists())
        self.assertLessEqual(enriched, {campus_wiki.pk})
        for location in Location.objects.filter(wiki__parent_wiki=campus_wiki):
            self.assertIsNotNone(location.place_resolved_at)

    def test_a_second_pin_on_the_campus_costs_no_structural_request(self) -> None:
        self.create_pin()
        other = baker.make(User).profile
        before = dict(self.upstreams.calls)

        self.create_pin(profile=other)

        for path in (
            "/locations/prewarm/",
            "/parcels/lookup/",
            "/parcels/{id}/boundaries/",
            "/parcels/{id}/buildings/",
        ):
            method = "POST" if "prewarm" in path else "GET"
            self.assertEqual(
                self.upstreams.count(path, method),
                sum(n for (verb, p), n in before.items() if verb == method and path in p),
                path,
            )


class PrewarmScopeTests(_BootstrapCase):
    def test_a_key_without_the_prewarm_scope_is_not_an_error_and_is_not_asked_again(self) -> None:
        self.upstreams = HrshUpstreams(prewarm_status=403)

        with self.assertNoLogs("urbanlens", level="ERROR"):
            pin = self.create_pin()
        self.create_pin(profile=baker.make(User).profile, latitude=offset(-60, 40)[0], longitude=offset(-60, 40)[1])

        self.assertEqual(self.upstreams.count("/locations/prewarm/", "POST"), 1)
        self.assertEqual(len(self.children(pin)), ON_PROPERTY_BUILDINGS)

    def assert_bootstraps_despite_prewarm(self, status: int) -> None:
        self.upstreams = HrshUpstreams(prewarm_status=status)

        with self.assertNoLogs("urbanlens", level="ERROR"):
            pin = self.create_pin()

        self.assertIsNotNone(enclosing_parcel(pin.location.place))
        self.assertEqual(len(self.children(pin)), ON_PROPERTY_BUILDINGS)

    def test_a_throttled_prewarm_still_bootstraps_the_campus(self) -> None:
        self.assert_bootstraps_despite_prewarm(429)

    def test_a_prewarm_redata_cannot_take_still_bootstraps_the_campus(self) -> None:
        self.assert_bootstraps_despite_prewarm(503)

    def test_prewarm_is_not_asked_twice_for_one_location(self) -> None:
        self.create_pin()
        self.create_pin(profile=baker.make(User).profile)

        self.assertEqual(self.upstreams.count("/locations/prewarm/", "POST"), 1)


class WithoutRedataTests(_BootstrapCase):
    """Open-source installs: no REData. The chain still draws the parcel and finds the buildings, from OpenStreetMap."""

    def setUp(self) -> None:
        super().setUp()
        for attribute in ("redata_api_url", "redata_api_key"):
            patcher = mock.patch.object(app_settings, attribute, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_parcel_and_its_buildings_still_arrive_without_errors(self) -> None:
        with self.assertNoLogs("urbanlens", level="ERROR"):
            pin = self.create_pin(inline=(tasks.bootstrap_location, tasks.fetch_panel_source))

        self.assertEqual(self.upstreams.redata_total, 0)
        parcel = enclosing_parcel(pin.location.place)
        self.assertIsNotNone(parcel)
        self.assertEqual(parcel.kind, PlaceKind.PARCEL)
        children = self.children(pin)
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.location.place.geometry is not None for child in children))


class ProfileGateTests(_BootstrapCase):
    def test_no_external_apis_means_no_bootstrap(self) -> None:
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])

        pin = self.create_pin(inline=())

        self.assertFalse(self.enqueued(tasks.bootstrap_location))
        self.assertTrue(self.enqueued(tasks.auto_nest_building_pins), "buildings another user fetched still nest")
        self.assertEqual(self.upstreams.redata_total, 0)
        self.assertEqual(self.children(pin), [])

    def test_no_community_means_building_pins_without_wikis(self) -> None:
        self.profile.community_enabled = False
        self.profile.save(update_fields=["community_enabled"])

        pin = self.create_pin()

        self.assertEqual(len(self.children(pin)), ON_PROPERTY_BUILDINGS)
        self.assertFalse(Wiki.objects.exists())

    def test_no_auto_building_pins_still_draws_the_parcel_and_fetches_the_site(self) -> None:
        self.profile.auto_create_building_pins = False
        self.profile.save(update_fields=["auto_create_building_pins"])

        pin = self.create_pin()

        self.assertEqual(self.children(pin), [])
        self.assertIsNotNone(enclosing_parcel(pin.location.place))
        self.assertTrue(panel_readiness(pin, [get_panel_source("property_records")])["property_records"])

    def test_a_child_pin_is_not_bootstrapped(self) -> None:
        pin = self.create_pin()

        with tasks_run_inline() as enqueue, self.captureOnCommitCallbacks(execute=True):
            create_pin_for_profile(
                self.profile, name="Door", latitude=offset(5, 5)[0], longitude=offset(5, 5)[1], parent_id=pin.uuid
            )

        self.assertFalse([call for call in enqueue.call_args_list if call.args[0] is tasks.bootstrap_location])


class BulkCreationTrickleTests(_BootstrapCase):
    """A client creating root pins in a loop gets a trickle, not one bootstrap per pin against the shared budget."""

    def test_past_the_hourly_allowance_a_root_pin_waits_for_backfill(self) -> None:
        bootstrapped = []
        for index in range(bootstrap.BOOTSTRAP_BURST + 1):
            latitude, longitude = offset(2000 + index * 600, 0)
            self.create_pin(latitude=latitude, longitude=longitude, inline=())
            bootstrapped.append(bool(self.enqueued(tasks.bootstrap_location)))

        self.assertEqual(bootstrapped, [True] * bootstrap.BOOTSTRAP_BURST + [False])
        self.assertTrue(self.enqueued(tasks.auto_nest_building_pins), "the pin still nests buildings already known")

    def test_many_accounts_together_cannot_spend_more_than_the_site_allowance(self) -> None:
        bootstrapped = []
        for index in range(bootstrap.SITE_BOOTSTRAP_BURST + 1):
            latitude, longitude = offset(2000 + index * 600, 0)
            profile = baker.make(User).profile
            self.create_pin(latitude=latitude, longitude=longitude, profile=profile, inline=())
            bootstrapped.append(bool(self.enqueued(tasks.bootstrap_location)))

        self.assertEqual(bootstrapped, [True] * bootstrap.SITE_BOOTSTRAP_BURST + [False])

    def test_one_account_past_its_allowance_leaves_the_site_allowance_to_others(self) -> None:
        for index in range(bootstrap.BOOTSTRAP_BURST + 3):
            latitude, longitude = offset(2000 + index * 600, 0)
            self.create_pin(latitude=latitude, longitude=longitude, inline=())

        others = bootstrap.SITE_BOOTSTRAP_BURST - bootstrap.BOOTSTRAP_BURST
        bootstrapped = []
        for index in range(others):
            latitude, longitude = offset(-2000 - index * 600, 0)
            self.create_pin(latitude=latitude, longitude=longitude, profile=baker.make(User).profile, inline=())
            bootstrapped.append(bool(self.enqueued(tasks.bootstrap_location)))

        self.assertEqual(bootstrapped, [True] * others)

    def test_imports_never_bootstrap(self) -> None:
        with tasks_run_inline() as enqueue, self.captureOnCommitCallbacks(execute=True):
            Pin.objects.get_nearby_or_create(CAMPUS_LAT, CAMPUS_LNG, self.profile, defaults={"name": "Imported"})

        self.assertFalse([call for call in enqueue.call_args_list if call.args[0] is tasks.bootstrap_location])


class RolledBackCreationTests(_BootstrapCase):
    def test_a_creation_that_rolls_back_holds_no_location(self) -> None:
        class _AbortError(Exception):
            pass

        def create_then_roll_back() -> None:
            with transaction.atomic():
                pin = create_pin_for_profile(self.profile, name="HRSH", latitude=CAMPUS_LAT, longitude=CAMPUS_LNG).pin
                raise _AbortError(pin.location_id)

        with self.upstreams.serving(), tasks_run_inline(), self.assertRaises(_AbortError) as aborted:
            create_then_roll_back()

        self.assertEqual(bootstrap.locations_bootstrapping([aborted.exception.args[0]]), set())


class InFlightDeduplicationTests(_BootstrapCase):
    """A page opened while the bootstrap runs shares its fetches, and the bootstrap shares the page's."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = self.create_pin(inline=())

    def run_stage(self, stage: str, **kwargs) -> None:
        with self.upstreams.serving(), tasks_run_inline() as self.enqueue:
            tasks.bootstrap_location(self.pin.pk, stage=stage, **kwargs)

    def test_a_pin_no_longer_eligible_releases_its_location(self) -> None:
        """Otherwise every enrichment source skips the location for the marker's hour."""
        self.assertEqual(bootstrap.locations_bootstrapping([self.pin.location_id]), {self.pin.location_id})
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=baker.make(Pin, profile=self.profile))

        self.run_stage(bootstrap.BootstrapStage.BUILDINGS)

        self.assertEqual(bootstrap.locations_bootstrapping([self.pin.location_id]), set())

    def test_a_buildings_fetch_already_in_flight_is_waited_for_not_repeated(self) -> None:
        source = get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)
        acquire_lock(source.flight_key(self.pin), 150)

        self.run_stage(bootstrap.BootstrapStage.BUILDINGS)

        self.assertEqual(self.upstreams.count("/parcels/{id}/buildings/"), 0)
        retry = self.enqueued(tasks.bootstrap_location)
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0].kwargs["stage"], bootstrap.BootstrapStage.BUILDINGS)
        self.assertGreater(retry[0].kwargs["countdown"], 0)

    def test_after_enough_waiting_the_chain_moves_on(self) -> None:
        source = get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)
        acquire_lock(source.flight_key(self.pin), 150)

        self.run_stage(bootstrap.BootstrapStage.BUILDINGS, attempt=bootstrap.MAX_FLIGHT_WAITS)

        self.assertEqual(self.enqueued(tasks.bootstrap_location)[0].kwargs["stage"], bootstrap.BootstrapStage.NEST)

    def test_a_page_opened_during_the_site_fetches_dispatches_none_of_its_own(self) -> None:
        for stage in (
            bootstrap.BootstrapStage.BOUNDARY,
            bootstrap.BootstrapStage.BUILDINGS,
            bootstrap.BootstrapStage.NEST,
        ):
            self.run_stage(stage)
        # A broker that accepts the fetches without running them: they stay in flight.
        with self.upstreams.serving(), mock.patch(_ENQUEUE, return_value=mock.Mock()) as bootstrap_enqueue:
            tasks.bootstrap_location(self.pin.pk, stage=bootstrap.BootstrapStage.PANELS)
        self.assertTrue([call for call in bootstrap_enqueue.call_args_list if call.args[0] is tasks.fetch_panel_source])

        with self.upstreams.serving(), mock.patch(_ENQUEUE, return_value=mock.Mock()) as page:
            for key in bootstrap.SITE_PANEL_KEYS:
                source = get_panel_source(key)
                if source is not None and gate_allows(source, self.pin) and not source.has_landed(self.pin):
                    schedule_panel_fetch(key, self.pin)

        self.assertFalse([call for call in page.call_args_list if call.args[0] is tasks.fetch_panel_source])

    def test_wiki_enrichment_leaves_the_boundary_to_a_fetch_in_flight(self) -> None:
        wiki = Wiki.objects.get_or_create_for_location(self.pin.location)[0]
        acquire_lock(get_panel_source("boundary").flight_key(self.pin), 150)

        with (
            self.upstreams.serving(),
            tasks_run_inline(),
            mock.patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as generate,
        ):
            tasks.enrich_wiki_location(wiki.pk)

        generate.assert_not_called()


class BuildDateTests(_BootstrapCase):
    def test_a_date_the_owner_set_is_kept(self) -> None:
        pin = self.create_pin(inline=())
        Pin.objects.filter(pk=pin.pk).update(date_built=date(1868, 6, 1))

        with self.upstreams.serving(), tasks_run_inline(*AUTOMATIC), self.captureOnCommitCallbacks(execute=True):
            tasks.bootstrap_location(pin.pk)

        pin.refresh_from_db()
        self.assertEqual(pin.date_built, date(1868, 6, 1))

    def test_a_register_listing_drawn_around_the_pin_outranks_its_building(self) -> None:
        self.upstreams = HrshUpstreams(register_year=1867)

        pin = self.create_pin()

        self.assertEqual(pin.date_built, _year(1867))
        self.assertEqual(Pin.objects.get(name="MAIN/ADMIN").date_built, _year(MAIN_YEAR))

    def test_with_no_building_under_it_the_root_takes_the_assessors_year(self) -> None:
        latitude, longitude = offset(-30, -60)

        pin = self.create_pin(latitude=latitude, longitude=longitude)

        self.assertEqual(pin.date_built, _year(ASSESSOR_YEAR))

    def test_with_nothing_known_the_date_stays_empty(self) -> None:
        self.upstreams = HrshUpstreams(buildings=[], parcel_year=None)

        pin = self.create_pin()

        self.assertIsNone(pin.date_built)

    def test_the_about_card_shows_the_build_year(self) -> None:
        pin = self.create_pin()
        wiki = Wiki.objects.get(location=pin.location)

        html = render_to_string("dashboard/partials/wiki/_wiki_about_card.html", {"wiki": wiki, "wiki_links": []})

        self.assertIn(f"Built {MAIN_YEAR}", html)

    def test_rerunning_records_no_second_observation(self) -> None:
        pin = self.create_pin()
        before = FactEvidence.objects.count()

        with self.upstreams.serving(), tasks_run_inline(*AUTOMATIC), self.captureOnCommitCallbacks(execute=True):
            tasks.bootstrap_location(pin.pk)

        self.assertGreater(before, 0)
        self.assertEqual(FactEvidence.objects.count(), before)


class EnrichmentBackfillTests(_BootstrapCase):
    """The hourly job's cap stays for backfill; it is not spent on locations a bootstrap is already filling."""

    def test_a_location_being_bootstrapped_is_not_an_enrichment_candidate(self) -> None:
        from django.db.models import Q

        from urbanlens.dashboard.services.locations.enrichment import prioritized_location_candidates

        pin = self.create_pin(inline=())
        backfill = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=40.0, longitude=-75.0))

        candidates = prioritized_location_candidates(Q(), limit=10)

        self.assertNotIn(pin.location_id, [location.pk for location in candidates])
        self.assertIn(backfill.location_id, [location.pk for location in candidates])


class OnDemandTests(_BootstrapCase):
    def test_an_existing_root_pin_can_be_bootstrapped_by_hand(self) -> None:
        from django.core.management import call_command

        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])
        pin = self.create_pin(inline=())
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])

        with self.upstreams.serving(), tasks_run_inline(*AUTOMATIC), self.captureOnCommitCallbacks(execute=True):
            call_command("bootstrap_pin", str(pin.uuid), stdout=mock.Mock())

        self.assertEqual(len(self.children(pin)), ON_PROPERTY_BUILDINGS)

    def test_a_child_pin_is_refused(self) -> None:
        from django.core.management import CommandError, call_command

        pin = self.create_pin()
        child = self.children(pin)[0]

        with self.assertRaises(CommandError):
            call_command("bootstrap_pin", str(child.uuid), stdout=mock.Mock())
