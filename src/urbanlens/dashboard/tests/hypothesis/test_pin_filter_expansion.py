"""Tests for the filters expansion: date-range/security/has-links/detail-pin-count
criteria on PinQuerySet.filter_by_criteria, Pin.last_viewed_at + mark_viewed(),
the new smart-list/saved-filter-cache resync signals, and criteria (de)serialization.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.services.filter_criteria import deserialize_criteria, serialize_form_criteria

_coord_counter = 0


def _make_pin(profile, **kwargs) -> Pin:
    """Create a pin with a real coordinate-bearing Location (unique per call)."""
    global _coord_counter
    location = kwargs.pop("location", None)
    if location is None:
        _coord_counter += 1
        location = baker.make(Location, latitude=40.0 + _coord_counter * 0.001, longitude=-74.0 - _coord_counter * 0.001)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


class DateBuiltAbandonedLastViewedFilterTests(TestCase):
    """date_built / date_abandoned / last_viewed after/before range criteria."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_date_built_range(self) -> None:
        old = _make_pin(self.profile, date_built=date(1900, 1, 1))
        new = _make_pin(self.profile, date_built=date(2000, 1, 1))
        unset = _make_pin(self.profile, date_built=None)
        qs = self._base_qs().filter_by_criteria({"date_built_after": date(1950, 1, 1)})
        ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(old.pk, ids)
        self.assertIn(new.pk, ids)
        self.assertNotIn(unset.pk, ids)

    def test_date_abandoned_range(self) -> None:
        old = _make_pin(self.profile, date_abandoned=date(1990, 1, 1))
        new = _make_pin(self.profile, date_abandoned=date(2020, 1, 1))
        qs = self._base_qs().filter_by_criteria({"date_abandoned_before": date(2000, 1, 1)})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(old.pk, ids)
        self.assertNotIn(new.pk, ids)

    def test_last_viewed_range(self) -> None:
        recently_viewed = _make_pin(self.profile)
        recently_viewed.last_viewed_at = timezone.now()
        recently_viewed.save(update_fields=["last_viewed_at"])
        never_viewed = _make_pin(self.profile, last_viewed_at=None)
        qs = self._base_qs().filter_by_criteria({"last_viewed_after": date.today() - timedelta(days=1)})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(recently_viewed.pk, ids)
        self.assertNotIn(never_viewed.pk, ids)

    def test_omitting_new_date_keys_applies_no_filter(self) -> None:
        pin = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({})
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))


class SecurityIndicatorFilterTests(TestCase):
    """security_<field> criteria: exact match on a SecurityLevel choice."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_exact_match_on_one_field(self) -> None:
        fenced = _make_pin(self.profile, fences="everywhere")
        unfenced = _make_pin(self.profile, fences="no")
        qs = self._base_qs().filter_by_criteria({"security_fences": "everywhere"})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(fenced.pk, ids)
        self.assertNotIn(unfenced.pk, ids)

    def test_invalid_value_is_ignored(self) -> None:
        pin = _make_pin(self.profile, fences="no")
        qs = self._base_qs().filter_by_criteria({"security_fences": "not_a_real_level"})
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))

    def test_every_security_field_is_filterable(self) -> None:
        """Every SECURITY_FIELDS entry has a working criteria key, not just fences."""
        for field_key, _label in SECURITY_FIELDS:
            match = _make_pin(self.profile, **{field_key: "everywhere"})
            other = _make_pin(self.profile, **{field_key: "no"})
            qs = self._base_qs().filter_by_criteria({f"security_{field_key}": "everywhere"})
            ids = set(qs.values_list("pk", flat=True))
            self.assertIn(match.pk, ids, f"{field_key} filter should include an exact match")
            self.assertNotIn(other.pk, ids, f"{field_key} filter should exclude a non-match")

    def test_two_security_fields_combine_with_and(self) -> None:
        both = _make_pin(self.profile, fences="everywhere", cameras="everywhere")
        one_only = _make_pin(self.profile, fences="everywhere", cameras="no")
        qs = self._base_qs().filter_by_criteria({"security_fences": "everywhere", "security_cameras": "everywhere"})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(both.pk, ids)
        self.assertNotIn(one_only.pk, ids)


class HasLinksFilterTests(TestCase):
    """has_links criterion: tri-state yes/no/any."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile)

    def test_yes_includes_only_pins_with_links(self) -> None:
        with_link = _make_pin(self.profile)
        PinLink.objects.create(pin=with_link, url="https://example.com/a")
        without_link = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({"has_links": "yes"})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(with_link.pk, ids)
        self.assertNotIn(without_link.pk, ids)

    def test_no_includes_only_pins_without_links(self) -> None:
        with_link = _make_pin(self.profile)
        PinLink.objects.create(pin=with_link, url="https://example.com/a")
        without_link = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({"has_links": "no"})
        ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(with_link.pk, ids)
        self.assertIn(without_link.pk, ids)

    def test_multiple_links_do_not_duplicate_the_pin(self) -> None:
        pin = _make_pin(self.profile)
        PinLink.objects.create(pin=pin, url="https://example.com/a")
        PinLink.objects.create(pin=pin, url="https://example.com/b")
        qs = self._base_qs().filter_by_criteria({"has_links": "yes"})
        self.assertEqual(list(qs.values_list("pk", flat=True)).count(pin.pk), 1)

    def test_empty_string_applies_no_filter(self) -> None:
        pin = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({"has_links": ""})
        self.assertIn(pin.pk, qs.values_list("pk", flat=True))


class DetailPinCountFilterTests(TestCase):
    """min_detail_pins / max_detail_pins criteria count a pin's own detail pins."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def _base_qs(self):
        return Pin.objects.filter(profile=self.profile).root_pins()

    def test_min_detail_pins(self) -> None:
        parent_with_two = _make_pin(self.profile)
        _make_pin(self.profile, parent_pin=parent_with_two)
        _make_pin(self.profile, parent_pin=parent_with_two)
        parent_with_none = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({"min_detail_pins": 2})
        ids = set(qs.values_list("pk", flat=True))
        self.assertIn(parent_with_two.pk, ids)
        self.assertNotIn(parent_with_none.pk, ids)

    def test_max_detail_pins(self) -> None:
        parent_with_two = _make_pin(self.profile)
        _make_pin(self.profile, parent_pin=parent_with_two)
        _make_pin(self.profile, parent_pin=parent_with_two)
        parent_with_none = _make_pin(self.profile)
        qs = self._base_qs().filter_by_criteria({"max_detail_pins": 0})
        ids = set(qs.values_list("pk", flat=True))
        self.assertNotIn(parent_with_two.pk, ids)
        self.assertIn(parent_with_none.pk, ids)

    def test_count_is_not_inflated_by_an_unrelated_join(self) -> None:
        """Combining a detail-pin-count filter with a label filter must not
        multiply the Count() via the label m2m join (Count(distinct=True) guards this)."""
        from urbanlens.dashboard.models.labels.model import KIND_TAG, Label

        tag = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="Interesting")
        parent = _make_pin(self.profile)
        parent.labels.add(tag)
        _make_pin(self.profile, parent_pin=parent)
        qs = self._base_qs().filter_by_criteria({"tags": [tag], "min_detail_pins": 1, "max_detail_pins": 1})
        self.assertIn(parent.pk, qs.values_list("pk", flat=True))


class MarkViewedTests(TestCase):
    """Pin.mark_viewed(): throttled to once/day, fires the resync signal."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.pin = _make_pin(self.profile)

    def test_first_view_sets_last_viewed_at(self) -> None:
        self.assertIsNone(self.pin.last_viewed_at)
        self.pin.mark_viewed()
        self.pin.refresh_from_db()
        self.assertIsNotNone(self.pin.last_viewed_at)

    def test_second_view_same_day_does_not_bump_the_timestamp(self) -> None:
        self.pin.mark_viewed()
        self.pin.refresh_from_db()
        first_ts = self.pin.last_viewed_at
        self.pin.mark_viewed()
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.last_viewed_at, first_ts)

    def test_view_on_a_later_day_bumps_the_timestamp(self) -> None:
        self.pin.last_viewed_at = timezone.now() - timedelta(days=2)
        self.pin.save(update_fields=["last_viewed_at"])
        old_ts = self.pin.last_viewed_at
        self.pin.mark_viewed()
        self.pin.refresh_from_db()
        self.assertGreater(self.pin.last_viewed_at, old_ts)

    def test_view_updates_last_viewed_smart_list(self) -> None:
        """Viewing a pin resyncs smart lists filtering on last_viewed_after (uses
        save(), so the existing generic post_save resync signal fires for free)."""
        pin_list = baker.make(PinList, profile=self.profile, is_smart=True, smart_filter={"last_viewed_after": date.today().isoformat()})
        with self.captureOnCommitCallbacks(execute=True):
            self.pin.mark_viewed()
        self.assertTrue(PinListItem.objects.filter(pin_list=pin_list, pin=self.pin).exists())


class PinViewMarksViewedTests(TestCase):
    """The pin detail view (controllers.pin.PinController.view) marks the pin viewed."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = _make_pin(self.profile)
        self.client.force_login(self.user)

    def test_loading_the_pin_page_sets_last_viewed_at(self) -> None:
        self.assertIsNone(self.pin.last_viewed_at)
        self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.pin.refresh_from_db()
        self.assertIsNotNone(self.pin.last_viewed_at)


class PinLinkResyncSignalTests(TestCase):
    """Adding/removing a PinLink must resync smart lists and bump the pin's
    updated timestamp (PinLink writes never call Pin.save() themselves)."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.pin = _make_pin(self.profile)
        self.pin_list = baker.make(PinList, profile=self.profile, is_smart=True, smart_filter={"has_links": "yes"})

    def test_adding_a_link_resyncs_smart_lists(self) -> None:
        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.pin).exists())
        with self.captureOnCommitCallbacks(execute=True):
            PinLink.objects.create(pin=self.pin, url="https://example.com/a")
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.pin).exists())

    def test_removing_the_last_link_resyncs_smart_lists(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            link = PinLink.objects.create(pin=self.pin, url="https://example.com/a")
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.pin).exists())

        with self.captureOnCommitCallbacks(execute=True):
            link.delete()
        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.pin).exists())

    def test_adding_a_link_bumps_the_pins_updated_timestamp(self) -> None:
        """Confirms the saved-filter-cache staleness gap is closed too (its cache
        key fingerprints on Max(Pin.updated))."""
        original_updated = self.pin.updated
        with self.captureOnCommitCallbacks(execute=True):
            PinLink.objects.create(pin=self.pin, url="https://example.com/a")
        self.pin.refresh_from_db()
        self.assertGreater(self.pin.updated, original_updated)


class DetailPinResyncSignalTests(TestCase):
    """Creating/deleting a detail pin must resync the PARENT's smart lists and
    bump the parent's updated timestamp (only the child's own post_save/delete
    fires by default - the parent's detail-pin-count criterion needs its own signal)."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.parent = _make_pin(self.profile)
        self.pin_list = baker.make(PinList, profile=self.profile, is_smart=True, smart_filter={"min_detail_pins": 1})

    def test_creating_a_detail_pin_resyncs_the_parents_smart_lists(self) -> None:
        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.parent).exists())
        with self.captureOnCommitCallbacks(execute=True):
            child = _make_pin(self.profile, parent_pin=self.parent)
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.parent).exists())
        self.assertIsNotNone(child.pk)

    def test_deleting_the_last_detail_pin_resyncs_the_parents_smart_lists(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            child = _make_pin(self.profile, parent_pin=self.parent)
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.parent).exists())

        with self.captureOnCommitCallbacks(execute=True):
            child.delete()
        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=self.parent).exists())

    def test_editing_a_detail_pin_without_changing_count_does_not_loop_forever(self) -> None:
        """A guard-rail: editing an existing detail pin must not recurse (created=False)."""
        with self.captureOnCommitCallbacks(execute=True):
            child = _make_pin(self.profile, parent_pin=self.parent)
        child.name = "Renamed"
        with self.captureOnCommitCallbacks(execute=True):
            child.save(update_fields=["name", "updated"])
        # Reaching this line at all (no RecursionError/timeout) is the assertion.
        self.assertEqual(child.name, "Renamed")


class SerializeDeserializeCriteriaTests(TestCase):
    """serialize_form_criteria / deserialize_criteria round-trip the new keys."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def test_date_keys_round_trip(self) -> None:
        cleaned = {
            "date_built_after": date(2000, 1, 1),
            "date_built_before": date(2010, 1, 1),
            "date_abandoned_after": date(2015, 1, 1),
            "date_abandoned_before": date(2020, 1, 1),
            "last_viewed_after": date(2025, 1, 1),
            "last_viewed_before": date(2026, 1, 1),
        }
        stored = serialize_form_criteria(cleaned, None, None)
        self.assertEqual(stored["date_built_after"], "2000-01-01")
        restored = deserialize_criteria(stored, self.profile)
        self.assertEqual(restored["date_built_after"], date(2000, 1, 1))
        self.assertEqual(restored["last_viewed_before"], date(2026, 1, 1))

    def test_security_and_links_and_detail_pin_keys_round_trip(self) -> None:
        cleaned = {
            "security_fences": "everywhere",
            "security_locked": "no",
            "has_links": "yes",
            "min_detail_pins": 2,
            "max_detail_pins": 5,
        }
        stored = serialize_form_criteria(cleaned, None, None)
        for key, value in cleaned.items():
            self.assertEqual(stored[key], value)
        restored = deserialize_criteria(stored, self.profile)
        for key, value in cleaned.items():
            self.assertEqual(restored[key], value)

    def test_unset_new_fields_are_omitted(self) -> None:
        stored = serialize_form_criteria({"security_fences": "", "has_links": "", "min_detail_pins": None}, None, None)
        self.assertNotIn("security_fences", stored)
        self.assertNotIn("has_links", stored)
        self.assertNotIn("min_detail_pins", stored)


class SearchFormNewFieldsTests(TestCase):
    """SearchForm accepts the new fields and rejects invalid security choices."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def test_valid_submission(self) -> None:
        from urbanlens.dashboard.forms.search import SearchForm

        form = SearchForm(
            {
                "date_built_after": "2000-01-01",
                "security_fences": "everywhere",
                "has_links": "yes",
                "min_detail_pins": "1",
                "max_detail_pins": "5",
            },
            profile=self.profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["security_fences"], "everywhere")

    def test_invalid_security_choice_rejected(self) -> None:
        from urbanlens.dashboard.forms.search import SearchForm

        form = SearchForm({"security_fences": "not_a_real_level"}, profile=self.profile)
        self.assertFalse(form.is_valid())

    def test_negative_detail_pin_bounds_rejected(self) -> None:
        from urbanlens.dashboard.forms.search import SearchForm

        form = SearchForm({"min_detail_pins": "-1"}, profile=self.profile)
        self.assertFalse(form.is_valid())


class FilterCriteriaSummaryNewKeysTests(SimpleTestCase):
    """filter_criteria_summary() surfaces the new criteria in the auto-name/card summary."""

    def test_summary_mentions_each_new_dimension(self) -> None:
        from urbanlens.dashboard.templatetags.dashboard_tags import filter_criteria_summary

        summary = filter_criteria_summary(
            {
                "date_built_after": "2000-01-01",
                "date_abandoned_before": "2020-01-01",
                "last_viewed_after": "2025-01-01",
                "has_links": "yes",
                "min_detail_pins": 1,
                "security_fences": "everywhere",
                "security_cameras": "no",
            },
        )
        self.assertIn("date built range", summary)
        self.assertIn("date abandoned range", summary)
        self.assertIn("last viewed range", summary)
        self.assertIn("has links", summary)
        self.assertIn("detail pin count range", summary)
        self.assertIn("2 security indicator(s)", summary)

    def test_empty_criteria_is_no_conditions(self) -> None:
        from urbanlens.dashboard.templatetags.dashboard_tags import filter_criteria_summary

        self.assertEqual(filter_criteria_summary({}), "No conditions set")
