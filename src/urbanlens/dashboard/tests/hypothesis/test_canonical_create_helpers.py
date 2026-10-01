"""Get-or-create against a unique constraint goes through one helper per model, and never 500s."""

from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.labels.queryset import LabelNameConflictError, LabelQuerySet
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki.queryset import WikiManager


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make(User))


def _first_lookup_misses(method_owner: type, name: str):
    """Patch *name* so its first call finds nothing - the state a concurrent insert leaves behind."""
    real = getattr(method_owner, name)
    calls: list[int] = []

    def wrapper(self, *args, **kwargs):
        calls.append(1)
        result = real(self, *args, **kwargs)
        if len(calls) == 1:
            return result.none() if hasattr(result, "none") else None
        return result

    return patch.object(method_owner, name, wrapper)


class TripActivityGeocodedLocationTests(TestCase):
    """G3-20: numeric(9,6) columns never matched a raw 8-decimal float, so the second activity 500'd."""

    def test_two_activities_at_one_precise_point_share_a_location(self) -> None:
        from urbanlens.dashboard.services.trips.trip_activities import resolve_activity_place

        profile = _profile()
        body = {"geocoded_lat": "40.12345678", "geocoded_lng": "-74.12345678", "geocoded_name": "Old Mill"}

        first, _ = resolve_activity_place(body, profile)
        second, _ = resolve_activity_place(body, profile)

        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(Location.objects.filter(latitude="40.123457", longitude="-74.123457").count(), 1)


class LabelResolveOrCreateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()

    def test_prefers_the_profiles_own_label_over_a_global_one(self) -> None:
        Label.objects.create(profile=None, name="ZzAudit Silo", kind=KIND_TAG)
        own = Label.objects.create(profile=self.profile, name="zzaudit silo", kind=KIND_TAG)

        label, created = Label.objects.resolve_or_create(self.profile, "ZZAUDIT SILO", KIND_TAG)

        self.assertEqual((label, created), (own, False))

    def test_reuses_a_global_label_rather_than_shadowing_it(self) -> None:
        shared = Label.objects.create(profile=None, name="ZzAudit Silo", kind=KIND_TAG)

        label, created = Label.objects.resolve_or_create(self.profile, "zzaudit silo", KIND_TAG)

        self.assertEqual((label, created), (shared, False))
        self.assertFalse(Label.objects.filter(profile=self.profile, name__iexact="zzaudit silo").exists())

    def test_a_profile_scoped_kind_never_resolves_to_a_global_label(self) -> None:
        Label.objects.create(profile=None, name="ZzAudit Silo", kind=KIND_CATEGORY)

        label, created = Label.objects.resolve_or_create(self.profile, "zzaudit silo", KIND_CATEGORY)

        self.assertTrue(created)
        self.assertEqual(label.profile_id, self.profile.pk)

    def test_creates_an_owned_label_with_defaults_when_none_exists(self) -> None:
        label, created = Label.objects.resolve_or_create(
            self.profile, "  Water Tower ", KIND_TAG, defaults={"color": "#2196F3"}
        )

        self.assertTrue(created)
        self.assertEqual(
            (label.profile_id, label.name, label.kind, label.color),
            (self.profile.pk, "Water Tower", KIND_TAG, "#2196F3"),
        )

    def test_kind_scopes_the_lookup(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Silo", kind=KIND_TAG)

        label, created = Label.objects.resolve_or_create(self.profile, "ZzAudit Silo", KIND_CATEGORY)

        self.assertTrue(created)
        self.assertEqual(label.kind, KIND_CATEGORY)

    def test_a_concurrent_case_variant_insert_is_reused_not_raised(self) -> None:
        raced = Label.objects.create(profile=self.profile, name="Water Tower", kind=KIND_TAG)

        with _first_lookup_misses(LabelQuerySet, "named"):
            label, created = Label.objects.resolve_or_create(self.profile, "WATER TOWER", KIND_TAG)

        self.assertEqual((label, created), (raced, False))

    def test_blank_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Label.objects.resolve_or_create(self.profile, "   ", KIND_TAG)


class LabelCreateUniqueTests(TestCase):
    """Refuse-on-conflict creation (the Organize form, the external API) reports a raced collision."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()

    def test_a_raced_case_variant_raises_the_conflict_not_integrity_error(self) -> None:
        raced = Label.objects.create(profile=self.profile, name="Water Tower", kind=KIND_TAG)

        with _first_lookup_misses(LabelQuerySet, "named"), self.assertRaises(LabelNameConflictError) as caught:
            Label.objects.create_unique(profile=self.profile, name="water tower", kind=KIND_TAG)

        self.assertEqual(caught.exception.conflict, raced)

    def test_a_global_label_of_that_name_is_a_conflict(self) -> None:
        shared = Label.objects.create(profile=None, name="ZzAudit Silo", kind=KIND_TAG)

        with self.assertRaises(LabelNameConflictError) as caught:
            Label.objects.create_unique(profile=self.profile, name="zzaudit silo", kind=KIND_TAG)

        self.assertEqual(caught.exception.conflict, shared)

    def test_the_organize_create_view_answers_400_on_a_raced_collision(self) -> None:
        from urbanlens.dashboard.controllers.labels import LabelCreateView

        Label.objects.create(profile=self.profile, name="Water Tower", kind=KIND_TAG)
        request = RequestFactory().post("/labels/tags/create/", {"name": "WATER TOWER"})
        request.user = self.profile.user

        with patch("urbanlens.dashboard.controllers.labels.find_conflicting_label", return_value=None):
            response = LabelCreateView.as_view(kind=KIND_TAG)(request)

        self.assertEqual(response.status_code, 400)


class PinCategoryEditTests(TestCase):
    """G3-16: the category edit matches names case-insensitively, stays profile-scoped, and is all-or-nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        self.pin = baker.make(Pin, profile=self.profile)
        self.kept = Label.objects.create(profile=self.profile, name="Existing", kind=KIND_CATEGORY)
        self.pin.labels.add(self.kept)

    def _post(self, body: dict):
        from urbanlens.dashboard.controllers.pin_edit import PinEditView

        request = RequestFactory().post(
            f"/map/pin/{self.pin.slug}/edit/", data=json.dumps(body), content_type="application/json"
        )
        request.user = self.profile.user
        with patch(
            "urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name",
            return_value=None,
        ):
            return PinEditView.as_view()(request, pin_slug=self.pin.slug)

    def test_a_case_variant_of_an_own_category_is_reused(self) -> None:
        self.assertEqual(self._post({"categories": "EXISTING, Silo"}).status_code, 200)

        names = sorted(self.pin.labels.filter(kind=KIND_CATEGORY).values_list("name", flat=True))
        self.assertEqual(names, ["Existing", "Silo"])
        self.assertEqual(Label.objects.filter(profile=self.profile, name__iexact="existing").count(), 1)

    def test_categories_stay_profile_scoped_even_when_a_global_one_exists(self) -> None:
        Label.objects.create(profile=None, name="ZzAudit Silo", kind=KIND_CATEGORY)

        self.assertEqual(self._post({"categories": "zzaudit silo"}).status_code, 200)

        attached = list(self.pin.labels.filter(kind=KIND_CATEGORY))
        self.assertEqual([label.profile_id for label in attached], [self.profile.pk])

    def test_a_failure_part_way_leaves_the_old_categories_in_place(self) -> None:
        real = LabelQuerySet.resolve_or_create

        def fail_on_second(self, profile, name, kind, **kwargs):
            if name.casefold() == "second":
                raise IntegrityError("simulated")
            return real(self, profile, name, kind, **kwargs)

        with patch.object(LabelQuerySet, "resolve_or_create", fail_on_second), self.assertRaises(IntegrityError):
            self._post({"categories": "first,second"})

        self.assertEqual(list(self.pin.labels.filter(kind=KIND_CATEGORY)), [self.kept])


class MediaLabelTests(TestCase):
    def test_a_raced_case_variant_media_label_is_reused(self) -> None:
        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.services.media.media_labels import set_media_labels

        profile = _profile()
        image = baker.make(Image, profile=profile)
        raced = Label.objects.create(profile=profile, name="Rooftop", kind=KIND_MEDIA)

        with _first_lookup_misses(LabelQuerySet, "named"):
            labels = set_media_labels(image, ["ROOFTOP"], profile)

        self.assertEqual(labels, [raced])


class DefaultLabelSeedingTests(TestCase):
    """G1-25: default labels are seeded in bulk and skip a name the profile already has in any case."""

    def test_new_profile_gets_the_defaults_and_the_hierarchy(self) -> None:
        from urbanlens.dashboard.models.labels.signals import CATEGORY_HIERARCHY, DEFAULT_CATEGORIES, DEFAULT_TAGS

        profile = _profile()

        self.assertEqual(Label.objects.filter(profile=profile, kind=KIND_CATEGORY).count(), len(DEFAULT_CATEGORIES))
        self.assertEqual(Label.objects.filter(profile=profile, kind=KIND_TAG).count(), len(DEFAULT_TAGS))
        self.assertTrue(Label.objects.get(profile=profile, name="Visited").is_protected)
        parent_name, child_name = CATEGORY_HIERARCHY[0]
        child = Label.objects.get(profile=profile, kind=KIND_CATEGORY, name=child_name)
        self.assertEqual([p.name for p in child.parents.all()], [parent_name])

    def test_reseeding_over_a_case_variant_does_not_raise(self) -> None:
        from urbanlens.dashboard.models.labels.signals import create_default_tags

        profile = _profile()
        Label.objects.filter(profile=profile, name="Hospital", kind=KIND_CATEGORY).update(name="HOSPITAL")

        create_default_tags(Profile, profile, created=True)

        self.assertEqual(Label.objects.filter(profile=profile, name__iexact="hospital", kind=KIND_CATEGORY).count(), 1)


class LabelImportUuidCollisionTests(TestCase):
    """The exported uuid is globally unique, so another account's label holding it must not abort the import."""

    def test_a_label_uuid_owned_by_another_profile_gets_a_fresh_uuid(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_labels

        importer = _profile()
        other = _profile()
        taken = Label.objects.create(profile=other, name="ZzAudit Source", kind=KIND_TAG)
        row = {"uuid": str(taken.uuid), "name": "ZzAudit Imported", "kind": KIND_TAG, "is_user_label": True}
        label_uuid_map: dict[str, int] = {}

        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "labels.json").write_text(json.dumps([row]), encoding="utf-8")
            _import_labels(importer, data_dir, ImportResult(), pin_uuid_map={}, label_uuid_map=label_uuid_map)

        imported = Label.objects.get(profile=importer, name="ZzAudit Imported")
        self.assertNotEqual(imported.uuid, taken.uuid)
        self.assertEqual(label_uuid_map[str(taken.uuid)], imported.pk)
        taken.refresh_from_db()
        self.assertEqual(taken.profile_id, other.pk)

    def test_a_global_row_recreated_as_personal_also_avoids_a_taken_uuid(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_labels

        importer = _profile()
        taken = Label.objects.create(profile=_profile(), name="ZzAudit Source", kind=KIND_TAG)
        row = {"uuid": str(taken.uuid), "name": "ZzAudit Global", "kind": KIND_TAG, "is_user_label": False}

        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "labels.json").write_text(json.dumps([row]), encoding="utf-8")
            _import_labels(importer, data_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})

        self.assertTrue(Label.objects.filter(profile=importer, name="ZzAudit Global").exists())

    def test_an_import_resolved_onto_a_global_label_does_not_reparent_it(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_labels

        importer = _profile()
        shared = Label.objects.create(profile=None, name="ZzAudit Shared", kind=KIND_TAG)
        parent_row = {"uuid": "00000000-0000-4000-8000-00000000a001", "name": "ZzAudit Parent", "kind": KIND_TAG}
        child_row = {
            "uuid": "00000000-0000-4000-8000-00000000a002",
            "name": "zzaudit shared",
            "kind": KIND_TAG,
            "is_user_label": False,
            "parent_uuids": [parent_row["uuid"]],
        }

        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "labels.json").write_text(json.dumps([parent_row, child_row]), encoding="utf-8")
            _import_labels(importer, data_dir, ImportResult(), pin_uuid_map={}, label_uuid_map={})

        self.assertFalse(shared.parents.exists())

    def test_reimporting_ones_own_export_still_matches_by_uuid(self) -> None:
        from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_labels

        importer = _profile()
        mine = Label.objects.create(profile=importer, name="ZzAudit Mine", kind=KIND_TAG)
        row = {"uuid": str(mine.uuid), "name": "ZzAudit Renamed Since", "kind": KIND_TAG, "is_user_label": True}
        label_uuid_map: dict[str, int] = {}

        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "labels.json").write_text(json.dumps([row]), encoding="utf-8")
            _import_labels(importer, data_dir, ImportResult(), pin_uuid_map={}, label_uuid_map=label_uuid_map)

        self.assertEqual(label_uuid_map[str(mine.uuid)], mine.pk)
        self.assertFalse(Label.objects.filter(name="ZzAudit Renamed Since").exists())


class ApiKeyPrefixCollisionTests(TestCase):
    """G3-28: a prefix collision is retried at the insert, not pre-checked and then left to raise."""

    def test_a_colliding_prefix_is_retried(self) -> None:
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        baker.make(ApiKey, prefix="collide123")
        prefixes = iter(["collide123456", "fresh45678xx"])

        with patch(
            "urbanlens.dashboard.services.auth.api_keys.secrets.token_urlsafe",
            side_effect=lambda n: next(prefixes) if n == 8 else "s" * 48,
        ):
            api_key, _raw = generate_api_key(baker.make(User), "Zapier")

        self.assertEqual(api_key.prefix, "fresh45678")

    def test_a_collision_is_detected_by_the_insert_not_a_precheck(self) -> None:
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        baker.make(ApiKey, prefix="collide123")
        prefixes = iter(["collide123456", "fresh45678xx"])

        with (
            patch.object(ApiKey.objects, "filter", side_effect=AssertionError("pre-check")),
            patch(
                "urbanlens.dashboard.services.auth.api_keys.secrets.token_urlsafe",
                side_effect=lambda n: next(prefixes) if n == 8 else "s" * 48,
            ),
        ):
            api_key, _raw = generate_api_key(baker.make(User), "Zapier")

        self.assertEqual(api_key.prefix, "fresh45678")


class WikiCreateRaceTests(TestCase):
    """G1-14: both one-to-ones on Wiki (location, place) race; the loser reads the winner's row."""

    def test_a_raced_insert_on_the_same_location_returns_the_existing_wiki(self) -> None:
        location = Location.objects.create(latitude=40.0, longitude=-74.0)
        existing = baker.make(Wiki, location=location)
        location = Location.objects.get(pk=location.pk)

        with _first_lookup_misses(WikiManager, "existing_for_location"):
            wiki, created = Wiki.objects.get_or_create_for_location(location)

        self.assertEqual((wiki, created), (existing, False))

    def test_a_raced_insert_for_another_location_on_the_same_place_returns_the_places_wiki(self) -> None:
        place = baker.make(Place, kind=PlaceKind.PARCEL)
        first = Location.objects.create(latitude=41.0, longitude=-75.0, place=place)
        second = Location.objects.create(latitude=41.001, longitude=-75.001, place=place)
        existing = baker.make(Wiki, location=first, place=place)

        with _first_lookup_misses(WikiManager, "existing_for_location"):
            wiki, created = Wiki.objects.get_or_create_for_location(second)

        self.assertEqual((wiki, created), (existing, False))
        self.assertEqual(Wiki.objects.filter(place=place).count(), 1)

    def test_an_unrelated_integrity_error_still_raises(self) -> None:
        location = Location.objects.create(latitude=42.0, longitude=-76.0)

        with (
            patch.object(WikiManager, "create", side_effect=IntegrityError("other")),
            self.assertRaises(IntegrityError),
        ):
            Wiki.objects.get_or_create_for_location(location, defaults={"name": "Explicit"})


class AliasResolveOrCreateTests(TestCase):
    """Alias save() sanitizes the name, so a raw-name lookup missed the stored row and the insert collided."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        self.pin = baker.make(Pin, profile=self.profile)

    def test_a_name_that_sanitizes_onto_an_existing_alias_reuses_it(self) -> None:
        existing = PinAlias.objects.create(pin=self.pin, name="Old Mill")

        alias, created = PinAlias.objects.resolve_or_create(self.pin, "  old   MILL ✨ ")

        self.assertEqual((alias, created), (existing, False))

    def test_a_name_that_sanitizes_to_nothing_creates_nothing(self) -> None:
        self.assertEqual(PinAlias.objects.resolve_or_create(self.pin, "✨✨"), (None, False))
        self.assertFalse(PinAlias.objects.filter(pin=self.pin).exists())

    def test_the_owner_may_be_given_by_primary_key(self) -> None:
        wiki = baker.make(Wiki, location=Location.objects.create(latitude=43.0, longitude=-77.0))

        alias, created = WikiAlias.objects.resolve_or_create(wiki.pk, "Grain Elevator", defaults={"kind": "official"})

        self.assertTrue(created)
        self.assertEqual((alias.wiki_id, alias.kind), (wiki.pk, "official"))

    def test_sharing_a_pin_under_a_name_that_sanitizes_onto_an_alias_does_not_500(self) -> None:
        from django.urls import reverse

        from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

        recipient = _profile()
        Friendship.objects.create(from_profile=self.profile, to_profile=recipient, status=FriendshipStatus.ACCEPTED)
        self.pin.slug = self.pin.ensure_slug()
        self.pin.save(update_fields=["slug"])
        PinAlias.objects.create(pin=self.pin, name="Old Mill")
        self.client.force_login(self.profile.user)

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.pin.slug}),
            {"profile_id": recipient.pk, "custom_name": "Old  Mill"},
        )

        self.assertLess(response.status_code, 500)
        self.assertEqual(PinAlias.objects.filter(pin=self.pin, name__iexact="old mill").count(), 1)

    def test_an_external_name_differing_by_case_leaves_the_transaction_usable(self) -> None:
        from django.db import transaction

        from urbanlens.dashboard.services.locations.name_resolution import NameCandidate
        from urbanlens.dashboard.services.locations.naming import _add_wiki_aliases

        wiki = baker.make(Wiki, location=Location.objects.create(latitude=44.0, longitude=-78.0))
        WikiAlias.objects.create(wiki=wiki, name="old mill")

        with transaction.atomic():
            _add_wiki_aliases(wiki, [NameCandidate(name="Old Mill", source="osm")])
            self.assertEqual(WikiAlias.objects.filter(wiki=wiki, name__iexact="old mill").count(), 1)
