"""Tests for pin list regressions found while polishing the pin list feature.

Invariants verified:
  - serialize_form_criteria preserves the "name" (pin-name-contains) field -
    it was previously dropped silently, so a saved filter or smart list built
    from a name search lost that criterion the moment it was saved.
  - PinListMarkupMapView's "no pins with coordinates" error is valid JSON (the
    client always calls response.json() on it), not a plain-text body that
    throws a SyntaxError in the browser.
  - Picking a saved filter (or drawing a boundary) on the list-detail page
    populates matching pins immediately, even before "keep this list in sync
    automatically" (is_smart) is turned on - it used to silently do nothing
    until that separate toggle was flipped, which read as "the smart filter
    doesn't work".
  - A smart list's membership re-syncs when a pin's labels change (add/remove/
    clear), not just when the pin itself is saved - label add/remove is a
    pure M2M operation that never calls Pin.save(), so this used to leave
    smart lists stale after a badge was added to (or removed from) a pin.
  - SavedFilterSuggestNameView returns a name summarizing the current form
    criteria (for the create/edit dialog's auto-suggested "Filter name"), or
    None when there's nothing active yet to summarize.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.filter_criteria import serialize_form_criteria

_db_settings = settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])

_coord_counter = 0


def _make_pin(profile, **kwargs) -> Pin:
    """Create a pin with a real coordinate-bearing Location (unique per call)."""
    global _coord_counter
    location = kwargs.pop("location", None)
    if location is None:
        _coord_counter += 1
        location = baker.make(Location, latitude=40.0 + _coord_counter * 0.001, longitude=-74.0 - _coord_counter * 0.001)
    return baker.make(Pin, profile=profile, location=location, **kwargs)


class SerializeFormCriteriaNamePreservedTests(SimpleTestCase):
    """serialize_form_criteria must not drop the `name` filter field."""

    @_db_settings
    @given(name=st.text(min_size=1, max_size=100).filter(lambda s: s.strip()))
    def test_name_round_trips(self, name: str) -> None:
        criteria = serialize_form_criteria({"name": name}, label_groups=None, custom_field_criteria=None)
        self.assertEqual(criteria.get("name"), name.strip())

    def test_blank_name_is_not_stored(self) -> None:
        criteria = serialize_form_criteria({"name": "   "}, label_groups=None, custom_field_criteria=None)
        self.assertNotIn("name", criteria)

    def test_name_alone_is_not_an_empty_criteria_dict(self) -> None:
        # A saved filter made of just a name search must be considered "active"
        # criteria by SavedFilterCreateView, which rejects an empty dict.
        criteria = serialize_form_criteria({"name": "rooftop"}, label_groups=None, custom_field_criteria=None)
        self.assertTrue(criteria)


class PinListMarkupMapErrorIsJsonTests(TestCase):
    """The markup-map endpoint must return JSON even on its error paths."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_no_geo_pins_returns_json_error(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Empty list")
        response = self.client.post(reverse("lists.markup_map", kwargs={"list_slug": pin_list.slug}))
        self.assertEqual(response.status_code, 400)
        # Must not raise - this is exactly what broke: the client always calls
        # response.json(), and a plain-text body throws a SyntaxError.
        data = json.loads(response.content)
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_detail_page_has_a_create_markup_map_button(self) -> None:
        """The list-detail page overhaul dropped this button's trigger from the
        more-actions menu entirely - the backend endpoint above was reachable
        but nothing in the UI called it. Regression guard for the button/JS
        handler wiring, not just the endpoint's own behavior."""
        pin_list = baker.make(PinList, profile=self.profile, name="Has pins")
        PinListItem.objects.create(pin_list=pin_list, pin=_make_pin(self.profile), added_via=PinListItem.ADDED_MANUAL)
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertContains(response, "pinListCreateMarkupMap()")
        self.assertContains(response, "Create Markup Map")
        self.assertNotContains(response, 'disabled title="Add pins to this list first">\n                    <i class="material-symbols-outlined">map</i>')

    def test_create_markup_map_button_disabled_when_list_is_empty(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Empty list")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        content = response.content.decode()
        markup_btn_start = content.index("pinListCreateMarkupMap()")
        # The disabled attribute is on the same <button ...> tag as the onclick handler.
        tag_start = content.rindex("<button", 0, markup_btn_start)
        tag_end = content.index(">", markup_btn_start)
        self.assertIn("disabled", content[tag_start:tag_end])


class PinListDetailInlineEditableTests(TestCase):
    """Click-to-edit-in-place title/description on the list detail hero.

    Reuses the pre-existing PinListEditView (lists.edit) endpoint - only the
    template/JS wiring is new. name/description via that endpoint were
    exercised only incidentally by the smart-filter tests above, never with
    a dedicated "just rename it" test, so this batch adds that too.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_title_and_description_render_as_editable(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Urban Ruins", description="Places I want to explore.")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertContains(response, "pin-list-title-editable")
        self.assertContains(response, 'data-raw-name="Urban Ruins"')
        self.assertContains(response, "pin-list-description-editable")
        self.assertContains(response, 'data-raw-description="Places I want to explore."')

    def test_empty_description_shows_a_placeholder(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="No description yet", description="")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertContains(response, ">Add a description...<")

    def test_renaming_via_the_edit_endpoint(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Old Name", description="Kept as-is.")
        response = self.client.post(
            reverse("lists.edit", kwargs={"list_slug": pin_list.slug}),
            data=json.dumps({"name": "New Name"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pin_list.refresh_from_db()
        self.assertEqual(pin_list.name, "New Name")
        self.assertEqual(pin_list.description, "Kept as-is.")

    def test_updating_description_via_the_edit_endpoint(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Kept as-is", description="Old description.")
        response = self.client.post(
            reverse("lists.edit", kwargs={"list_slug": pin_list.slug}),
            data=json.dumps({"description": "New description."}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pin_list.refresh_from_db()
        self.assertEqual(pin_list.name, "Kept as-is")
        self.assertEqual(pin_list.description, "New description.")

    def test_blank_name_is_ignored_not_cleared(self) -> None:
        """The model requires a name - PinListEditView silently no-ops a blank
        one rather than erroring, matching how the inline editor reverts to
        the prior text instead of submitting an empty rename."""
        pin_list = baker.make(PinList, profile=self.profile, name="Keep Me")
        response = self.client.post(
            reverse("lists.edit", kwargs={"list_slug": pin_list.slug}),
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pin_list.refresh_from_db()
        self.assertEqual(pin_list.name, "Keep Me")


class SelectingSavedFilterImmediatelyPopulatesListTests(TestCase):
    """Picking a saved filter must show matching pins right away, not only after also enabling "is_smart"."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.matching_pin = _make_pin(self.profile, name="Rooftop Ruin", priority=5)
        self.non_matching_pin = _make_pin(self.profile, name="Basement", priority=1)
        self.saved_filter = SavedFilter.objects.create(
            profile=self.profile,
            name="High priority",
            criteria={"min_priority": 5},
        )
        self.pin_list = baker.make(PinList, profile=self.profile, name="My List")

    def test_selecting_filter_populates_matches_without_enabling_is_smart(self) -> None:
        response = self.client.post(
            reverse("lists.edit", kwargs={"list_slug": self.pin_list.slug}),
            data=json.dumps({"saved_filter_uuid": str(self.saved_filter.uuid)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.pin_list.refresh_from_db()
        self.assertFalse(self.pin_list.is_smart)
        member_pin_ids = set(self.pin_list.items.values_list("pin_id", flat=True))
        self.assertIn(self.matching_pin.pk, member_pin_ids)
        self.assertNotIn(self.non_matching_pin.pk, member_pin_ids)

    def test_clearing_the_filter_removes_previously_matched_pins(self) -> None:
        edit_url = reverse("lists.edit", kwargs={"list_slug": self.pin_list.slug})
        self.client.post(edit_url, data=json.dumps({"saved_filter_uuid": str(self.saved_filter.uuid)}), content_type="application/json")
        self.client.post(edit_url, data=json.dumps({"saved_filter_uuid": ""}), content_type="application/json")
        self.pin_list.refresh_from_db()
        self.assertEqual(self.pin_list.items.count(), 0)

    def test_turning_is_smart_off_alone_does_not_touch_existing_membership(self) -> None:
        edit_url = reverse("lists.edit", kwargs={"list_slug": self.pin_list.slug})
        self.client.post(edit_url, data=json.dumps({"saved_filter_uuid": str(self.saved_filter.uuid)}), content_type="application/json")
        self.client.post(edit_url, data=json.dumps({"is_smart": True}), content_type="application/json")
        self.pin_list.refresh_from_db()
        self.assertTrue(self.pin_list.items.filter(pin=self.matching_pin).exists())

        self.client.post(edit_url, data=json.dumps({"is_smart": False}), content_type="application/json")
        self.pin_list.refresh_from_db()
        self.assertFalse(self.pin_list.is_smart)
        # Membership is a frozen snapshot once sync is paused - still present.
        self.assertTrue(self.pin_list.items.filter(pin=self.matching_pin).exists())


class SmartListLabelChangeResyncTests(TestCase):
    """A smart list must re-sync membership when a pin's labels change, not only when the pin itself is saved."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.exclude_label = baker.make(Label, kind=KIND_TAG, profile=self.profile, name="Demolished")
        self.pin_list = baker.make(
            PinList,
            profile=self.profile,
            name="Smart Exclusions",
            is_smart=True,
            smart_filter={"exclude_tags": [self.exclude_label.pk]},
        )

    def test_pin_removed_from_list_once_it_gains_the_excluded_label(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            pin = _make_pin(self.profile, name="Old Factory")
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).exists())

        with self.captureOnCommitCallbacks(execute=True):
            pin.labels.add(self.exclude_label)

        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).exists())

    def test_pin_readded_once_the_excluded_label_is_removed(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            pin = _make_pin(self.profile, name="Old Factory")
            pin.labels.add(self.exclude_label)
        self.assertFalse(PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).exists())

        with self.captureOnCommitCallbacks(execute=True):
            pin.labels.remove(self.exclude_label)

        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).exists())

    def test_manually_added_pin_is_not_removed_by_a_later_label_change(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            pin = _make_pin(self.profile, name="Kept Manually")
            pin.labels.add(self.exclude_label)
        PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).delete()
        PinListItem.objects.create(pin_list=self.pin_list, pin=pin, added_via=PinListItem.ADDED_MANUAL)

        with self.captureOnCommitCallbacks(execute=True):
            pin.labels.remove(self.exclude_label)
            pin.labels.add(self.exclude_label)

        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list, pin=pin).exists())


class EditingSourceSavedFilterResyncsDerivedListsTests(TestCase):
    """Editing a SavedFilter must refresh and resync any PinList still pointing at it -
    otherwise a smart list silently drifts out of sync with a filter it was built from
    the moment the user tweaks that filter from the Filters tab instead of the list page."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.alpha_pin = _make_pin(self.profile, name="Alpha Ruin")
        self.beta_pin = _make_pin(self.profile, name="Beta Ruin")
        self.saved_filter = SavedFilter.objects.create(profile=self.profile, name="My Filter", criteria={"name": "Ruin"})
        self.pin_list = baker.make(PinList, profile=self.profile, name="Derived List")
        self.client.post(
            reverse("lists.edit", kwargs={"list_slug": self.pin_list.slug}),
            data=json.dumps({"saved_filter_uuid": str(self.saved_filter.uuid)}),
            content_type="application/json",
        )
        self.pin_list.refresh_from_db()

    def test_selecting_a_saved_filter_records_it_as_the_source(self) -> None:
        self.assertEqual(self.pin_list.source_saved_filter_id, self.saved_filter.pk)

    def test_widening_the_source_filters_criteria_pulls_in_newly_matching_pins(self) -> None:
        self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "My Filter", "name": "Alpha"},
        )
        self.pin_list.refresh_from_db()
        self.assertNotIn(self.beta_pin.pk, set(self.pin_list.items.values_list("pin_id", flat=True)))

        self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "My Filter", "name": "Ruin"},
        )

        self.pin_list.refresh_from_db()
        self.assertEqual(self.pin_list.smart_filter.get("name"), "Ruin")
        member_pin_ids = set(self.pin_list.items.values_list("pin_id", flat=True))
        self.assertIn(self.alpha_pin.pk, member_pin_ids)
        self.assertIn(self.beta_pin.pk, member_pin_ids)

    def test_narrowing_the_source_filter_drops_pins_that_no_longer_match(self) -> None:
        self.assertIn(self.beta_pin.pk, set(self.pin_list.items.values_list("pin_id", flat=True)))

        self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "My Filter", "name": "Alpha"},
        )

        self.pin_list.refresh_from_db()
        self.assertNotIn(self.beta_pin.pk, set(self.pin_list.items.values_list("pin_id", flat=True)))
        self.assertIn(self.alpha_pin.pk, set(self.pin_list.items.values_list("pin_id", flat=True)))

    def test_clearing_the_lists_own_filter_stops_further_propagation(self) -> None:
        self.client.post(
            reverse("lists.edit", kwargs={"list_slug": self.pin_list.slug}),
            data=json.dumps({"saved_filter_uuid": ""}),
            content_type="application/json",
        )
        self.pin_list.refresh_from_db()
        self.assertIsNone(self.pin_list.source_saved_filter_id)

        self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "My Filter", "name": "Alpha"},
        )
        self.pin_list.refresh_from_db()
        self.assertIsNone(self.pin_list.smart_filter)

    def test_a_list_manually_editing_its_own_smart_filter_is_unaffected_by_other_lists(self) -> None:
        other_list = baker.make(PinList, profile=self.profile, name="Independent List", is_smart=True, smart_filter={"name": "Gamma"})
        self.client.post(
            reverse("saved_filters.edit", kwargs={"filter_uuid": self.saved_filter.uuid}),
            {"filter_name": "My Filter", "name": "Alpha"},
        )
        other_list.refresh_from_db()
        self.assertEqual(other_list.smart_filter, {"name": "Gamma"})


class SavedFilterSuggestNameViewTests(TestCase):
    """The create/edit dialog's name auto-suggestion endpoint."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.suggest_url = reverse("saved_filters.suggest_name")

    def test_suggests_a_name_from_active_criteria(self) -> None:
        response = self.client.post(self.suggest_url, data={"min_rating": "4"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["name"], "4★+")

    def test_returns_none_when_no_criteria_are_active(self) -> None:
        response = self.client.post(self.suggest_url, data={})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIsNone(data["name"])

    def test_returns_none_on_invalid_form_data(self) -> None:
        response = self.client.post(self.suggest_url, data={"min_rating": "not-a-number"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIsNone(data["name"])


class PinListSlugTests(TestCase):
    """PinList URLs use a human-readable slug, unique per-profile (not globally).

    See PublicDashboardModel / Pin._slugify_qs for the pattern this mirrors -
    Pin is scoped the same way, since a slug only needs to be unique within
    one user's own lists, not across every user's.
    """

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_slug_is_minted_from_name_on_save(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Rooftop Ruins")
        self.assertEqual(pin_list.slug, "rooftop-ruins")

    def test_colliding_slug_base_for_same_profile_gets_a_distinct_slug(self) -> None:
        # Different names that slugify to the same base ("Ruins" / "Ruins!!!"
        # both -> "ruins") must still resolve to distinct slugs within one
        # profile, rather than the second save raising IntegrityError.
        first = baker.make(PinList, profile=self.profile, name="Ruins")
        second = baker.make(PinList, profile=self.profile, name="Ruins!!!")
        self.assertEqual(first.slug, "ruins")
        self.assertNotEqual(first.slug, second.slug)
        self.assertTrue(second.slug.startswith("ruins"))

    def test_same_name_for_different_profiles_can_share_a_slug(self) -> None:
        other_user = baker.make(User)
        other_profile = other_user.profile
        mine = baker.make(PinList, profile=self.profile, name="Bucket List")
        theirs = baker.make(PinList, profile=other_profile, name="Bucket List")
        self.assertEqual(mine.slug, theirs.slug)

    def test_detail_view_resolves_by_slug(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="My Spots")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertEqual(response.status_code, 200)

    def test_detail_view_still_resolves_legacy_uuid_urls(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="My Spots")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": str(pin_list.uuid)}))
        self.assertEqual(response.status_code, 200)

    def test_detail_view_404s_for_another_profiles_list(self) -> None:
        other_user = baker.make(User)
        other_profile = other_user.profile
        pin_list = baker.make(PinList, profile=other_profile, name="Not Yours")
        response = self.client.get(reverse("lists.detail", kwargs={"list_slug": pin_list.slug}))
        self.assertEqual(response.status_code, 404)


class PinListQuerySetTests(TestCase):
    """PinListQuerySet.for_profile()/active_smart_lists() - previously six
    call sites across the codebase each re-wrote `.filter(profile=profile)`
    (or `profile_id=`) directly."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.other_profile = self.other_user.profile

    def test_for_profile_returns_only_that_profiles_lists(self) -> None:
        mine = baker.make(PinList, profile=self.profile, name="Mine")
        baker.make(PinList, profile=self.other_profile, name="Theirs")

        result = list(PinList.objects.for_profile(self.profile))

        self.assertEqual(result, [mine])

    def test_for_profile_accepts_a_raw_profile_id(self) -> None:
        mine = baker.make(PinList, profile=self.profile, name="Mine")

        result = list(PinList.objects.for_profile(self.profile.pk))

        self.assertEqual(result, [mine])

    def test_active_smart_lists_excludes_non_smart_lists(self) -> None:
        baker.make(PinList, profile=self.profile, name="Plain List", is_smart=False)

        result = list(PinList.objects.active_smart_lists(self.profile))

        self.assertEqual(result, [])

    def test_active_smart_lists_excludes_smart_lists_with_no_rules_configured(self) -> None:
        baker.make(PinList, profile=self.profile, name="Empty Smart List", is_smart=True, smart_filter=None, smart_boundary=None)

        result = list(PinList.objects.active_smart_lists(self.profile))

        self.assertEqual(result, [])

    def test_active_smart_lists_includes_a_list_with_a_smart_filter(self) -> None:
        smart_list = baker.make(PinList, profile=self.profile, name="Filtered", is_smart=True, smart_filter={"name": "ruins"})

        result = list(PinList.objects.active_smart_lists(self.profile))

        self.assertEqual(result, [smart_list])


class PinListItemQuerySetTests(TestCase):
    """PinListItemQuerySet.for_list()/membership() - previously four call
    sites across controllers/pin_lists.py and services/pin_list_membership.py
    each re-wrote `.filter(pin_list=pin_list, ...)` directly."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin_list = baker.make(PinList, profile=self.profile, name="Mine")
        self.other_list = baker.make(PinList, profile=self.profile, name="Other")

    def test_for_list_returns_only_that_lists_items(self) -> None:
        pin = _make_pin(self.profile)
        item = PinListItem.objects.create(pin_list=self.pin_list, pin=pin)
        other_pin = _make_pin(self.profile)
        PinListItem.objects.create(pin_list=self.other_list, pin=other_pin)

        result = list(PinListItem.objects.for_list(self.pin_list))

        self.assertEqual(result, [item])

    def test_for_list_empty_for_a_list_with_no_items(self) -> None:
        self.assertEqual(list(PinListItem.objects.for_list(self.pin_list)), [])

    def test_membership_returns_the_matching_item(self) -> None:
        pin = _make_pin(self.profile)
        item = PinListItem.objects.create(pin_list=self.pin_list, pin=pin)

        self.assertEqual(PinListItem.objects.membership(self.pin_list, pin), item)

    def test_membership_none_when_the_pin_is_not_on_the_list(self) -> None:
        pin = _make_pin(self.profile)
        self.assertIsNone(PinListItem.objects.membership(self.pin_list, pin))

    def test_membership_none_when_the_pin_is_only_on_a_different_list(self) -> None:
        pin = _make_pin(self.profile)
        PinListItem.objects.create(pin_list=self.other_list, pin=pin)

        self.assertIsNone(PinListItem.objects.membership(self.pin_list, pin))
