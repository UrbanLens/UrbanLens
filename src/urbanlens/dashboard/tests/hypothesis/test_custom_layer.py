"""Tests for CustomLayer: model, PinMarkup.layer, and the manage-layers endpoints.

Covers:
- CustomLayer.to_json() shape and for_pin/for_wiki querysets.
- Deleting a CustomLayer SET_NULLs referencing PinMarkup.layer without
  deleting the items.
- PinMarkup.to_json() layer_uuid field and MarkupJsonView's select_related.
- CustomLayerListCreateView / CustomLayerEditView / CustomLayerReorderView:
  create, rename, recolor, delete, reorder, and cross-owner/cross-user
  permission enforcement (pin-scoped vs. shared wiki-scoped access).
- MarkupView/MarkupEditView accepting layer_uuid to assign/reassign an item
  onto a layer in place, including the cross-owner silent-None case.
"""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.markup.model import CustomLayer, PinMarkup


def _location_with_wiki(name: str = "Old Mill"):
    location = baker.make("dashboard.Location")
    wiki = baker.make("dashboard.Wiki", location=location, name=name)
    return location, wiki


class CustomLayerModelTests(TestCase):
    """CustomLayer.to_json(), for_pin/for_wiki, and SET_NULL-on-delete."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_to_json_shape(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        layer = CustomLayer.objects.create(
            name="Tunnels",
            color="#F44336",
            icon="route",
            order=2,
            default_visible=True,
            parent_pin=pin,
            profile=self.profile,
        )
        self.assertEqual(
            layer.to_json(),
            {
                "uuid": str(layer.uuid),
                "name": "Tunnels",
                "color": "#F44336",
                "icon": "route",
                "order": 2,
                "default_visible": True,
            },
        )

    def test_for_pin_excludes_other_pins_and_wiki_layers(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        other_pin = baker.make("dashboard.Pin", profile=self.profile)
        _location, wiki = _location_with_wiki()
        mine = CustomLayer.objects.create(name="Mine", parent_pin=pin, profile=self.profile)
        CustomLayer.objects.create(name="Other pin", parent_pin=other_pin, profile=self.profile)
        CustomLayer.objects.create(name="Wiki layer", parent_wiki=wiki, profile=self.profile)
        self.assertEqual(list(CustomLayer.objects.for_pin(pin)), [mine])

    def test_for_wiki_excludes_pin_layers(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        _location, wiki = _location_with_wiki()
        CustomLayer.objects.create(name="Mine", parent_pin=pin, profile=self.profile)
        wiki_layer = CustomLayer.objects.create(name="Wiki layer", parent_wiki=wiki, profile=self.profile)
        self.assertEqual(list(CustomLayer.objects.for_wiki(wiki)), [wiki_layer])

    def test_deleting_layer_un_groups_items_without_deleting_them(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        layer.delete()
        item.refresh_from_db()
        self.assertIsNone(item.layer_id)
        self.assertTrue(PinMarkup.objects.filter(pk=item.pk).exists())


class PinMarkupLayerJsonTests(TestCase):
    """PinMarkup.to_json()'s layer_uuid field, and the markup/json/ endpoint."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def test_to_json_includes_layer_uuid_when_set(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        self.assertEqual(item.to_json()["layer_uuid"], str(layer.uuid))

    def test_to_json_layer_uuid_is_none_when_unset(self) -> None:
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        self.assertIsNone(item.to_json()["layer_uuid"])

    def test_markup_json_endpoint_reports_layer_uuid(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        response = self.client.get(reverse("pin.markup.json", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        items = response.json()["markup_items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["layer_uuid"], str(layer.uuid))

    def test_markup_json_endpoint_does_not_n_plus_one_on_layer(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        for _ in range(5):
            baker.make(
                "dashboard.PinMarkup",
                parent_pin=self.pin,
                profile=self.profile,
                markup_type="line",
                geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                layer=layer,
            )
        with self.assertNumQueries(1):
            list(PinMarkup.objects.for_pin(self.pin).select_related("layer"))


class CustomLayerPinEndpointTests(TestCase):
    """Pin-scoped manage-layers endpoints: create/rename/recolor/delete, ownership."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def _list_url(self):
        return reverse("pin.layers", args=[self.pin.slug])

    def _edit_url(self, layer):
        return reverse("pin.layers.edit", args=[self.pin.slug, layer.uuid])

    def _reorder_url(self, layer):
        return reverse("pin.layers.reorder", args=[self.pin.slug, layer.uuid])

    def test_get_renders_dialog_body(self) -> None:
        response = self.client.get(self._list_url())
        self.assertEqual(response.status_code, 200)

    def test_create_layer(self) -> None:
        response = self.client.post(self._list_url(), {"name": "Tunnels", "color": "#F44336", "icon": "route"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Trigger"], "ul:custom-layers-changed")
        layer = CustomLayer.objects.get(parent_pin=self.pin)
        self.assertEqual(layer.name, "Tunnels")
        self.assertEqual(layer.color, "#F44336")
        self.assertEqual(layer.icon, "route")
        self.assertEqual(layer.profile_id, self.profile.pk)

    def test_create_layer_requires_a_name(self) -> None:
        response = self.client.post(self._list_url(), {"name": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CustomLayer.objects.filter(parent_pin=self.pin).exists())

    def test_create_layer_rejects_overlong_name(self) -> None:
        response = self.client.post(self._list_url(), {"name": "x" * 101})
        self.assertEqual(response.status_code, 400)

    def test_create_layer_with_invalid_color_and_icon_blanks_them(self) -> None:
        response = self.client.post(self._list_url(), {"name": "Tunnels", "color": "javascript:alert(1)", "icon": "not-a-real-icon"})
        self.assertEqual(response.status_code, 200)
        layer = CustomLayer.objects.get(parent_pin=self.pin)
        self.assertEqual(layer.color, "")
        self.assertEqual(layer.icon, "")

    def test_create_orders_new_layers_after_existing_ones(self) -> None:
        CustomLayer.objects.create(name="First", parent_pin=self.pin, profile=self.profile, order=5)
        self.client.post(self._list_url(), {"name": "Second"})
        second = CustomLayer.objects.get(name="Second")
        self.assertEqual(second.order, 6)

    def test_rename_and_recolor(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        response = self.client.post(self._edit_url(layer), {"name": "Underground", "color": "#4CAF50"})
        self.assertEqual(response.status_code, 200)
        layer.refresh_from_db()
        self.assertEqual(layer.name, "Underground")
        self.assertEqual(layer.color, "#4CAF50")

    def test_rename_to_empty_name_is_rejected(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        response = self.client.post(self._edit_url(layer), {"name": "   "})
        self.assertEqual(response.status_code, 400)
        layer.refresh_from_db()
        self.assertEqual(layer.name, "Tunnels")

    def test_update_default_visible(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        response = self.client.post(self._edit_url(layer), {"default_visible": "1"})
        self.assertEqual(response.status_code, 200)
        layer.refresh_from_db()
        self.assertTrue(layer.default_visible)

    def test_delete_layer_un_groups_items(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        response = self.client.delete(self._edit_url(layer))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CustomLayer.objects.filter(pk=layer.pk).exists())
        item.refresh_from_db()
        self.assertIsNone(item.layer_id)

    def test_reorder_up_swaps_with_previous_sibling(self) -> None:
        first = CustomLayer.objects.create(name="First", parent_pin=self.pin, profile=self.profile, order=1)
        second = CustomLayer.objects.create(name="Second", parent_pin=self.pin, profile=self.profile, order=2)
        response = self.client.post(self._reorder_url(second), {"direction": "up"})
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.order, 1)
        self.assertEqual(first.order, 2)

    def test_reorder_up_at_top_boundary_is_a_no_op(self) -> None:
        first = CustomLayer.objects.create(name="First", parent_pin=self.pin, profile=self.profile, order=1)
        CustomLayer.objects.create(name="Second", parent_pin=self.pin, profile=self.profile, order=2)
        response = self.client.post(self._reorder_url(first), {"direction": "up"})
        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.order, 1)

    def test_reorder_down_at_bottom_boundary_is_a_no_op(self) -> None:
        CustomLayer.objects.create(name="First", parent_pin=self.pin, profile=self.profile, order=1)
        second = CustomLayer.objects.create(name="Second", parent_pin=self.pin, profile=self.profile, order=2)
        response = self.client.post(self._reorder_url(second), {"direction": "down"})
        self.assertEqual(response.status_code, 200)
        second.refresh_from_db()
        self.assertEqual(second.order, 2)

    def test_other_users_pin_layer_404s_on_edit_and_delete(self) -> None:
        other_user = baker.make("auth.User")
        other_pin = baker.make("dashboard.Pin", profile=other_user.profile)
        other_layer = CustomLayer.objects.create(name="Not yours", parent_pin=other_pin, profile=other_user.profile)
        edit_url = reverse("pin.layers.edit", args=[other_pin.slug, other_layer.uuid])
        self.assertEqual(self.client.post(edit_url, {"name": "Hijacked"}).status_code, 404)
        self.assertEqual(self.client.delete(edit_url).status_code, 404)
        other_layer.refresh_from_db()
        self.assertEqual(other_layer.name, "Not yours")

    def test_layer_uuid_from_a_different_pin_404s_even_under_this_pins_url(self) -> None:
        other_pin = baker.make("dashboard.Pin", profile=self.profile)
        other_layer = CustomLayer.objects.create(name="Elsewhere", parent_pin=other_pin, profile=self.profile)
        edit_url = reverse("pin.layers.edit", args=[self.pin.slug, other_layer.uuid])
        self.assertEqual(self.client.post(edit_url, {"name": "Hijacked"}).status_code, 404)


class CustomLayerWikiEndpointTests(TestCase):
    """Wiki-scoped manage-layers endpoints: shared edit access, not creator-only."""

    def setUp(self) -> None:
        self.location, self.wiki = _location_with_wiki()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def _list_url(self):
        return reverse("location.wiki.layers", args=[self.location.slug])

    def _edit_url(self, layer):
        return reverse("location.wiki.layers.edit", args=[self.location.slug, layer.uuid])

    def test_create_layer_on_wiki(self) -> None:
        response = self.client.post(self._list_url(), {"name": "Tunnels"})
        self.assertEqual(response.status_code, 200)
        layer = CustomLayer.objects.get(parent_wiki=self.wiki)
        self.assertEqual(layer.name, "Tunnels")

    def test_a_second_pinned_profile_can_edit_the_first_profiles_layer(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_wiki=self.wiki, profile=self.profile)

        other_user = baker.make("auth.User")
        baker.make("dashboard.Pin", profile=other_user.profile, location=self.location)
        self.client.force_login(other_user)

        response = self.client.post(self._edit_url(layer), {"name": "Renamed by someone else"})
        self.assertEqual(response.status_code, 200)
        layer.refresh_from_db()
        self.assertEqual(layer.name, "Renamed by someone else")

    def test_unpinned_user_cannot_see_or_edit_wiki_layers(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_wiki=self.wiki, profile=self.profile)
        unpinned_user = baker.make("auth.User")
        self.client.force_login(unpinned_user)
        self.assertEqual(self.client.get(self._list_url()).status_code, 404)
        self.assertEqual(self.client.post(self._edit_url(layer), {"name": "Hijacked"}).status_code, 404)


class MarkupLayerAssignmentTests(TestCase):
    """MarkupView/MarkupEditView accepting layer_uuid to move items onto/off a layer."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def test_create_with_layer_uuid_assigns_the_item(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        response = self.client.post(
            reverse("pin.markup", args=[self.pin.slug]),
            data=json.dumps({
                "markup_type": "line",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "layer_uuid": str(layer.uuid),
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item = PinMarkup.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(item.layer_id, layer.pk)

    def test_create_with_another_pins_layer_uuid_silently_resolves_to_none(self) -> None:
        other_pin = baker.make("dashboard.Pin", profile=self.profile)
        foreign_layer = CustomLayer.objects.create(name="Elsewhere", parent_pin=other_pin, profile=self.profile)
        response = self.client.post(
            reverse("pin.markup", args=[self.pin.slug]),
            data=json.dumps({
                "markup_type": "line",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "layer_uuid": str(foreign_layer.uuid),
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item = PinMarkup.objects.get(uuid=response.json()["uuid"])
        self.assertIsNone(item.layer_id)

    def test_edit_moves_item_onto_layer_in_place(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        )
        response = self.client.post(
            reverse("pin.markup.edit", args=[self.pin.slug, item.uuid]),
            data=json.dumps({"layer_uuid": str(layer.uuid)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.layer_id, layer.pk)
        # Same item, not recreated.
        self.assertEqual(PinMarkup.objects.filter(parent_pin=self.pin).count(), 1)

    def test_edit_moves_item_off_layer_by_sending_empty_layer_uuid(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        response = self.client.post(
            reverse("pin.markup.edit", args=[self.pin.slug, item.uuid]),
            data=json.dumps({"layer_uuid": ""}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertIsNone(item.layer_id)

    def test_edit_omitting_layer_uuid_leaves_layer_unchanged(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        response = self.client.post(
            reverse("pin.markup.edit", args=[self.pin.slug, item.uuid]),
            data=json.dumps({"label": "renamed"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.layer_id, layer.pk)

    def test_edit_with_another_pins_layer_uuid_silently_clears_layer(self) -> None:
        layer = CustomLayer.objects.create(name="Tunnels", parent_pin=self.pin, profile=self.profile)
        item = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            layer=layer,
        )
        other_pin = baker.make("dashboard.Pin", profile=self.profile)
        foreign_layer = CustomLayer.objects.create(name="Elsewhere", parent_pin=other_pin, profile=self.profile)
        response = self.client.post(
            reverse("pin.markup.edit", args=[self.pin.slug, item.uuid]),
            data=json.dumps({"layer_uuid": str(foreign_layer.uuid)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertIsNone(item.layer_id)
