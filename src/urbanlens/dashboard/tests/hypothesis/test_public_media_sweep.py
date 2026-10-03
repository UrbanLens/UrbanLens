"""The public-media sweep removes cached results no one is shown, and nothing else (P233).

Built on P196's Hudson River State Hospital row: two photos and a report are about the place; a photo geolocated in
Binghamton, one across the river with no caption, and two books that only mention the words are not.
"""

from __future__ import annotations

from dataclasses import asdict
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from hypothesis import HealthCheck, given, settings as hypothesis_settings, strategies as st
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.media import subject_relevance
from urbanlens.dashboard.services.media.public_media_sweep import sweep_public_media
from urbanlens.dashboard.services.media.subject_relevance import MediaSubject, subject_for_location
from urbanlens.dashboard.services.pins.external_data import get_panel_source
from urbanlens.dashboard.services.pins.search_names import search_names
from urbanlens.dashboard.tests.hypothesis.test_media_place_relevance import (
    ACROSS_THE_RIVER,
    ANCESTRY,
    ANNUAL_REPORT,
    COUNTY_HISTORY,
    IN_BINGHAMTON,
    ITEMS,
    NAMED,
    ON_CAMPUS,
    GoogleImagesTests,
    _commons,
    _HrshTestCase,
)

ABOUT_THE_PLACE = [ON_CAMPUS.url, NAMED.url, ANNUAL_REPORT.url]
BY_ALIAS = _commons("Withers_asylum_1880.jpg", "The Withers Asylum, Poughkeepsie, about 1880")
BY_ACRONYM = _commons("WSAP_1890.jpg", "WSAP, 1890")


class _SweepCase(_HrshTestCase):
    def cached_urls(self, audience: str = "", source: str = "wikimedia") -> list[str]:
        row = LocationCache.objects.get(location=self.location, source=source, audience=audience)
        return [item.get("url") or item.get("thumbnail") for item in row.data["items"]]

    def other_profile(self):
        return baker.make("auth.User").profile


class SharedRowTests(_SweepCase):
    def test_a_result_not_about_the_place_is_removed(self) -> None:
        sweep_public_media()

        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)

    def test_the_row_keeps_its_age_and_everything_but_the_removed_results(self) -> None:
        before = LocationCache.objects.get(location=self.location, source="wikimedia")

        sweep_public_media()

        after = LocationCache.objects.get(pk=before.pk)
        self.assertEqual(after.updated, before.updated)
        self.assertEqual(after.query_key, before.query_key)
        self.assertEqual(after.data["search_names"], before.data["search_names"])
        self.assertEqual(after.data["items"], [item for item in before.data["items"] if item["url"] in ABOUT_THE_PLACE])

    def test_a_result_anyone_marked_is_kept_whichever_way(self) -> None:
        for item, is_relevant in ((ACROSS_THE_RIVER, False), (IN_BINGHAMTON, True)):
            MediaRelevance.objects.create(
                profile=self.other_profile(),
                location=self.location,
                source="wikimedia",
                item_key=media_item_key(item.url),
                is_relevant=is_relevant,
            )

        sweep_public_media()

        self.assertEqual(set(self.cached_urls()), {*ABOUT_THE_PLACE, ACROSS_THE_RIVER.url, IN_BINGHAMTON.url})

    def test_a_result_anyone_copied_is_kept_under_any_provider(self) -> None:
        for item, source in ((ANCESTRY, "wikimedia"), (COUNTY_HISTORY, "wikipedia_media")):
            baker.make(
                Image,
                location=self.location,
                profile=self.other_profile(),
                media_type=MediaKind.PHOTO,
                media_source_key=source,
                media_item_key=media_item_key(item.url),
            )

        sweep_public_media()

        self.assertEqual(set(self.cached_urls()), {*ABOUT_THE_PLACE, ANCESTRY.url, COUNTY_HISTORY.url})

    def test_only_the_cached_results_change(self) -> None:
        baker.make(Image, location=self.location, pin=self.pin, profile=self.profile, media_type=MediaKind.PHOTO)
        MediaRelevance.objects.create(
            profile=self.profile, location=self.location, source="wikimedia", item_key="f" * 40, is_relevant=False
        )
        counts = (
            Image.objects.count(),
            MediaRelevance.objects.count(),
            LocationCache.objects.count(),
            Pin.objects.count(),
        )

        sweep_public_media()

        self.assertEqual(
            (Image.objects.count(), MediaRelevance.objects.count(), LocationCache.objects.count(), Pin.objects.count()),
            counts,
        )

    def test_a_source_that_does_not_judge_relevance_is_left_alone(self) -> None:
        LocationCache.set(self.location, "wikipedia_media", {"items": [asdict(IN_BINGHAMTON), asdict(ANCESTRY)]}, "q")

        sweep_public_media()

        self.assertEqual(self.cached_urls(source="wikipedia_media"), [IN_BINGHAMTON.url, ANCESTRY.url])

    def test_google_images_results_are_judged_like_the_rest(self) -> None:
        results = [GoogleImagesTests.ARTWORK, GoogleImagesTests.MOVIE, GoogleImagesTests.ABOUT_THE_PLACE]
        LocationCache.set(self.location, "google_images", {"items": results}, query_key="138 Hudson View Dr")

        sweep_public_media()

        self.assertEqual(self.cached_urls(source="google_images"), [GoogleImagesTests.ABOUT_THE_PLACE["thumbnail"]])

    def test_a_result_that_is_not_a_media_item_is_kept(self) -> None:
        self.cache(ITEMS)
        row = LocationCache.objects.get(location=self.location, source="wikimedia")
        LocationCache.objects.filter(pk=row.pk).update(data={**row.data, "items": [*row.data["items"], "not an item"]})

        sweep_public_media()

        row.refresh_from_db()
        self.assertEqual(row.data["items"][-1], "not an item")
        self.assertEqual([item["url"] for item in row.data["items"][:-1]], ABOUT_THE_PLACE)


class ReadersSeeTheSameTests(_SweepCase):
    """Whatever a reader judging by the place's public names was shown before the sweep, it is shown after."""

    @hypothesis_settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        cached=st.lists(st.sampled_from(ITEMS), unique_by=lambda item: item.url, min_size=1),
        marked=st.sets(st.sampled_from([item.url for item in ITEMS])),
    )
    def test_the_sweep_never_changes_what_is_shown(self, cached, marked) -> None:
        self.cache(cached)
        voter = self.other_profile()
        for url in marked:
            MediaRelevance.objects.create(
                profile=voter,
                location=self.location,
                source="wikimedia",
                item_key=media_item_key(url),
                is_relevant=True,
            )
        kept = {media_item_key(url) for url in marked}
        panel = get_panel_source("wikimedia")
        subject = subject_for_location(self.location)

        def shown() -> list[str]:
            data = LocationCache.objects.get(location=self.location, source="wikimedia").data
            return [item.url for item in panel.relevant_media_items(data, subject, kept=kept)]

        before = shown()
        gallery = self.pin_gallery()

        sweep_public_media()

        self.assertEqual(shown(), before)
        self.assertEqual(self.pin_gallery(), gallery)


class AudienceRowTests(_SweepCase):
    """A row cached for a pin's own names is judged by the names of the pins that read it."""

    def setUp(self) -> None:
        super().setUp()
        PinAlias.objects.create(pin=self.pin, name="Withers Asylum")
        PinAlias.objects.create(pin=self.pin, name="WSAP")
        own = search_names(self.pin).own
        assert own is not None
        self.audience = own.audience
        self.cache([BY_ALIAS, BY_ACRONYM, ACROSS_THE_RIVER], audience=self.audience)

    def test_what_its_readers_names_match_is_kept(self) -> None:
        sweep_public_media()

        self.assertEqual(self.cached_urls(self.audience), [BY_ALIAS.url, BY_ACRONYM.url])

    def test_the_shared_row_is_judged_by_public_names_only(self) -> None:
        self.cache([*ITEMS, BY_ALIAS])

        sweep_public_media()

        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)

    def test_a_row_no_pin_reads_any_more_is_left_alone(self) -> None:
        PinAlias.objects.filter(pin=self.pin).delete()

        sweep_public_media()

        self.assertEqual(self.cached_urls(self.audience), [BY_ALIAS.url, BY_ACRONYM.url, ACROSS_THE_RIVER.url])


class RuleVersionTests(_SweepCase):
    def test_a_swept_row_is_not_judged_again_under_the_same_rule(self) -> None:
        sweep_public_media()

        with mock.patch.object(MediaSubject, "matches", return_value=False):
            sweep_public_media()

        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)

    def test_a_changed_rule_judges_every_row_again(self) -> None:
        sweep_public_media()

        with (
            mock.patch.object(subject_relevance, "RULE_VERSION", subject_relevance.RULE_VERSION + 1),
            mock.patch.object(
                MediaSubject, "matches", autospec=True, side_effect=lambda _subject, item: item.url != NAMED.url
            ),
        ):
            sweep_public_media()

        self.assertEqual(self.cached_urls(), [ON_CAMPUS.url, ANNUAL_REPORT.url])

    def test_a_refetched_row_is_judged_again(self) -> None:
        sweep_public_media()
        self.cache(ITEMS)
        self.assertEqual(len(self.cached_urls()), len(ITEMS))

        sweep_public_media()

        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)


class ConcurrentFetchTests(_SweepCase):
    def test_a_fetch_landing_mid_sweep_is_not_overwritten(self) -> None:
        judge = MediaSubject.matches
        fetched = [asdict(ACROSS_THE_RIVER)]

        def fetch_lands(subject: MediaSubject, item) -> bool:
            if LocationCache.objects.filter(location=self.location, source="wikimedia", data__items=fetched).exists():
                return judge(subject, item)
            LocationCache.set(self.location, "wikimedia", {"items": fetched}, query_key="refetched")
            return judge(subject, item)

        with mock.patch.object(MediaSubject, "matches", autospec=True, side_effect=fetch_lands):
            sweep_public_media()

        self.assertEqual(self.cached_urls(), [ACROSS_THE_RIVER.url])
        sweep_public_media()
        self.assertEqual(self.cached_urls(), [])


class BatchTests(_SweepCase):
    def test_a_sweep_out_of_time_leaves_the_rest_for_the_next(self) -> None:
        LocationCache.set(self.location, "flickr", {"items": [asdict(IN_BINGHAMTON)]}, "q")

        first = sweep_public_media(batch_size=1, budget_seconds=0)
        second = sweep_public_media(batch_size=1, budget_seconds=0)
        third = sweep_public_media(batch_size=1, budget_seconds=0)

        self.assertEqual((first.rows, first.remaining), (1, True))
        self.assertEqual((second.rows, second.remaining), (1, False))
        self.assertEqual(third.rows, 0)
        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)
        self.assertEqual(self.cached_urls(source="flickr"), [])

    def test_queries_do_not_grow_with_the_results_a_row_holds(self) -> None:
        def queries_for(items) -> int:
            self.cache(items)
            with CaptureQueriesContext(connection) as captured:
                sweep_public_media()
            return len(captured)

        few = queries_for(ITEMS)
        many = queries_for([_commons(f"IMG_{n}.jpg", f"IMG_{n}") for n in range(60)] + list(ITEMS))

        self.assertEqual(many, few)


class TaskTests(_SweepCase):
    def test_the_sweep_is_scheduled(self) -> None:
        tasks = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}

        self.assertIn("urbanlens.dashboard.tasks.sweep_public_media_cache", tasks)

    def test_the_task_sweeps(self) -> None:
        from urbanlens.dashboard.tasks import sweep_public_media_cache

        sweep_public_media_cache()

        self.assertEqual(self.cached_urls(), ABOUT_THE_PLACE)

    def test_a_run_out_of_time_queues_the_next(self) -> None:
        from urbanlens.dashboard import tasks

        with (
            mock.patch.object(tasks, "_PUBLIC_MEDIA_SWEEP_BUDGET_SECONDS", 0),
            mock.patch.object(tasks, "_PUBLIC_MEDIA_SWEEP_BATCH", 1),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            LocationCache.set(self.location, "flickr", {"items": [asdict(IN_BINGHAMTON)]}, "q")
            tasks.sweep_public_media_cache()

        enqueue.assert_called_once()
        self.assertIs(enqueue.call_args.args[0], tasks.sweep_public_media_cache)
