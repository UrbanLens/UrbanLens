"""Tests for the data-consolidating pin merge and its PinMergeSuggestion review queue.

Covers:
- PinMergeSuggestion.objects.upsert - order-independent dedup of pending
  suggestions for the same pair, and the default-survivor heuristic.
- plan_merge_conflicts - only Article/Boundary/CustomFieldValue collisions
  surface as a real conflict needing a user decision.
- merge_pins - every rule in its per-relation table (safe bulk reassigns,
  auto-dedup, ask-the-user conflicts, child-pin reparenting), atomicity, and
  that the loser is gone with a PinTombstone left behind.
- accept_pin_merge_suggestion / reject_pin_merge_suggestion.
- PinMergeSuggestionActionView over HTTP - ownership, already-handled, and
  unresolved-conflict guards.
- The legacy-CID collision trigger raising exactly one suggestion, deduped
  across repeated collisions.
"""

from __future__ import annotations

from datetime import timedelta
import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.auto_removals.model import PinAutoRemoval
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldType, CustomFieldValue
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion, PinMergeSuggestionOrigin, PinMergeSuggestionStatus
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.pin_tombstone.model import PinTombstone
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import repair_legacy_pin_coordinates
from urbanlens.dashboard.services.pins.pin_merge import UnresolvedMergeConflictError, merge_pins, plan_merge_conflicts
from urbanlens.dashboard.services.pins.pin_merge_suggestions import accept_pin_merge_suggestion, reject_pin_merge_suggestion


class MergeSuggestionUpsertTests(TestCase):
    """PinMergeSuggestion.objects.upsert - order-independent dedup, default survivor."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location_a = baker.make_recipe("dashboard.location")
        self.location_b = baker.make_recipe("dashboard.location")
        self.pin_a = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location_a)
        self.pin_b = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location_b)

    def test_creates_a_pending_suggestion(self) -> None:
        suggestion = PinMergeSuggestion.objects.upsert(
            profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION,
        )
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.PENDING)
        self.assertEqual({suggestion.pin_a_id, suggestion.pin_b_id}, {self.pin_a.pk, self.pin_b.pk})

    def test_second_call_same_order_returns_existing(self) -> None:
        first = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        second = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PinMergeSuggestion.objects.count(), 1)

    def test_reversed_pair_still_dedupes(self) -> None:
        first = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        second = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_b, pin_b=self.pin_a, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PinMergeSuggestion.objects.count(), 1)

    def test_new_suggestion_created_after_prior_one_resolved(self) -> None:
        first = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        first.status = PinMergeSuggestionStatus.REJECTED
        first.save(update_fields=["status"])
        second = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(PinMergeSuggestion.objects.count(), 2)

    def test_explicit_survivor_overrides_heuristic(self) -> None:
        baker.make(PinVisit, pin=self.pin_b, source="manual")  # pin_b would win the heuristic
        suggestion = PinMergeSuggestion.objects.upsert(
            profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION, suggested_survivor=self.pin_a,
        )
        self.assertEqual(suggestion.suggested_survivor_id, self.pin_a.pk)

    def test_default_survivor_prefers_more_visits(self) -> None:
        baker.make(PinVisit, pin=self.pin_b, source="manual")
        suggestion = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        self.assertEqual(suggestion.suggested_survivor_id, self.pin_b.pk)

    def test_default_survivor_falls_back_to_older_pin(self) -> None:
        Pin.objects.filter(pk=self.pin_a.pk).update(created=self.pin_b.created - timedelta(days=5))
        self.pin_a.refresh_from_db()
        suggestion = PinMergeSuggestion.objects.upsert(profile=self.profile, pin_a=self.pin_a, pin_b=self.pin_b, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)
        self.assertEqual(suggestion.suggested_survivor_id, self.pin_a.pk)


class PlanMergeConflictsTests(TestCase):
    """plan_merge_conflicts only flags genuine Article/Boundary/CustomFieldValue collisions."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin_a = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.pin_b = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_no_conflicts_by_default(self) -> None:
        self.assertEqual(plan_merge_conflicts(self.pin_a, self.pin_b), [])

    def test_article_conflict_only_when_both_have_one(self) -> None:
        Article.objects.create(pin=self.pin_a, content="A's article")
        self.assertEqual(plan_merge_conflicts(self.pin_a, self.pin_b), [])
        Article.objects.create(pin=self.pin_b, content="B's article")
        conflicts = plan_merge_conflicts(self.pin_a, self.pin_b)
        self.assertEqual([c.key for c in conflicts], ["article"])

    def test_boundary_conflict_only_for_matching_type(self) -> None:
        baker.make(Boundary, pin=self.pin_a, location=self.pin_a.location, profile=self.profile, boundary_type="property")
        baker.make(Boundary, pin=self.pin_b, location=self.pin_b.location, profile=self.profile, boundary_type="building")
        self.assertEqual(plan_merge_conflicts(self.pin_a, self.pin_b), [])
        baker.make(Boundary, pin=self.pin_b, location=self.pin_b.location, profile=self.profile, boundary_type="property")
        conflicts = plan_merge_conflicts(self.pin_a, self.pin_b)
        self.assertEqual([c.key for c in conflicts], ["boundary:property"])

    def test_custom_field_conflict_only_for_shared_field(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT)
        other_field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Gate Code", field_type=CustomFieldType.TEXT)
        CustomFieldValue.objects.create(field=field, pin=self.pin_a, value_text="Good")
        CustomFieldValue.objects.create(field=other_field, pin=self.pin_b, value_text="1234")
        self.assertEqual(plan_merge_conflicts(self.pin_a, self.pin_b), [])
        CustomFieldValue.objects.create(field=field, pin=self.pin_b, value_text="Fair")
        conflicts = plan_merge_conflicts(self.pin_a, self.pin_b)
        self.assertEqual([c.key for c in conflicts], [f"custom_field:{field.pk}"])

    def test_multiple_simultaneous_conflicts(self) -> None:
        Article.objects.create(pin=self.pin_a, content="A")
        Article.objects.create(pin=self.pin_b, content="B")
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT)
        CustomFieldValue.objects.create(field=field, pin=self.pin_a, value_text="Good")
        CustomFieldValue.objects.create(field=field, pin=self.pin_b, value_text="Fair")
        keys = {c.key for c in plan_merge_conflicts(self.pin_a, self.pin_b)}
        self.assertEqual(keys, {"article", f"custom_field:{field.pk}"})


class MergePinsTests(TestCase):
    """merge_pins - the full per-relation rule table."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other_user = baker.make(User)
        self.survivor = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.loser = baker.make_recipe("dashboard.pin", profile=self.profile)

    def test_rejects_merging_a_pin_into_itself(self) -> None:
        with self.assertRaises(ValueError):
            merge_pins(self.survivor, self.survivor, self.profile)

    def test_rejects_pins_from_a_different_profile(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin", profile=self.other_user.profile)
        with self.assertRaises(ValueError):
            merge_pins(self.survivor, other_pin, self.profile)

    def test_safe_bulk_reassigns_land_on_survivor(self) -> None:
        visit = baker.make(PinVisit, pin=self.loser, source="manual")
        image = baker.make(Image, pin=self.loser)
        link = PinLink.objects.create(pin=self.loser, name="Website", url="https://example.com")
        note = PinNote.objects.create(pin=self.loser, text="A note")
        trip = baker.make_recipe("dashboard.trip", creator=self.profile)
        activity = baker.make_recipe("dashboard.trip_activity", trip=trip, pin=self.loser)
        comment = baker.make_recipe("dashboard.comment", pin=self.loser, profile=self.profile)
        markup_map = baker.make(MarkupMap, pin=self.loser)
        markup_item = baker.make_recipe("dashboard.pin_markup", parent_pin=self.loser)
        property_sale = PinPropertySale.objects.create(pin=self.loser)
        other_field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Related", field_type=CustomFieldType.REFERENCE)
        ref_value = CustomFieldValue.objects.create(field=other_field, pin=self.survivor, ref_pin=self.loser)
        other_suggestion = PinSuggestion.objects.create(profile=self.profile, pin=self.loser, latitude="1.0", longitude="1.0", origin=PinSuggestionOrigin.IMMICH)
        label = baker.make("dashboard.Label", profile=self.profile, kind="tag", name="Neat")
        self.loser.labels.add(label)

        merge_pins(self.survivor, self.loser, self.profile)

        visit.refresh_from_db()
        self.assertEqual(visit.pin_id, self.survivor.pk)
        image.refresh_from_db()
        self.assertEqual(image.pin_id, self.survivor.pk)
        link.refresh_from_db()
        self.assertEqual(link.pin_id, self.survivor.pk)
        note.refresh_from_db()
        self.assertEqual(note.pin_id, self.survivor.pk)
        activity.refresh_from_db()
        self.assertEqual(activity.pin_id, self.survivor.pk)
        comment.refresh_from_db()
        self.assertEqual(comment.pin_id, self.survivor.pk)
        markup_map.refresh_from_db()
        self.assertEqual(markup_map.pin_id, self.survivor.pk)
        markup_item.refresh_from_db()
        self.assertEqual(markup_item.parent_pin_id, self.survivor.pk)
        property_sale.refresh_from_db()
        self.assertEqual(property_sale.pin_id, self.survivor.pk)
        ref_value.refresh_from_db()
        self.assertEqual(ref_value.ref_pin_id, self.survivor.pk)
        other_suggestion.refresh_from_db()
        self.assertEqual(other_suggestion.pin_id, self.survivor.pk)
        self.assertIn(label, self.survivor.labels.all())

    def test_inferred_pins_m2m_moves_to_survivor(self) -> None:
        markup_map = baker.make(MarkupMap)
        markup_map.inferred_pins.add(self.loser)
        merge_pins(self.survivor, self.loser, self.profile)
        markup_map.refresh_from_db()
        self.assertIn(self.survivor, markup_map.inferred_pins.all())

    def test_duplicate_alias_is_deduped_distinct_one_is_kept(self) -> None:
        PinAlias.objects.create(pin=self.survivor, name="Old Mill")
        PinAlias.objects.create(pin=self.loser, name="old mill")  # case-insensitive duplicate
        PinAlias.objects.create(pin=self.loser, name="The Mill")  # distinct
        loser_pk = self.loser.pk  # merge_pins deletes loser, clearing self.loser.pk

        merge_pins(self.survivor, self.loser, self.profile)

        names = {alias.name.casefold() for alias in self.survivor.aliases.all()}
        self.assertIn("old mill", names)
        self.assertIn("the mill", names)
        self.assertEqual(PinAlias.objects.filter(pin_id=loser_pk).count(), 0)

    def test_duplicate_owner_is_deduped(self) -> None:
        PinOwner.objects.create(pin=self.survivor, name="Jane Doe")
        PinOwner.objects.create(pin=self.loser, name="jane doe")
        PinOwner.objects.create(pin=self.loser, name="John Smith")

        merge_pins(self.survivor, self.loser, self.profile)

        names = {owner.name.casefold() for owner in self.survivor.owners.all()}
        self.assertEqual(names, {"jane doe", "john smith"})

    def test_duplicate_auto_removal_is_deduped(self) -> None:
        PinAutoRemoval.objects.create(pin=self.survivor, kind="alias", value="unwanted")
        PinAutoRemoval.objects.create(pin=self.loser, kind="alias", value="unwanted")
        PinAutoRemoval.objects.create(pin=self.loser, kind="link", value="https://spam.example.com")

        merge_pins(self.survivor, self.loser, self.profile)

        keys = set(PinAutoRemoval.objects.filter(pin=self.survivor).values_list("kind", "value"))
        self.assertEqual(keys, {("alias", "unwanted"), ("link", "https://spam.example.com")})

    def test_duplicate_pending_share_to_same_recipient_is_deduped(self) -> None:
        recipient = baker.make(User).profile
        PinShare.objects.create(pin=self.survivor, from_profile=self.profile, to_profile=recipient, status=PinShareStatus.PENDING)
        PinShare.objects.create(pin=self.loser, from_profile=self.profile, to_profile=recipient, status=PinShareStatus.PENDING)

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(PinShare.objects.filter(pin=self.survivor, to_profile=recipient, status=PinShareStatus.PENDING).count(), 1)

    def test_share_to_a_different_recipient_is_reassigned_not_dropped(self) -> None:
        recipient_a = baker.make(User).profile
        recipient_b = baker.make(User).profile
        PinShare.objects.create(pin=self.survivor, from_profile=self.profile, to_profile=recipient_a, status=PinShareStatus.PENDING)
        share_b = PinShare.objects.create(pin=self.loser, from_profile=self.profile, to_profile=recipient_b, status=PinShareStatus.PENDING)

        merge_pins(self.survivor, self.loser, self.profile)

        share_b.refresh_from_db()
        self.assertEqual(share_b.pin_id, self.survivor.pk)

    def test_duplicate_list_membership_is_deduped(self) -> None:
        pin_list = baker.make(PinList, profile=self.profile, name="Favorites")
        PinListItem.objects.create(pin_list=pin_list, pin=self.survivor)
        PinListItem.objects.create(pin_list=pin_list, pin=self.loser)
        loser_pk = self.loser.pk  # merge_pins deletes loser, clearing self.loser.pk

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(PinListItem.objects.filter(pin_list=pin_list, pin=self.survivor).count(), 1)
        self.assertEqual(PinListItem.objects.filter(pin_id=loser_pk).count(), 0)

    def test_membership_in_a_different_list_is_reassigned(self) -> None:
        list_a = baker.make(PinList, profile=self.profile, name="List A")
        list_b = baker.make(PinList, profile=self.profile, name="List B")
        PinListItem.objects.create(pin_list=list_a, pin=self.survivor)
        item_b = PinListItem.objects.create(pin_list=list_b, pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile)

        item_b.refresh_from_db()
        self.assertEqual(item_b.pin_id, self.survivor.pk)

    def test_review_from_same_profile_keeps_the_more_recently_updated(self) -> None:
        reviewer = baker.make(User).profile
        older = Review.objects.create(pin=self.survivor, profile=reviewer, rating=2)
        Review.objects.filter(pk=older.pk).update(updated=older.updated - timedelta(days=10))
        newer = Review.objects.create(pin=self.loser, profile=reviewer, rating=5)
        loser_pk = self.loser.pk  # merge_pins deletes loser, clearing self.loser.pk

        merge_pins(self.survivor, self.loser, self.profile)

        survivor_review = Review.objects.get(pin=self.survivor, profile=reviewer)
        self.assertEqual(survivor_review.rating, newer.rating)
        self.assertFalse(Review.objects.filter(pin_id=loser_pk).exists())

    def test_review_from_a_different_profile_is_reassigned(self) -> None:
        other_reviewer = baker.make(User).profile
        review = Review.objects.create(pin=self.loser, profile=other_reviewer, rating=4)

        merge_pins(self.survivor, self.loser, self.profile)

        review.refresh_from_db()
        self.assertEqual(review.pin_id, self.survivor.pk)

    def test_article_conflict_without_resolution_raises(self) -> None:
        Article.objects.create(pin=self.survivor, content="Survivor's")
        Article.objects.create(pin=self.loser, content="Loser's")
        with self.assertRaises(UnresolvedMergeConflictError):
            merge_pins(self.survivor, self.loser, self.profile)
        # nothing should have been touched
        self.assertTrue(Pin.objects.filter(pk=self.loser.pk).exists())

    def test_article_conflict_resolved_to_loser_replaces_survivors(self) -> None:
        Article.objects.create(pin=self.survivor, content="Survivor's")
        loser_article = Article.objects.create(pin=self.loser, content="Loser's")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.loser.pk})

        survivor_article = Article.objects.get(pin=self.survivor)
        self.assertEqual(survivor_article.pk, loser_article.pk)
        self.assertEqual(survivor_article.content, "Loser's")

    def test_article_conflict_resolved_to_survivor_drops_losers(self) -> None:
        survivor_article = Article.objects.create(pin=self.survivor, content="Survivor's")
        Article.objects.create(pin=self.loser, content="Loser's")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.survivor.pk})

        remaining = Article.objects.get(pin=self.survivor)
        self.assertEqual(remaining.pk, survivor_article.pk)
        self.assertEqual(Article.objects.count(), 1)

    def test_only_losers_article_is_reassigned_without_conflict(self) -> None:
        loser_article = Article.objects.create(pin=self.loser, content="Loser's")
        merge_pins(self.survivor, self.loser, self.profile)
        loser_article.refresh_from_db()
        self.assertEqual(loser_article.pin_id, self.survivor.pk)

    def test_boundary_conflict_without_resolution_raises(self) -> None:
        baker.make(Boundary, pin=self.survivor, location=self.survivor.location, profile=self.profile, boundary_type="property")
        baker.make(Boundary, pin=self.loser, location=self.loser.location, profile=self.profile, boundary_type="property")
        with self.assertRaises(UnresolvedMergeConflictError):
            merge_pins(self.survivor, self.loser, self.profile)

    def test_boundary_conflict_resolved_to_loser(self) -> None:
        baker.make(Boundary, pin=self.survivor, location=self.survivor.location, profile=self.profile, boundary_type="property")
        loser_boundary = baker.make(Boundary, pin=self.loser, location=self.loser.location, profile=self.profile, boundary_type="property")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"boundary:property": self.loser.pk})

        remaining = Boundary.objects.get(pin=self.survivor, boundary_type="property")
        self.assertEqual(remaining.pk, loser_boundary.pk)

    def test_custom_field_conflict_without_resolution_raises(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT)
        CustomFieldValue.objects.create(field=field, pin=self.survivor, value_text="Good")
        CustomFieldValue.objects.create(field=field, pin=self.loser, value_text="Fair")
        with self.assertRaises(UnresolvedMergeConflictError):
            merge_pins(self.survivor, self.loser, self.profile)

    def test_custom_field_conflict_resolved_to_loser(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT)
        CustomFieldValue.objects.create(field=field, pin=self.survivor, value_text="Good")
        CustomFieldValue.objects.create(field=field, pin=self.loser, value_text="Fair")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={f"custom_field:{field.pk}": self.loser.pk})

        remaining = CustomFieldValue.objects.get(field=field, pin=self.survivor)
        self.assertEqual(remaining.value_text, "Fair")

    def test_child_pins_are_reparented_onto_survivor(self) -> None:
        child = baker.make_recipe("dashboard.pin", profile=self.profile, parent_pin=self.loser)
        merge_pins(self.survivor, self.loser, self.profile)
        child.refresh_from_db()
        self.assertEqual(child.parent_pin_id, self.survivor.pk)

    def test_child_pin_that_would_create_a_cycle_is_detached_to_root(self) -> None:
        # survivor is nested under loser's own child - re-parenting that child
        # under survivor would make survivor its own ancestor. The child must
        # be detached to root rather than left pointed at loser, or deleting
        # loser would CASCADE-delete the child - and survivor beneath it.
        child = baker.make_recipe("dashboard.pin", profile=self.profile, parent_pin=self.loser)
        self.survivor.parent_pin = child
        self.survivor.save(update_fields=["parent_pin"])

        merge_pins(self.survivor, self.loser, self.profile)

        child.refresh_from_db()
        self.assertIsNone(child.parent_pin_id)
        self.assertTrue(Pin.objects.filter(pk=self.survivor.pk).exists())

    def test_lineage_gap_filled_from_loser_when_survivor_has_none(self) -> None:
        recipient = baker.make(User).profile
        share = PinShare.objects.create(pin=None, from_profile=self.profile, to_profile=recipient, status=PinShareStatus.ACCEPTED)
        self.loser.source_share = share
        self.loser.save(update_fields=["source_share"])

        merge_pins(self.survivor, self.loser, self.profile)

        self.survivor.refresh_from_db()
        self.assertEqual(self.survivor.source_share_id, share.pk)

    def test_lineage_not_overwritten_when_survivor_already_has_one(self) -> None:
        recipient = baker.make(User).profile
        survivor_share = PinShare.objects.create(pin=None, from_profile=self.profile, to_profile=recipient, status=PinShareStatus.ACCEPTED)
        loser_share = PinShare.objects.create(pin=None, from_profile=self.profile, to_profile=recipient, status=PinShareStatus.ACCEPTED)
        self.survivor.source_share = survivor_share
        self.survivor.save(update_fields=["source_share"])
        self.loser.source_share = loser_share
        self.loser.save(update_fields=["source_share"])

        merge_pins(self.survivor, self.loser, self.profile)

        self.survivor.refresh_from_db()
        self.assertEqual(self.survivor.source_share_id, survivor_share.pk)

    def test_loser_is_deleted_and_tombstoned(self) -> None:
        loser_uuid = self.loser.uuid
        merge_pins(self.survivor, self.loser, self.profile)
        self.assertFalse(Pin.objects.filter(pk=self.loser.pk).exists())
        self.assertTrue(PinTombstone.objects.filter(pin_uuid=loser_uuid).exists())

    def test_other_pending_suggestion_mentioning_loser_is_repointed(self) -> None:
        third_pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        other_suggestion = PinMergeSuggestion.objects.upsert(
            profile=self.profile, pin_a=self.loser, pin_b=third_pin, origin=PinMergeSuggestionOrigin.LEGACY_CID_COLLISION,
        )

        merge_pins(self.survivor, self.loser, self.profile)

        other_suggestion.refresh_from_db()
        self.assertEqual({other_suggestion.pin_a_id, other_suggestion.pin_b_id}, {self.survivor.pk, third_pin.pk})

    def test_atomicity_nothing_persists_when_a_merge_step_fails(self) -> None:
        PinAlias.objects.create(pin=self.loser, name="Should not move")
        with mock.patch("urbanlens.dashboard.services.pins.pin_merge._merge_lineage", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            merge_pins(self.survivor, self.loser, self.profile)

        # The alias reassignment that ran before the forced failure must have
        # been rolled back along with everything else in the same transaction.
        self.assertTrue(Pin.objects.filter(pk=self.loser.pk).exists())
        self.assertTrue(PinAlias.objects.filter(pin=self.loser, name="Should not move").exists())
        self.assertFalse(PinAlias.objects.filter(pin=self.survivor, name="Should not move").exists())


class AcceptRejectPinMergeSuggestionTests(TestCase):
    """accept_pin_merge_suggestion / reject_pin_merge_suggestion."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin_a = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.pin_b = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _suggestion(self, **kwargs) -> PinMergeSuggestion:
        defaults = {
            "profile": self.profile,
            "pin_a": self.pin_a,
            "pin_b": self.pin_b,
            "origin": PinMergeSuggestionOrigin.LEGACY_CID_COLLISION,
            "suggested_survivor": self.pin_a,
        }
        defaults.update(kwargs)
        return PinMergeSuggestion.objects.create(**defaults)

    def test_accept_defaults_to_suggested_survivor(self) -> None:
        suggestion = self._suggestion()
        merged = accept_pin_merge_suggestion(suggestion, self.profile)
        self.assertEqual(merged.pk, self.pin_a.pk)
        self.assertFalse(Pin.objects.filter(pk=self.pin_b.pk).exists())
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.ACCEPTED)

    def test_accept_honors_explicit_survivor_override(self) -> None:
        suggestion = self._suggestion()  # suggested_survivor is pin_a
        merged = accept_pin_merge_suggestion(suggestion, self.profile, survivor_pk=self.pin_b.pk)
        self.assertEqual(merged.pk, self.pin_b.pk)
        self.assertFalse(Pin.objects.filter(pk=self.pin_a.pk).exists())

    def test_accept_rejects_a_survivor_pk_outside_the_pair(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        suggestion = self._suggestion()
        with self.assertRaises(ValueError):
            accept_pin_merge_suggestion(suggestion, self.profile, survivor_pk=other_pin.pk)

    def test_reject_touches_neither_pin(self) -> None:
        suggestion = self._suggestion()
        reject_pin_merge_suggestion(suggestion)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.REJECTED)
        self.assertTrue(Pin.objects.filter(pk=self.pin_a.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())


class PinMergeSuggestionActionViewTests(TestCase):
    """Accept/reject over HTTP, with ownership, already-handled, and conflict guards."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin_a = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.pin_b = baker.make_recipe("dashboard.pin", profile=self.profile)

    def _suggestion(self, **kwargs) -> PinMergeSuggestion:
        defaults = {
            "profile": self.profile,
            "pin_a": self.pin_a,
            "pin_b": self.pin_b,
            "origin": PinMergeSuggestionOrigin.LEGACY_CID_COLLISION,
            "suggested_survivor": self.pin_a,
        }
        defaults.update(kwargs)
        return PinMergeSuggestion.objects.create(**defaults)

    def test_accept_merges_and_returns_200(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "accept"]), {"survivor_pk": str(self.pin_a.pk)})
        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.ACCEPTED)
        self.assertFalse(Pin.objects.filter(pk=self.pin_b.pk).exists())

    def test_reject_dismisses_without_merging(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "reject"]))
        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.REJECTED)
        self.assertTrue(Pin.objects.filter(pk=self.pin_a.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())

    def test_unknown_action_is_404(self) -> None:
        suggestion = self._suggestion()
        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "explode"]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_act_on_another_profiles_suggestion(self) -> None:
        other = baker.make(User)
        other_pin_a = baker.make_recipe("dashboard.pin", profile=other.profile)
        other_pin_b = baker.make_recipe("dashboard.pin", profile=other.profile)
        suggestion = self._suggestion(profile=other.profile, pin_a=other_pin_a, pin_b=other_pin_b, suggested_survivor=other_pin_a)
        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "accept"]))
        self.assertEqual(response.status_code, 404)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.PENDING)

    def test_already_handled_suggestion_is_a_noop(self) -> None:
        suggestion = self._suggestion(status=PinMergeSuggestionStatus.ACCEPTED)
        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "accept"]))
        self.assertEqual(response.status_code, 200)
        # Both pins should still exist - nothing to merge, already handled.
        self.assertTrue(Pin.objects.filter(pk=self.pin_a.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())

    def test_missing_conflict_resolution_re_renders_card_instead_of_erroring(self) -> None:
        Article.objects.create(pin=self.pin_a, content="A's article")
        Article.objects.create(pin=self.pin_b, content="B's article")
        suggestion = self._suggestion()

        response = self.client.post(reverse("memories.locations.merge.action", args=[suggestion.pk, "accept"]), {"survivor_pk": str(self.pin_a.pk)})

        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.PENDING)
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["level"], "error")

    def test_accept_with_conflict_resolution_succeeds(self) -> None:
        Article.objects.create(pin=self.pin_a, content="A's article")
        Article.objects.create(pin=self.pin_b, content="B's article")
        suggestion = self._suggestion()

        response = self.client.post(
            reverse("memories.locations.merge.action", args=[suggestion.pk, "accept"]),
            {"survivor_pk": str(self.pin_a.pk), "resolution__article": str(self.pin_a.pk)},
        )

        self.assertEqual(response.status_code, 200)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinMergeSuggestionStatus.ACCEPTED)
        self.assertFalse(Pin.objects.filter(pk=self.pin_b.pk).exists())


class LegacyCidCollisionTriggerTests(TestCase):
    """repair_legacy_pin_coordinates raises exactly one deduped PinMergeSuggestion on collision."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile

    def _setup_collision(self):
        """Two pins: a pre-cutoff legacy pin with a CID, and one already at the corrected location."""
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.services.apis.locations.legacy_cid_coordinate_fix import LEGACY_COORDINATE_CUTOFF

        wrong_location = baker.make_recipe("dashboard.location", latitude="42.500000", longitude="-73.500000")
        with mock.patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None):
            wrong_location.cid = 12345
        legacy_pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=wrong_location, name="Old Tower")
        Pin.objects.filter(pk=legacy_pin.pk).update(created=LEGACY_COORDINATE_CUTOFF - timedelta(days=30))
        legacy_pin.refresh_from_db()

        correct_location, _ = Location.objects.get_nearby_or_create(42.600000, -73.600000)
        existing_pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=correct_location, name="Correct Tower")
        return legacy_pin, existing_pin

    def test_collision_raises_a_merge_suggestion_with_the_correct_pin_as_survivor(self) -> None:
        legacy_pin, existing_pin = self._setup_collision()

        result = repair_legacy_pin_coordinates(profile=self.profile, cid=12345, name="Old Tower", latitude=42.600000, longitude=-73.600000)

        self.assertIsNone(result)
        suggestion = PinMergeSuggestion.objects.get(profile=self.profile)
        self.assertEqual({suggestion.pin_a_id, suggestion.pin_b_id}, {legacy_pin.pk, existing_pin.pk})
        self.assertEqual(suggestion.suggested_survivor_id, existing_pin.pk)
        self.assertEqual(suggestion.origin, PinMergeSuggestionOrigin.LEGACY_CID_COLLISION)

    def test_repeated_collision_does_not_duplicate_the_suggestion(self) -> None:
        self._setup_collision()

        repair_legacy_pin_coordinates(profile=self.profile, cid=12345, name="Old Tower", latitude=42.600000, longitude=-73.600000)
        repair_legacy_pin_coordinates(profile=self.profile, cid=12345, name="Old Tower", latitude=42.600000, longitude=-73.600000)

        self.assertEqual(PinMergeSuggestion.objects.filter(profile=self.profile).count(), 1)
