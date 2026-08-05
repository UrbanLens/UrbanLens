"""Custom layer views - user-created, independently-toggleable groupings of
markup items on a Pin or Wiki map (e.g. a "Tunnels" layer).

Two parents can own a CustomLayer (see ``CustomLayer``): a Pin (personal,
editable only by its owner) or a Wiki (shared community data, editable by any
signed-in user with wiki access) - the same permission split as ``PinMarkup``
itself. Layers never attach to a standalone ``MarkupMap``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES
from urbanlens.dashboard.models.markup.meta import CUSTOM_LAYER_ICON_CHOICES
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.text_limits import text_length_error
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from django.http import HttpRequest

    from urbanlens.dashboard.models.wiki.model import Wiki

_MAX_LAYER_NAME_LENGTH = 100
_ALLOWED_COLORS = {hex_value for hex_value, _label in COLOR_CHOICES}
_ALLOWED_ICONS = {value for value, _label in CUSTOM_LAYER_ICON_CHOICES}


def _resolve_layer_owner(request: HttpRequest, pin_slug: str | None, location_slug: str | None) -> tuple[Pin | Wiki, QuerySet[CustomLayer]]:
    """Resolve the CustomLayer owner (Pin or Wiki) from URL kwargs.

    Same permission split as ``markup._resolve_owner``'s pin/wiki branches:
    pin-scoped requires ownership, wiki-scoped uses ``resolve_visible_wiki``
    (any signed-in user who has pinned the location may manage its layers,
    matching the existing shared markup-editing model).

    Args:
        request: The current HttpRequest (used for the ownership checks).
        pin_slug: Slug of the parent pin, if this is a personal-layer route.
        location_slug: Slug of the parent location, if this is a community-layer route.

    Returns:
        Tuple of (owner, layer queryset already filtered to that owner).
    """
    if pin_slug is not None:
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return pin, CustomLayer.objects.for_pin(pin)
    if location_slug is None:
        raise Http404
    _location, wiki, _profile = resolve_visible_wiki(request, location_slug)
    return wiki, CustomLayer.objects.for_wiki(wiki)


def _owner_kwargs(owner: Pin | Wiki) -> dict:
    """Return the CustomLayer FK kwargs (parent_pin/parent_wiki) for *owner*."""
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def _get_layer(request: HttpRequest, pin_slug: str | None, location_slug: str | None, layer_uuid: str) -> tuple[Pin | Wiki, QuerySet[CustomLayer], CustomLayer]:
    """Resolve a single owner-scoped CustomLayer, 404ing if it belongs to someone else.

    Args:
        request: The current HttpRequest.
        pin_slug: Slug of the parent pin (personal-layer route).
        location_slug: Slug of the parent location (community-layer route).
        layer_uuid: UUID of the layer to resolve.

    Returns:
        Tuple of (owner, owner-scoped queryset, the resolved layer).
    """
    owner, qs = _resolve_layer_owner(request, pin_slug, location_slug)
    return owner, qs, get_object_or_404(qs, uuid=layer_uuid)


def _share_layer_to_wiki(pin_layer: CustomLayer, profile: Profile) -> tuple[CustomLayer | None, bool]:
    """Copy a pin-scoped layer's name/color/icon into a wiki-scoped layer.

    Never creates the wiki itself (same rule ``services.pins.pin_wiki_sync``
    follows for sending pins to a wiki) and never duplicates: if a wiki layer
    with the same name already exists, that one is reused rather than adding
    a second copy on repeat clicks.

    Args:
        pin_layer: The pin-scoped ``CustomLayer`` to share.
        profile: The profile to attribute a newly-created wiki layer to.

    Returns:
        Tuple of (the wiki-scoped layer, whether it was newly created). The
        layer is ``None`` when the pin's location has no community wiki yet.

    Raises:
        ValueError: If ``pin_layer`` isn't pin-scoped.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    if pin_layer.parent_pin is None:
        raise ValueError("_share_layer_to_wiki requires a pin-scoped layer")

    wiki = Wiki.objects.get_for_location(pin_layer.parent_pin.location)
    if wiki is None:
        return None, False

    wiki_layers = CustomLayer.objects.for_wiki(wiki)
    existing = wiki_layers.filter(name__iexact=pin_layer.name).first()
    if existing is not None:
        return existing, False

    next_order = (wiki_layers.order_by("-order").values_list("order", flat=True).first() or 0) + 1
    new_layer = CustomLayer.objects.create(
        name=pin_layer.name,
        color=pin_layer.color,
        icon=pin_layer.icon,
        order=next_order,
        profile=profile,
        parent_wiki=wiki,
    )
    return new_layer, True


def _render_layer_list(request: HttpRequest, owner: Pin | Wiki, qs: QuerySet[CustomLayer]) -> HttpResponse:
    """Render the manage-layers list partial, with an ``HX-Trigger`` for the map JS.

    URLs are pre-built here (by positional ``args``, so the pin vs. wiki URL
    name difference is invisible to the template) rather than reversed
    in-template, since ``{% url %}`` can't cleanly take a dynamic view name
    plus a dynamic set of keyword arguments.

    Args:
        request: The current HttpRequest.
        owner: The Pin or Wiki the layers belong to (used to build action URLs).
        qs: The owner's CustomLayer queryset.

    Returns:
        Rendered ``_custom_layers_list.html`` partial with an
        ``HX-Trigger: ul:custom-layers-changed`` payload carrying the fresh
        layer list, so map-annotations.ts can re-sync the layers panel
        buttons/toggles and the markup layer-assignment dropdown without a
        full page reload.
    """
    is_pin = isinstance(owner, Pin)
    url_prefix = "pin.layers" if is_pin else "location.wiki.layers"
    owner_slug = owner.slug if is_pin else owner.location.slug

    ordered_layers = list(qs.order_by("order", "created"))
    rows = [
        {
            "layer": layer,
            "edit_url": reverse(f"{url_prefix}.edit", args=[owner_slug, layer.uuid]),
            "reorder_url": reverse(f"{url_prefix}.reorder", args=[owner_slug, layer.uuid]),
            "share_url": reverse("pin.layers.share_to_wiki", args=[owner_slug, layer.uuid]) if is_pin else None,
        }
        for layer in ordered_layers
    ]
    response = render(
        request,
        "dashboard/partials/layout/_custom_layers_list.html",
        {
            "rows": rows,
            "create_url": reverse(url_prefix, args=[owner_slug]),
            "color_choices": COLOR_CHOICES,
            "custom_layer_icon_choices": CUSTOM_LAYER_ICON_CHOICES,
        },
    )
    response["HX-Trigger"] = json.dumps(
        {
            "ul:custom-layers-changed": {
                "layers": [
                    {
                        "uuid": str(layer.uuid),
                        "name": layer.name,
                        "icon": layer.icon or "layers",
                        "color": layer.color,
                        "default_visible": layer.default_visible,
                    }
                    for layer in ordered_layers
                ],
            },
        }
    )
    return response


class CustomLayerListCreateView(LoginRequiredMixin, View):
    """List (dialog body) and create custom layers for a pin or wiki.

    GET /map/pin/<pin_slug>/layers/
    GET /location/<location_slug>/wiki/layers/
    POST /map/pin/<pin_slug>/layers/
    POST /location/<location_slug>/wiki/layers/
    """

    def get(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Render the Manage Layers dialog body.

        Args:
            request: HttpRequest.
            pin_slug: Slug of the parent pin (personal-layer route).
            location_slug: Slug of the parent location (community-layer route).

        Returns:
            Rendered dialog body listing the owner's custom layers.
        """
        owner, qs = _resolve_layer_owner(request, pin_slug, location_slug)
        return _render_layer_list(request, owner, qs)

    def post(self, request: HttpRequest, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Create a new custom layer for the resolved owner.

        Args:
            request: HttpRequest with form-encoded ``name``/``color``/``icon``.
            pin_slug: Slug of the parent pin (personal-layer route).
            location_slug: Slug of the parent location (community-layer route).

        Returns:
            Re-rendered layer list on success, or a 400 with an error message.
        """
        owner, qs = _resolve_layer_owner(request, pin_slug, location_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        name = request.POST.get("name", "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)
        length_error = text_length_error(name, _MAX_LAYER_NAME_LENGTH, "Layer name")
        if length_error:
            return HttpResponse(length_error, status=400)

        color = request.POST.get("color") or ""
        icon = request.POST.get("icon") or ""
        next_order = (qs.order_by("-order").values_list("order", flat=True).first() or 0) + 1

        CustomLayer.objects.create(
            name=name,
            color=color if color in _ALLOWED_COLORS else "",
            icon=icon if icon in _ALLOWED_ICONS else "",
            order=next_order,
            profile=profile,
            **_owner_kwargs(owner),
        )
        return _render_layer_list(request, owner, qs)


class CustomLayerEditView(LoginRequiredMixin, View):
    """Update or delete a single custom layer.

    POST/DELETE /map/pin/<pin_slug>/layers/<layer_uuid>/
    POST/DELETE /location/<location_slug>/wiki/layers/<layer_uuid>/
    """

    def post(self, request: HttpRequest, layer_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Update mutable fields on a custom layer.

        Args:
            request: HttpRequest with form-encoded fields.
            layer_uuid: UUID of the layer to update.
            pin_slug: Slug of the parent pin (personal-layer route).
            location_slug: Slug of the parent location (community-layer route).

        Returns:
            Re-rendered layer list on success, or a 400 with an error message.
        """
        owner, qs, layer = _get_layer(request, pin_slug, location_slug, layer_uuid)

        if "name" in request.POST:
            name = request.POST["name"].strip()
            if not name:
                return HttpResponse("Name is required.", status=400)
            length_error = text_length_error(name, _MAX_LAYER_NAME_LENGTH, "Layer name")
            if length_error:
                return HttpResponse(length_error, status=400)
            layer.name = name
        if "color" in request.POST:
            color = request.POST["color"]
            layer.color = color if color in _ALLOWED_COLORS else ""
        if "icon" in request.POST:
            icon = request.POST["icon"]
            layer.icon = icon if icon in _ALLOWED_ICONS else ""
        if "default_visible" in request.POST:
            layer.default_visible = request.POST["default_visible"] in ("1", "true", "on")
        layer.save()
        return _render_layer_list(request, owner, qs)

    def delete(self, request: HttpRequest, layer_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Delete a custom layer, un-grouping (not deleting) its items.

        Args:
            request: HttpRequest.
            layer_uuid: UUID of the layer to delete.
            pin_slug: Slug of the parent pin (personal-layer route).
            location_slug: Slug of the parent location (community-layer route).

        Returns:
            Re-rendered layer list.
        """
        owner, qs, layer = _get_layer(request, pin_slug, location_slug, layer_uuid)
        layer.delete()
        return _render_layer_list(request, owner, qs)


class CustomLayerReorderView(LoginRequiredMixin, View):
    """Move a custom layer up or down among its owner's other layers.

    POST /map/pin/<pin_slug>/layers/<layer_uuid>/reorder/
    POST /location/<location_slug>/wiki/layers/<layer_uuid>/reorder/
    """

    def post(self, request: HttpRequest, layer_uuid: str, pin_slug: str | None = None, location_slug: str | None = None) -> HttpResponse:
        """Swap this layer's ``order`` with its immediate neighbor.

        Args:
            request: HttpRequest with form-encoded ``direction`` ("up"|"down").
            layer_uuid: UUID of the layer to move.
            pin_slug: Slug of the parent pin (personal-layer route).
            location_slug: Slug of the parent location (community-layer route).

        Returns:
            Re-rendered layer list. A move at either boundary is a no-op.
        """
        owner, qs, layer = _get_layer(request, pin_slug, location_slug, layer_uuid)
        ordered = list(qs.order_by("order", "created"))
        index = next((i for i, candidate in enumerate(ordered) if candidate.pk == layer.pk), -1)
        step = {"up": -1, "down": 1}.get(request.POST.get("direction", ""))

        if index >= 0 and step is not None:
            neighbor_index = index + step
            if 0 <= neighbor_index < len(ordered):
                neighbor = ordered[neighbor_index]
                layer.order, neighbor.order = neighbor.order, layer.order
                CustomLayer.objects.bulk_update([layer, neighbor], ["order"])

        return _render_layer_list(request, owner, qs)


class CustomLayerShareToWikiView(LoginRequiredMixin, View):
    """Copy a pin-scoped layer's name/color/icon into the location's wiki.

    POST /map/pin/<pin_slug>/layers/<layer_uuid>/share-to-wiki/

    Pin-only - there's no wiki-side equivalent, since sharing only ever flows
    from a personal layer up to the shared wiki, never the other way.
    """

    def post(self, request: HttpRequest, layer_uuid: str, pin_slug: str) -> HttpResponse:
        """Share the layer, then re-render the (unchanged) pin-scoped layer list with a toast.

        Args:
            request: HttpRequest.
            layer_uuid: UUID of the pin-scoped layer to share.
            pin_slug: Slug of the parent pin.

        Returns:
            Re-rendered layer list with a ``showToast`` HX-Trigger describing
            the outcome (shared, already shared, or no wiki yet).
        """
        owner, qs, layer = _get_layer(request, pin_slug, None, layer_uuid)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        wiki_layer, created = _share_layer_to_wiki(layer, profile)
        response = _render_layer_list(request, owner, qs)
        if wiki_layer is None:
            toast = {"level": "info", "message": "This property has no community wiki yet."}
        elif created:
            toast = {"level": "success", "message": f'Shared "{layer.name}" to the community wiki.'}
        else:
            toast = {"level": "info", "message": f'"{layer.name}" is already on the community wiki.'}
        triggers = json.loads(response["HX-Trigger"])
        triggers["showToast"] = toast
        response["HX-Trigger"] = json.dumps(triggers)
        return response
