"""
merge_pins keeps what the merged-away pin held, even where the survivor has only one slot for it.
Each test here covers something an earlier merge deleted outright: the losing article, boundary and custom field value, the loser's own columns, a duplicate owner's details and sales, a duplicate share's message and bundle - and the automatic visit that crashed the merge instead."""

from __future__ import annotations

from datetime import date, timedelta
import inspect
import re

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.custom_fields.model import (
    CustomField,
    CustomFieldEntity,
    CustomFieldType,
    CustomFieldValue,
)
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.models.markup.model import PinMarkup
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_NOTE_LENGTH
from urbanlens.dashboard.services.pins import pin_merge
from urbanlens.dashboard.services.pins.pin_merge import PIN_FIELD_POLICY, merge_pins
from urbanlens.dashboard.services.wiki.articles import save_article


def _square(lng: float, lat: float, size: float = 0.001) -> MultiPolygon:
    """A small square outline at (lng, lat)."""
    ring = ((lng, lat), (lng + size, lat), (lng + size, lat + size), (lng, lat + size), (lng, lat))
    return MultiPolygon(Polygon(ring), srid=4326)


class _MergeTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.survivor = baker.make_recipe("dashboard.pin", profile=self.profile, name="Old Mill")
        self.loser = baker.make_recipe("dashboard.pin", profile=self.profile, name="Grist Works")

    def merge_note(self) -> str:
        """Every merge note on the survivor, joined."""
        return "\n".join(PinNote.objects.filter(pin=self.survivor).values_list("text", flat=True))


class ArticleIsKeptAsHistoryTests(_MergeTestCase):
    def test_the_losing_articles_text_and_revisions_move_into_the_kept_articles_history(self) -> None:
        save_article(editor=self.profile, content="Survivor's text", pin=self.survivor)
        save_article(editor=self.profile, content="Loser's first draft", pin=self.loser)
        save_article(editor=self.profile, content="Loser's final text", pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.survivor.pk})

        article = Article.objects.get(pin=self.survivor)
        self.assertEqual(article.content, "Survivor's text")
        history = list(article.revisions.values_list("content", flat=True))
        self.assertIn("Loser's first draft", history)
        self.assertIn("Loser's final text", history)
        self.assertEqual(Article.objects.count(), 1)

    def test_live_text_with_no_revision_is_kept(self) -> None:
        """An article written directly (an import) has text no revision carries."""
        Article.objects.create(pin=self.survivor, content="Survivor's")
        Article.objects.create(pin=self.loser, content="Imported text only")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.survivor.pk})

        self.assertTrue(
            ArticleRevision.objects.filter(article__pin=self.survivor, content="Imported text only").exists()
        )

    def test_choosing_the_losers_article_keeps_the_survivors_in_history(self) -> None:
        save_article(editor=self.profile, content="Survivor's text", pin=self.survivor)
        save_article(editor=self.profile, content="Loser's text", pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.loser.pk})

        article = Article.objects.get(pin=self.survivor)
        self.assertEqual(article.content, "Loser's text")
        self.assertTrue(article.revisions.filter(content="Survivor's text").exists())

    def test_the_newest_revision_is_the_live_text(self) -> None:
        """Adopted revisions interleave by date; the history's top entry must still be what the page shows."""
        save_article(editor=self.profile, content="Survivor's text", pin=self.survivor)
        save_article(editor=self.profile, content="Loser's newer text", pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"article": self.survivor.pk})

        newest = ArticleRevision.objects.filter(article__pin=self.survivor).order_by("-created", "-pk").first()
        self.assertEqual(newest.content, "Survivor's text")
        self.assertEqual(newest.editor_id, self.profile.pk)
        self.assertIn("Article (Grist Works): kept in this pin's article history", self.merge_note())


class BoundaryIsKeptAsMarkupTests(_MergeTestCase):
    def _boundary(self, pin: Pin, polygon: MultiPolygon) -> Boundary:
        return baker.make(
            Boundary, pin=pin, location=pin.location, profile=self.profile, boundary_type="property", polygon=polygon
        )

    def test_the_outline_not_chosen_becomes_a_markup_polygon_on_the_survivor(self) -> None:
        self._boundary(self.survivor, _square(-90.0, 40.0))
        self._boundary(self.loser, _square(-90.5, 40.5))

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"boundary:property": self.survivor.pk})

        markup = PinMarkup.objects.get(parent_pin=self.survivor)
        self.assertEqual(markup.markup_type, MarkupType.POLYGON)
        self.assertEqual(markup.geometry["type"], "Polygon")
        self.assertAlmostEqual(markup.geometry["coordinates"][0][0][0], -90.5)
        self.assertIn("Grist Works", markup.label)
        self.assertEqual(Boundary.objects.get(pin=self.survivor).polygon.coords, _square(-90.0, 40.0).coords)

    def test_choosing_the_losers_outline_keeps_the_survivors_as_markup(self) -> None:
        self._boundary(self.survivor, _square(-90.0, 40.0))
        self._boundary(self.loser, _square(-90.5, 40.5))

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"boundary:property": self.loser.pk})

        markup = PinMarkup.objects.get(parent_pin=self.survivor)
        self.assertAlmostEqual(markup.geometry["coordinates"][0][0][0], -90.0)
        self.assertIn("Old Mill", markup.label)

    def test_each_part_of_a_multipart_outline_is_kept(self) -> None:
        self._boundary(self.survivor, _square(-90.0, 40.0))
        self._boundary(self.loser, MultiPolygon(*_square(-91.0, 41.0), *_square(-92.0, 42.0), srid=4326))

        merge_pins(self.survivor, self.loser, self.profile, resolutions={"boundary:property": self.survivor.pk})

        self.assertEqual(PinMarkup.objects.filter(parent_pin=self.survivor).count(), 2)


class CustomFieldValueIsNotedTests(_MergeTestCase):
    def test_the_value_not_chosen_is_written_into_the_merge_note(self) -> None:
        field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT
        )
        CustomFieldValue.objects.create(field=field, pin=self.survivor, value_text="Good")
        CustomFieldValue.objects.create(field=field, pin=self.loser, value_text="Collapsing roof")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={f"custom_field:{field.pk}": self.survivor.pk})

        self.assertEqual(CustomFieldValue.objects.get(field=field, pin=self.survivor).value_text, "Good")
        self.assertIn("Condition (Grist Works): Collapsing roof", self.merge_note())

    def test_the_same_value_on_both_writes_no_note(self) -> None:
        field = CustomField.objects.create(
            profile=self.profile, entity_type=CustomFieldEntity.PIN, name="Condition", field_type=CustomFieldType.TEXT
        )
        CustomFieldValue.objects.create(field=field, pin=self.survivor, value_text="Good")
        CustomFieldValue.objects.create(field=field, pin=self.loser, value_text="Good")

        merge_pins(self.survivor, self.loser, self.profile, resolutions={f"custom_field:{field.pk}": self.survivor.pk})

        self.assertFalse(PinNote.objects.filter(pin=self.survivor).exists())


class PinColumnsAreKeptTests(_MergeTestCase):
    def test_every_pin_column_has_a_merge_policy(self) -> None:
        """A column added later must be decided on, not silently deleted with the loser."""
        columns = {field.name for field in Pin._meta.concrete_fields}
        self.assertEqual(sorted(columns - set(PIN_FIELD_POLICY)), [])
        self.assertEqual(sorted(set(PIN_FIELD_POLICY) - columns), [])

    def test_a_blank_survivor_column_takes_the_losers_value(self) -> None:
        Pin.objects.filter(pk=self.loser.pk).update(
            description="Loser's notes", date_built=date(1901, 1, 1), fences="some", icon="factory"
        )
        Pin.objects.filter(pk=self.survivor.pk).update(description="", date_built=None, fences="unknown", icon=None)
        self.loser.refresh_from_db()
        self.survivor.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(self.survivor.description, "Loser's notes")
        self.assertEqual(self.survivor.date_built, date(1901, 1, 1))
        self.assertEqual(self.survivor.fences, "some")
        self.assertEqual(self.survivor.icon, "factory")
        self.assertFalse(PinNote.objects.filter(pin=self.survivor).exists())

    def test_a_differing_loser_value_is_written_into_the_merge_note(self) -> None:
        Pin.objects.filter(pk=self.survivor.pk).update(description="Survivor's notes", date_abandoned=date(1980, 1, 1))
        Pin.objects.filter(pk=self.loser.pk).update(
            description="Loser's\nmultiline notes", date_abandoned=date(1975, 6, 1)
        )
        self.loser.refresh_from_db()
        self.survivor.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(self.survivor.description, "Survivor's notes")
        note = self.merge_note()
        self.assertIn("Loser's\nmultiline notes", note)
        self.assertIn("1975-06-01", note)
        self.assertIn("Merged Grist Works into Old Mill", note)

    def test_a_value_longer_than_one_note_continues_in_the_next_without_losing_a_character(self) -> None:
        """A 50,000-character description fills a note by itself; slicing it to fit dropped its tail."""
        Pin.objects.filter(pk=self.survivor.pk).update(description="Survivor's notes")
        Pin.objects.filter(pk=self.loser.pk).update(description="Z" * MAX_PIN_NOTE_LENGTH)
        self.loser.refresh_from_db()
        self.survivor.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        notes = list(PinNote.objects.filter(pin=self.survivor).values_list("text", flat=True))
        self.assertGreater(len(notes), 1)
        self.assertTrue(all(len(text) <= MAX_PIN_NOTE_LENGTH for text in notes))
        self.assertEqual(sum(text.count("Z") for text in notes), MAX_PIN_NOTE_LENGTH)

    def test_the_higher_danger_and_priority_are_kept_and_the_lower_noted(self) -> None:
        Pin.objects.filter(pk=self.survivor.pk).update(danger=1, priority=4)
        Pin.objects.filter(pk=self.loser.pk).update(danger=3, priority=2)
        self.loser.refresh_from_db()
        self.survivor.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual((self.survivor.danger, self.survivor.priority), (3, 4))
        note = self.merge_note()
        self.assertIn("Danger (Old Mill): 1", note)
        self.assertIn("Priority (Grist Works): 2", note)

    def test_a_user_chosen_pin_type_beats_an_automatic_one(self) -> None:
        Pin.objects.filter(pk=self.survivor.pk).update(pin_type=PinType.BUILDING, pin_type_is_user_provided=False)
        Pin.objects.filter(pk=self.loser.pk).update(pin_type=PinType.PARCEL, pin_type_is_user_provided=True)
        self.loser.refresh_from_db()
        self.survivor.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(self.survivor.pin_type, PinType.PARCEL)
        self.assertTrue(self.survivor.pin_type_is_user_provided)

    def test_the_losers_name_is_kept_as_an_alias(self) -> None:
        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(self.survivor.name, "Old Mill")
        self.assertTrue(PinAlias.objects.filter(pin=self.survivor, name__iexact="Grist Works").exists())

    def test_a_declined_restructure_offer_stays_declined(self) -> None:
        Pin.objects.filter(pk=self.loser.pk).update(restructure_offer_dismissed=True)
        self.loser.refresh_from_db()

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertTrue(self.survivor.restructure_offer_dismissed)


class AutomaticVisitOnTheSameDayTests(_MergeTestCase):
    """``db_pv_one_geo_per_pin_day`` made merging two pins with a geolocation visit on the same UTC day crash."""

    def test_two_automatic_visits_on_one_day_fold_into_one(self) -> None:
        day = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=3)
        friend = baker.make(User).profile
        kept = baker.make(
            PinVisit, pin=self.survivor, source=VisitSource.GEOLOCATION, visited_at=day, notes="Survivor's notes"
        )
        folded = baker.make(
            PinVisit,
            pin=self.loser,
            source=VisitSource.GEOLOCATION,
            visited_at=day - timedelta(hours=2),
            notes="Loser's notes",
        )
        folded.participants.add(friend)
        photo = baker.make(Image, profile=self.profile, visit=folded)

        merge_pins(self.survivor, self.loser, self.profile)

        visit = PinVisit.objects.get(pin=self.survivor, source=VisitSource.GEOLOCATION)
        self.assertEqual(visit.pk, kept.pk)
        self.assertEqual(visit.visited_at, day - timedelta(hours=2))
        self.assertIn("Survivor's notes", visit.notes)
        self.assertIn("Loser's notes", visit.notes)
        self.assertIn(friend, visit.participants.all())
        photo.refresh_from_db()
        self.assertEqual(photo.visit_id, kept.pk)

    def test_automatic_visits_on_different_days_both_move(self) -> None:
        day = timezone.now() - timedelta(days=5)
        baker.make(PinVisit, pin=self.survivor, source=VisitSource.GEOLOCATION, visited_at=day)
        baker.make(PinVisit, pin=self.loser, source=VisitSource.GEOLOCATION, visited_at=day - timedelta(days=1))

        merge_pins(self.survivor, self.loser, self.profile)

        self.assertEqual(PinVisit.objects.filter(pin=self.survivor).count(), 2)


class DuplicateRowsAreFoldedTests(_MergeTestCase):
    def test_a_duplicate_owners_details_and_sales_move_to_the_kept_owner(self) -> None:
        kept = PinOwner.objects.create(pin=self.survivor, name="Acme Holdings", phone="555-0100")
        duplicate = PinOwner.objects.create(
            pin=self.loser, name="ACME HOLDINGS", phone="555-0199", email="owner@example.com", notes="Responds by mail"
        )
        sale = PinPropertySale.objects.create(pin=self.loser)
        sale.new_owners.add(duplicate)

        merge_pins(self.survivor, self.loser, self.profile)

        kept.refresh_from_db()
        self.assertEqual(kept.email, "owner@example.com")
        self.assertIn("Responds by mail", kept.notes)
        self.assertIn("555-0199", kept.notes)
        self.assertEqual(list(sale.new_owners.all()), [kept])

    def test_a_duplicate_pending_shares_message_and_bundle_survive(self) -> None:
        recipient = baker.make(User).profile
        kept = PinShare.objects.create(
            pin=self.survivor,
            from_profile=self.profile,
            to_profile=recipient,
            status=PinShareStatus.PENDING,
            message="Check this out",
        )
        duplicate = PinShare.objects.create(
            pin=self.loser,
            from_profile=self.profile,
            to_profile=recipient,
            status=PinShareStatus.PENDING,
            message="Go at dawn",
        )
        bundled = PinShare.objects.create(
            pin=None,
            from_profile=self.profile,
            to_profile=recipient,
            status=PinShareStatus.PENDING,
            bundled_with=duplicate,
        )

        merge_pins(self.survivor, self.loser, self.profile)

        kept.refresh_from_db()
        self.assertIn("Go at dawn", kept.message)
        self.assertIn("Check this out", kept.message)
        bundled.refresh_from_db()
        self.assertEqual(bundled.bundled_with_id, kept.pk)


class SetNullRelationsTests(_MergeTestCase):
    def test_a_floorplan_moves_to_the_survivor(self) -> None:
        floorplan = baker.make(Floorplan, profile=self.profile, pin=self.loser)

        merge_pins(self.survivor, self.loser, self.profile)

        floorplan.refresh_from_db()
        self.assertEqual(floorplan.pin_id, self.survivor.pk)

    def test_no_set_null_relation_to_pin_is_left_unhandled(self) -> None:
        """A SET_NULL relation the merge never mentions keeps its rows but cuts them loose from any pin."""
        source = inspect.getsource(pin_merge)
        unhandled = sorted(
            rel.related_model.__name__
            for rel in Pin._meta.related_objects
            if getattr(getattr(rel.field.remote_field, "on_delete", None), "__name__", "") == "SET_NULL"
            and not re.search(rf"\b{rel.related_model.__name__}\b|\.{rel.get_accessor_name()}\b", source)
        )

        self.assertEqual(
            unhandled, [], "these SET_NULL relations lose their pin when one is merged away - handle or document them"
        )
