"""Scheduled background enrichment: budget math, run window, prioritization, and the cycle.

Covers services.enrichment end to end with the network fully mocked:
compute_service_budget must honor the admin buffer and pace multi-day limits
evenly (the "300 calls per 30 days, 6 used today -> enrich 3 more" contract),
enrichment_window_open must respect the admin's UTC window including midnight
wrap, prioritized_location_candidates must rank high-impact locations first,
and run_enrichment_cycle must respect budgets, caps, and per-source isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from unittest.mock import patch

from django.db.models import Q
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.enrichment import (
    EnrichmentSource,
    compute_service_budget,
    enrichment_sources,
    enrichment_window_open,
    prioritized_location_candidates,
    run_enrichment_cycle,
    stagger_seconds,
)
from urbanlens.dashboard.services.geo_boundary import USA
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError
from urbanlens.UrbanLens.environments.meta import EnvironmentTypes


def _make_profile() -> Profile:
    user = baker.make("auth.User")
    return Profile.objects.get(user=user)


def _make_location(lat: str = "42.650000", lng: str = "-73.750000", **kwargs) -> Location:
    return baker.make(Location, latitude=lat, longitude=lng, google_place=None, **kwargs)


class EnrichmentWindowTests(SimpleTestCase):
    """enrichment_window_open - the admin-scheduled UTC run window."""

    @hypothesis_settings(max_examples=60, deadline=None)
    @given(start=st.integers(0, 23), end=st.integers(0, 23), hour=st.integers(0, 23))
    def test_window_semantics(self, start: int, end: int, hour: int) -> None:
        site_settings = SiteSettings(enrichment_start_hour=start, enrichment_end_hour=end)
        now = datetime(2026, 7, 16, hour, 30, tzinfo=UTC)
        expected = True if start == end else (hour - start) % 24 < (end - start) % 24
        self.assertEqual(enrichment_window_open(site_settings, now=now), expected)

    def test_wrapping_window(self) -> None:
        site_settings = SiteSettings(enrichment_start_hour=22, enrichment_end_hour=4)
        self.assertTrue(enrichment_window_open(site_settings, now=datetime(2026, 7, 16, 23, 0, tzinfo=UTC)))
        self.assertTrue(enrichment_window_open(site_settings, now=datetime(2026, 7, 16, 2, 0, tzinfo=UTC)))
        self.assertFalse(enrichment_window_open(site_settings, now=datetime(2026, 7, 16, 12, 0, tzinfo=UTC)))


class ComputeServiceBudgetTests(TestCase):
    """compute_service_budget - buffered, evenly-paced spend calculations."""

    def _limit(self, service: str, **kwargs) -> ApiRateLimit:
        defaults = {"display_name": service, "calls_per_minute": None, "calls_per_day": None, "calls_per_30_days": None}
        defaults.update(kwargs)
        return ApiRateLimit.objects.create(service=service, **defaults)

    def _log_calls(self, service: str, count: int, *, age: timedelta | None = None) -> None:
        rows = [ApiCallLog.objects.create(service=service, success=True) for _ in range(count)]
        if age is not None:
            ApiCallLog.objects.filter(pk__in=[row.pk for row in rows]).update(created=datetime.now(UTC) - age)

    def test_thirty_day_limit_paces_evenly_with_buffer(self) -> None:
        """The example from the spec: 300/30 days, 10% buffer, 6 used today -> 3 left."""
        self._limit("svc_month", calls_per_30_days=300)
        self._log_calls("svc_month", 6)
        self.assertEqual(compute_service_budget("svc_month"), 3)

    def test_thirty_day_headroom_caps_the_daily_pace(self) -> None:
        """Near the end of a heavy month, total headroom (not the daily pace) is the bound."""
        self._limit("svc_heavy", calls_per_30_days=300)
        self._log_calls("svc_heavy", 268, age=timedelta(days=5))
        # Daily pace would allow 9, but only 270 - 268 = 2 remain in the window.
        self.assertEqual(compute_service_budget("svc_heavy"), 2)

    def test_daily_limit_applies_buffer(self) -> None:
        self._limit("svc_day", calls_per_day=100)
        self._log_calls("svc_day", 20)
        self.assertEqual(compute_service_budget("svc_day"), 70)

    def test_never_negative(self) -> None:
        self._limit("svc_over", calls_per_day=10)
        self._log_calls("svc_over", 50)
        self.assertEqual(compute_service_budget("svc_over"), 0)

    def test_disabled_service_has_no_budget(self) -> None:
        self._limit("svc_off", calls_per_day=100, enabled=False)
        self.assertEqual(compute_service_budget("svc_off"), 0)

    def test_unbounded_service_returns_none(self) -> None:
        self._limit("svc_free", calls_per_minute=10)
        self.assertIsNone(compute_service_budget("svc_free"))

    def test_geo_filtered_calls_do_not_count(self) -> None:
        self._limit("svc_geo", calls_per_day=100)
        for _ in range(30):
            ApiCallLog.objects.create(service="svc_geo", success=False, was_geo_filtered=True)
        self.assertEqual(compute_service_budget("svc_geo"), 90)

    @hypothesis_settings(max_examples=15, deadline=None)
    @given(limit=st.integers(30, 3000), used=st.integers(0, 40), buffer_percent=st.integers(0, 90))
    def test_budget_matches_buffered_paced_headroom(self, limit: int, used: int, buffer_percent: int) -> None:
        """Budget = min(total buffered headroom, buffered daily pace) - used, floored at 0."""
        service = "svc_prop"
        self._limit(service, calls_per_30_days=limit)
        self._log_calls(service, used)
        site_settings = SiteSettings.get_current()
        site_settings.enrichment_buffer_percent = buffer_percent
        budget = compute_service_budget(service, site_settings)
        effective = math.floor(limit * (1 - buffer_percent / 100.0))
        expected = max(0, min(effective - used, effective // 30 - used))
        self.assertEqual(budget, expected)


class StaggerSecondsTests(TestCase):
    """stagger_seconds - per-item pacing from per-minute limits."""

    class _Source(EnrichmentSource):
        key = "stagger_test"
        service_keys = ("svc_stagger",)

        def missing_filter(self) -> Q:
            return Q()

        def enrich(self, location) -> bool:
            return True

    def test_derived_from_tightest_per_minute_limit(self) -> None:
        ApiRateLimit.objects.create(service="svc_stagger", display_name="x", calls_per_minute=30, calls_per_day=None, calls_per_30_days=None)
        self.assertEqual(stagger_seconds(self._Source()), 2.0)

    def test_clamped_to_max(self) -> None:
        ApiRateLimit.objects.create(service="svc_stagger", display_name="x", calls_per_minute=1, calls_per_day=None, calls_per_30_days=None)
        self.assertEqual(stagger_seconds(self._Source()), 60.0)

    def test_default_when_no_per_minute_limit(self) -> None:
        ApiRateLimit.objects.create(service="svc_stagger", display_name="x", calls_per_minute=None, calls_per_day=None, calls_per_30_days=None)
        self.assertEqual(stagger_seconds(self._Source()), 2.0)


class PrioritizedCandidatesTests(TestCase):
    """prioritized_location_candidates - impact-ordered selection."""

    def test_orders_by_impact_and_excludes_orphans(self) -> None:
        popular = _make_location(lat="40.000000")
        for _ in range(3):
            baker.make(Pin, profile=_make_profile(), location=popular)

        single = _make_location(lat="41.000000")
        baker.make(Pin, profile=_make_profile(), location=single)

        wiki_only = _make_location(lat="42.000000")
        baker.make(Wiki, location=wiki_only, name="Wiki Only Place")

        _orphan = _make_location(lat="43.000000")

        candidates = prioritized_location_candidates(Q(), limit=10)
        pks = [location.pk for location in candidates]
        self.assertEqual(pks[0], popular.pk)
        self.assertIn(single.pk, pks)
        self.assertIn(wiki_only.pk, pks)
        self.assertNotIn(_orphan.pk, pks)
        self.assertLess(pks.index(single.pk), pks.index(wiki_only.pk))

    def test_list_membership_boosts_priority(self) -> None:
        profile_a = _make_profile()
        listed = _make_location(lat="40.000000")
        listed_pin = baker.make(Pin, profile=profile_a, location=listed)
        pin_list = baker.make(PinList, profile=profile_a, name="Targets", slug="targets")
        PinListItem.objects.create(pin_list=pin_list, pin=listed_pin)

        unlisted = _make_location(lat="41.000000")
        baker.make(Pin, profile=_make_profile(), location=unlisted)

        candidates = prioritized_location_candidates(Q(), limit=10)
        pks = [location.pk for location in candidates]
        self.assertLess(pks.index(listed.pk), pks.index(unlisted.pk))

    def test_limit_is_respected(self) -> None:
        for index in range(5):
            baker.make(Pin, profile=_make_profile(), location=_make_location(lat=f"40.{index:06d}"))
        self.assertEqual(len(prioritized_location_candidates(Q(), limit=2)), 2)

    def test_geo_boundary_excludes_locations_outside_it(self) -> None:
        foreign = _make_location(lat="48.850000", lng="2.350000", country="France")
        baker.make(Pin, profile=_make_profile(), location=foreign)
        domestic = _make_location(lat="42.650000", country="United States")
        baker.make(Pin, profile=_make_profile(), location=domestic)
        blank = _make_location(lat="42.660000", country="")
        baker.make(Pin, profile=_make_profile(), location=blank)

        pks = [location.pk for location in prioritized_location_candidates(Q(), limit=10, geo_boundary=USA)]
        self.assertNotIn(foreign.pk, pks)
        self.assertIn(domestic.pk, pks)
        self.assertIn(blank.pk, pks)

    def test_missing_filter_is_applied(self) -> None:
        cached = _make_location(lat="40.000000")
        baker.make(Pin, profile=_make_profile(), location=cached)
        LocationCache.set(cached, "wikipedia", {"title": "Cached"})
        uncached = _make_location(lat="41.000000")
        baker.make(Pin, profile=_make_profile(), location=uncached)

        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        pks = [location.pk for location in prioritized_location_candidates(WikipediaEnrichmentSource().missing_filter(), limit=10)]
        self.assertNotIn(cached.pk, pks)
        self.assertIn(uncached.pk, pks)

    def test_stale_cache_row_still_counts_as_enriched(self) -> None:
        """Background enrichment backfills never-fetched locations only; staleness is lazy loading's job."""
        location = _make_location(lat="40.000000")
        baker.make(Pin, profile=_make_profile(), location=location)
        entry = LocationCache.set(location, "wikipedia", {"title": "Old"})
        LocationCache.objects.filter(pk=entry.pk).update(updated=datetime.now(UTC) - timedelta(days=400))

        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        pks = [candidate.pk for candidate in prioritized_location_candidates(WikipediaEnrichmentSource().missing_filter(), limit=10)]
        self.assertNotIn(location.pk, pks)


class _RecordingSource(EnrichmentSource):
    """Test double that records which locations it was asked to enrich."""

    key = "recording"
    verbose_name = "Recording source"
    service_keys = ("svc_cycle",)

    def __init__(self, fail_after: int | None = None) -> None:
        self.enriched_pks: list[int] = []
        self.fail_after = fail_after

    def missing_filter(self) -> Q:
        return Q()

    def enrich(self, location) -> bool:
        if self.fail_after is not None and len(self.enriched_pks) >= self.fail_after:
            raise RateLimitExceededError("svc_cycle")
        self.enriched_pks.append(location.pk)
        return True


class RunEnrichmentCycleTests(TestCase):
    """run_enrichment_cycle - budgets, caps, window, and per-source isolation."""

    def setUp(self) -> None:
        super().setUp()
        self.site_settings = SiteSettings.get_current()
        ApiRateLimit.objects.create(
            service="svc_cycle",
            display_name="Cycle service",
            calls_per_minute=None,
            calls_per_day=100,
            calls_per_30_days=None,
        )

    def _run(self, source: EnrichmentSource, **kwargs):
        # The cycle only ever actually runs in production (see
        # ProductionGateTests below) - everything else in this class is
        # testing budget/window/cap logic, not the environment gate itself,
        # so pin the effective environment to PRODUCTION here.
        with (
            patch("urbanlens.dashboard.services.enrichment.enrichment_sources", return_value=[source]),
            patch.object(SiteSettings, "get_effective_environment_type", return_value=EnvironmentTypes.PRODUCTION),
        ):
            return run_enrichment_cycle(sleep=lambda _seconds: None, **kwargs)

    def test_enriches_within_budget(self) -> None:
        for index in range(3):
            baker.make(Pin, profile=_make_profile(), location=_make_location(lat=f"40.{index:06d}"))
        source = _RecordingSource()
        summary = self._run(source)
        self.assertEqual(len(source.enriched_pks), 3)
        self.assertEqual(summary["sources"]["recording"]["enriched"], 3)

    def test_admin_cap_limits_items_and_keeps_highest_priority(self) -> None:
        low = _make_location(lat="40.000000")
        baker.make(Pin, profile=_make_profile(), location=low)
        high = _make_location(lat="41.000000")
        for _ in range(3):
            baker.make(Pin, profile=_make_profile(), location=high)
        self.site_settings.enrichment_max_per_service_per_run = 1
        self.site_settings.save(update_fields=["enrichment_max_per_service_per_run"])

        source = _RecordingSource()
        self._run(source)
        self.assertEqual(source.enriched_pks, [high.pk])

    def test_exhausted_budget_skips_source(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        for _ in range(95):
            ApiCallLog.objects.create(service="svc_cycle", success=True)
        source = _RecordingSource()
        summary = self._run(source)
        self.assertEqual(source.enriched_pks, [])
        self.assertEqual(summary["sources"]["recording"]["skipped"], "no_budget")

    def test_disabled_setting_skips_cycle_unless_forced(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        self.site_settings.enrichment_enabled = False
        self.site_settings.save(update_fields=["enrichment_enabled"])

        source = _RecordingSource()
        summary = self._run(source)
        self.assertEqual(summary.get("skipped"), "disabled")
        self.assertEqual(source.enriched_pks, [])

        summary = self._run(source, force=True)
        self.assertNotIn("skipped", summary)
        self.assertEqual(len(source.enriched_pks), 1)

    def test_outside_window_skips_cycle(self) -> None:
        self.site_settings.enrichment_start_hour = 2
        self.site_settings.enrichment_end_hour = 3
        self.site_settings.save(update_fields=["enrichment_start_hour", "enrichment_end_hour"])
        source = _RecordingSource()
        with patch("urbanlens.dashboard.services.enrichment.enrichment_window_open", return_value=False):
            summary = self._run(source)
        self.assertEqual(summary.get("skipped"), "outside_window")

    def test_rate_limit_mid_run_stops_source_gracefully(self) -> None:
        for index in range(3):
            baker.make(Pin, profile=_make_profile(), location=_make_location(lat=f"40.{index:06d}"))
        source = _RecordingSource(fail_after=1)
        summary = self._run(source)
        self.assertEqual(len(source.enriched_pks), 1)
        self.assertEqual(summary["sources"]["recording"]["skipped"], "rate_limited")
        self.assertEqual(summary["sources"]["recording"]["enriched"], 1)

    def test_service_disabled_on_api_limits_page_skips_source(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        ApiRateLimit.objects.filter(service="svc_cycle").update(enabled=False)
        source = _RecordingSource()
        summary = self._run(source)
        self.assertEqual(source.enriched_pks, [])
        self.assertEqual(summary["sources"]["recording"]["skipped"], "service_disabled")

    def test_names_refreshed_for_name_sources(self) -> None:
        location = _make_location()
        baker.make(Pin, profile=_make_profile(), location=location)

        class NameSource(_RecordingSource):
            refreshes_names = True

        source = NameSource()
        with patch("urbanlens.dashboard.services.enrichment.refresh_official_names", return_value=1) as mock_refresh:
            summary = self._run(source)
        mock_refresh.assert_called_once_with({location.pk})
        self.assertEqual(summary["names_refreshed"], 1)


class EnrichmentCycleProductionGateTests(TestCase):
    """run_enrichment_cycle must never spend real API quota outside production."""

    def setUp(self) -> None:
        super().setUp()
        ApiRateLimit.objects.create(service="svc_cycle", display_name="Cycle service", calls_per_minute=None, calls_per_day=100, calls_per_30_days=None)

    def _run_with_environment(self, env_type: EnvironmentTypes, source: EnrichmentSource, **kwargs):
        with (
            patch("urbanlens.dashboard.services.enrichment.enrichment_sources", return_value=[source]),
            patch.object(SiteSettings, "get_effective_environment_type", return_value=env_type),
        ):
            return run_enrichment_cycle(sleep=lambda _seconds: None, **kwargs)

    def test_non_production_skips_the_cycle_entirely(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        source = _RecordingSource()
        summary = self._run_with_environment(EnvironmentTypes.DEVELOPMENT, source)
        self.assertEqual(summary.get("skipped"), "non_production")
        self.assertEqual(source.enriched_pks, [])
        self.assertEqual(summary["sources"], {})

    def test_staging_also_skips_the_cycle(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        source = _RecordingSource()
        summary = self._run_with_environment(EnvironmentTypes.STAGING, source)
        self.assertEqual(summary.get("skipped"), "non_production")
        self.assertEqual(source.enriched_pks, [])

    def test_non_production_gate_is_not_bypassed_by_force(self) -> None:
        """Unlike the enabled-toggle/run-window checks, force=True must not skip this gate."""
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        source = _RecordingSource()
        summary = self._run_with_environment(EnvironmentTypes.DEVELOPMENT, source, force=True)
        self.assertEqual(summary.get("skipped"), "non_production")
        self.assertEqual(source.enriched_pks, [])

    def test_production_runs_the_cycle_normally(self) -> None:
        baker.make(Pin, profile=_make_profile(), location=_make_location())
        source = _RecordingSource()
        summary = self._run_with_environment(EnvironmentTypes.PRODUCTION, source)
        self.assertNotEqual(summary.get("skipped"), "non_production")
        self.assertEqual(len(source.enriched_pks), 1)


class EnrichmentSourceRegistryTests(SimpleTestCase):
    """enrichment_sources - core sources plus plugin contributions."""

    def test_core_and_plugin_sources_are_registered(self) -> None:
        keys = {source.key for source in enrichment_sources()}
        self.assertIn("address", keys)
        self.assertIn("boundary", keys)
        self.assertIn("wikipedia", keys)
        self.assertIn("nominatim", keys)
        self.assertIn("nps", keys)
        self.assertIn("google_place_link", keys)

    def test_keys_are_unique(self) -> None:
        keys = [source.key for source in enrichment_sources()]
        self.assertEqual(len(keys), len(set(keys)))


class LocationCacheSourceBehaviorTests(TestCase):
    """LocationCacheEnrichmentSource - per-provider completion tracking."""

    def test_enrich_stores_empty_marker_when_nothing_found(self) -> None:
        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        location = _make_location()
        source = WikipediaEnrichmentSource()
        with patch.object(WikipediaEnrichmentSource, "fetch", return_value=(None, "q")):
            self.assertTrue(source.enrich(location))
        row = LocationCache.objects.get(location=location, source="wikipedia")
        self.assertEqual(row.data, {})
        # The empty marker means the location no longer counts as missing.
        self.assertFalse(Location.objects.filter(source.missing_filter(), pk=location.pk).exists())

    def test_provider_tracking_is_independent_per_source(self) -> None:
        from urbanlens.dashboard.plugins.builtin.nominatim import NominatimEnrichmentSource
        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        location = _make_location()
        LocationCache.set(location, "wikipedia", {"title": "Known"})
        self.assertFalse(Location.objects.filter(WikipediaEnrichmentSource().missing_filter(), pk=location.pk).exists())
        self.assertTrue(Location.objects.filter(NominatimEnrichmentSource().missing_filter(), pk=location.pk).exists())


class ScheduledEnrichmentTaskTests(TestCase):
    """tasks.run_scheduled_enrichment - the hourly beat entry point."""

    def test_runs_cycle_and_releases_lock(self) -> None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.enrichment import RUN_LOCK_CACHE_KEY
        from urbanlens.dashboard.tasks import run_scheduled_enrichment

        cache.delete(RUN_LOCK_CACHE_KEY)
        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.enrichment.run_enrichment_cycle", return_value={"sources": {}}) as mock_cycle,
        ):
            result = run_scheduled_enrichment.apply().result
        mock_cycle.assert_called_once()
        self.assertEqual(result, {"sources": {}})
        self.assertIsNone(cache.get(RUN_LOCK_CACHE_KEY))

    def test_single_flight_lock_skips_concurrent_run(self) -> None:
        from django.core.cache import cache

        from urbanlens.dashboard.services.enrichment import RUN_LOCK_CACHE_KEY
        from urbanlens.dashboard.tasks import run_scheduled_enrichment

        cache.add(RUN_LOCK_CACHE_KEY, 1, 60)
        try:
            with (
                patch("urbanlens.dashboard.tasks.update_task_progress"),
                patch("urbanlens.dashboard.services.enrichment.run_enrichment_cycle") as mock_cycle,
            ):
                result = run_scheduled_enrichment.apply().result
            mock_cycle.assert_not_called()
            self.assertEqual(result, {"skipped": "already_running"})
        finally:
            cache.delete(RUN_LOCK_CACHE_KEY)
