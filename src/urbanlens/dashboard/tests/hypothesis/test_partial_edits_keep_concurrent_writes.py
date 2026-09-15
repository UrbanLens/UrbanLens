"""A partial edit writes only what it changed, so a concurrent edit to another field survives it (P5).

Each test lands a second writer's change between the handler loading the row and saving it, which is
what two overlapping autosaves or two editors of one annotation produce.
"""

from __future__ import annotations

from decimal import Decimal
import json
from unittest import mock

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.notifications import _PREF_FIELDS
from urbanlens.dashboard.models.abstract.field_snapshot import FieldSnapshot
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.costs.model import CostComponent, OperatingCost
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import CustomLayer, PinMarkup
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.colors import clean_color
from urbanlens.dashboard.services.notifications.notification_center import get_preferences
from urbanlens.dashboard.tests.hypothesis.test_wiki_boundary_view import _square


def _concurrent_write_before_save(model, **other_writer):
    """Patch ``model.save`` so another writer's ``update`` lands just before the real save runs."""
    original = model.save

    def save(self, *args, **kwargs):
        model.objects.filter(pk=self.pk).update(**other_writer)
        return original(self, *args, **kwargs)

    return mock.patch.object(model, "save", autospec=True, side_effect=save)


class FieldSnapshotTests(TestCase):
    def test_nothing_changed_writes_nothing(self) -> None:
        layer = CustomLayer.objects.create(
            name="Tunnels", parent_pin=baker.make(Pin), profile=baker.make("auth.User").profile
        )
        with mock.patch.object(CustomLayer, "save") as save:
            self.assertEqual(FieldSnapshot(layer).save_changes(), [])
        save.assert_not_called()

    def test_an_in_place_change_to_a_json_value_counts_as_changed(self) -> None:
        item = PinMarkup(markup_type="polygon", geometry={"type": "Polygon", "coordinates": [[[0, 0]]]})
        snapshot = FieldSnapshot(item)
        item.geometry["coordinates"][0][0] = [1, 1]
        self.assertEqual(snapshot.changed(), ["geometry"])


class SiteSettingsAutosaveTests(TestCase):
    def test_saving_one_setting_keeps_another_admins_concurrent_change(self) -> None:
        settings = SiteSettings.get_current()
        admin = baker.make("auth.User", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        new_quota = settings.storage_quota_gb + 7
        other_members = settings.max_trip_members + 5

        with _concurrent_write_before_save(SiteSettings, max_trip_members=other_members):
            response = self.client.post(reverse("site_admin"), {"storage_quota_gb": new_quota})

        self.assertLess(response.status_code, 400)
        row = SiteSettings.objects.get(pk=settings.pk)
        self.assertEqual(row.storage_quota_gb, new_quota)
        self.assertEqual(row.max_trip_members, other_members, "the autosave reverted a setting it was never sent")


class _OwnPinCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))


class MarkupEditTests(_OwnPinCase):
    def test_relabelling_keeps_a_concurrent_colour_change(self) -> None:
        created = self.client.post(
            reverse("pin.markup", kwargs={"pin_slug": self.pin.slug}),
            data=json.dumps(
                {"markup_type": "polygon", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 0]]]}}
            ),
            content_type="application/json",
        )
        self.assertLess(created.status_code, 400)
        item = PinMarkup.objects.get(profile=self.profile)

        with _concurrent_write_before_save(PinMarkup, color="#00ff00"):
            response = self.client.post(
                reverse("pin.markup.edit", kwargs={"pin_slug": self.pin.slug, "markup_uuid": item.uuid}),
                data=json.dumps({"label": "Loading dock"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.label, "Loading dock")
        self.assertEqual(item.color, "#00ff00", "the relabel reverted a colour it was never sent")


class CustomLayerEditTests(_OwnPinCase):
    def test_renaming_keeps_a_concurrent_visibility_change(self) -> None:
        layer = CustomLayer.objects.create(
            name="Tunnels", parent_pin=self.pin, profile=self.profile, default_visible=True
        )

        with _concurrent_write_before_save(CustomLayer, default_visible=False):
            response = self.client.post(
                reverse("pin.layers.edit", args=[self.pin.slug, layer.uuid]), {"name": "Drains"}
            )

        self.assertEqual(response.status_code, 200)
        layer.refresh_from_db()
        self.assertEqual(layer.name, "Drains")
        self.assertFalse(layer.default_visible, "the rename reverted a visibility it was never sent")


class DetailPinEditTests(_OwnPinCase):
    def test_restyling_keeps_concurrent_notes(self) -> None:
        child = baker.make(
            Pin, profile=self.profile, parent_pin=self.pin, location=baker.make(Location), description="Mine"
        )

        with _concurrent_write_before_save(Pin, description="Written in another tab"):
            response = self.client.post(
                reverse("pin.detail_pin.edit", kwargs={"pin_slug": self.pin.slug, "detail_pin_uuid": child.uuid}),
                data=json.dumps({"color": "#ff0000"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.color, clean_color("#ff0000"))
        self.assertEqual(child.description, "Written in another tab", "the restyle reverted notes it was never sent")


class ChildWikiEditTests(TestCase):
    def test_restyling_keeps_another_editors_concurrent_description(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        user = baker.make("auth.User")
        self.client.force_login(user)
        location = Location.objects.create(latitude=40.0, longitude=-74.0)
        parent = baker.make_recipe("dashboard.wiki", location=location)
        baker.make_recipe("dashboard.pin", profile=user.profile, location=location)
        child = baker.make_recipe(
            "dashboard.wiki",
            parent_wiki=parent,
            location=Location.objects.create(latitude=40.001, longitude=-74.001),
            name="Gatehouse",
            description="Before",
        )

        with _concurrent_write_before_save(Wiki, description="Another editor's words"):
            response = self.client.post(
                reverse("location.wiki.detail_pin.edit", args=[location.slug, child.uuid]),
                data=json.dumps({"color": "#ff0000"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.color, clean_color("#ff0000"))
        self.assertEqual(
            child.description, "Another editor's words", "the restyle reverted a description it was never sent"
        )


class _SiteAdminCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(baker.make("auth.User", is_superuser=True, is_staff=True))


class CostEditTests(_SiteAdminCase):
    def test_renaming_a_component_keeps_a_concurrent_reorder(self) -> None:
        component = CostComponent.objects.create(
            name="Server", replacement_cost=Decimal(1000), deprecation_years=Decimal(5), order=0
        )
        form = {"name": "Rack server", "replacement_cost": "1000", "deprecation_years": "5", "order": "0"}

        with _concurrent_write_before_save(CostComponent, order=9):
            response = self.client.post(
                reverse("site_admin_cost_component_edit", kwargs={"component_id": component.pk}),
                {**form, "is_active": "on"},
            )

        self.assertLess(response.status_code, 400)
        component.refresh_from_db()
        self.assertEqual(component.name, "Rack server")
        self.assertEqual(component.order, 9, "the rename reverted an order it was sent unchanged")

    def test_renaming_an_operating_cost_keeps_a_concurrent_reorder(self) -> None:
        cost = OperatingCost.objects.create(name="Power", monthly_cost=Decimal(100), order=0)

        with _concurrent_write_before_save(OperatingCost, order=9):
            response = self.client.post(
                reverse("site_admin_operating_cost_edit", kwargs={"cost_id": cost.pk}),
                {"name": "Electricity", "monthly_cost": "100", "order": "0", "is_active": "on"},
            )

        self.assertLess(response.status_code, 400)
        cost.refresh_from_db()
        self.assertEqual(cost.name, "Electricity")
        self.assertEqual(cost.order, 9, "the rename reverted an order it was sent unchanged")


class ApiLimitsEditTests(_SiteAdminCase):
    def test_saving_limits_keeps_the_rate_limiters_concurrent_last_call(self) -> None:
        cfg = ApiRateLimit.objects.create(service="zz_partial_edit", display_name="Partial", calls_per_minute=10)
        called = timezone.now()

        with _concurrent_write_before_save(ApiRateLimit, last_call_at=called):
            response = self.client.post(
                reverse("site_admin_api_limits"),
                {"service": cfg.service, "enabled": "on", "calls_per_minute": "10", "notes": "Throttled"},
            )

        self.assertLess(response.status_code, 400)
        cfg.refresh_from_db()
        self.assertEqual(cfg.notes, "Throttled")
        self.assertEqual(cfg.last_call_at, called, "saving the limits reverted the rate limiter's last call")


class NotificationPreferencesEditTests(TestCase):
    def test_changing_one_preference_keeps_a_concurrent_change_to_another(self) -> None:
        user = baker.make("auth.User")
        self.client.force_login(user)
        prefs = get_preferences(user.profile)
        form = {}
        for field, _label in _PREF_FIELDS:
            value = getattr(prefs, field)
            if value in {DeliveryPreference.SITE, DeliveryPreference.BOTH}:
                form[f"{field}__site"] = "1"
            if value in {DeliveryPreference.EMAIL, DeliveryPreference.BOTH}:
                form[f"{field}__email"] = "1"
        form.pop("message__site", None)
        form["message__email"] = "1"
        concurrent = (
            DeliveryPreference.NONE if prefs.trip_updated != DeliveryPreference.NONE else DeliveryPreference.BOTH
        )

        with _concurrent_write_before_save(NotificationPreference, trip_updated=concurrent):
            response = self.client.post(reverse("notifications.preferences"), form)

        self.assertEqual(response.status_code, 200)
        prefs.refresh_from_db()
        self.assertEqual(prefs.message, DeliveryPreference.EMAIL)
        self.assertEqual(prefs.trip_updated, concurrent, "the save reverted a preference the form carried unchanged")


class WikiBoundaryEditTests(TestCase):
    def test_redrawing_keeps_a_concurrent_generated_boundary(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        user = baker.make("auth.User")
        self.client.force_login(user)
        location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        wiki = baker.make("dashboard.Wiki", location=location, name="Community Mill")
        baker.make(Pin, profile=user.profile, location=location)
        url = reverse("location.wiki.boundary", args=[location.slug])

        def post(delta: float):
            body = {"boundary_type": "property", "polygon": json.loads(_square(-74.0, 40.0, delta).geojson)}
            return self.client.post(url, data=json.dumps(body), content_type="application/json")

        with mock.patch(
            "urbanlens.dashboard.controllers.boundary.schedule_location_boundary_generation", return_value=False
        ):
            self.assertEqual(post(0.001).status_code, 200)
            generated = timezone.now()
            with _concurrent_write_before_save(Boundary, generated_at=generated):
                response = post(0.002)

        self.assertEqual(response.status_code, 200)
        row = Boundary.objects.get(wiki=wiki, boundary_type=BoundaryType.PROPERTY)
        self.assertEqual(row.generated_at, generated, "the redraw reverted a generation that landed meanwhile")
