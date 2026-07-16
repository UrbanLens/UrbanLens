"""Property owner and sale-history views - the pin detail page's Ownership card.

Every record is either PRIVATE (attached to the viewer's own Pin, never seen
by anyone else or the wiki) or SHARED (attached to the pin's Location,
visible/editable by anyone with a pin there - the same access rule as the
wiki, see ``services.wiki_access.location_visible_to``). See the module
docstring on ``models.property_owner.model`` for the full rationale.

``source=OFFICIAL`` records (reserved for a future automated data source,
see ``models.property_owner.meta.OwnerSource``) are never directly editable
or removable here - see the guards in ``OwnerUpdateView``/``OwnerRemoveView``/
``PropertySaleDeleteView``. Nothing in this codebase creates OFFICIAL records
yet; the guard exists so that capability can be added later without also
having to retrofit corruption protection at the same time.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.meta import OwnerSource, OwnerVisibility
from urbanlens.dashboard.models.property_owner.model import Owner, PropertySale

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _show_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    """Attach a showToast HX-Trigger to a response.

    Args:
        response: The response to annotate.
        message: Toast message text.
        level: toastr level (``success``, ``info``, ``warning``, ``error``).

    Returns:
        The same response, with the trigger header merged in.
    """
    triggers = json.loads(response.headers.get("HX-Trigger", "{}")) if response.headers.get("HX-Trigger") else {}
    triggers["showToast"] = {"level": level, "message": message}
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _get_pin(request: HttpRequest, pin_slug: str) -> Pin:
    """Return the requesting user's own pin (with its location), or 404.

    Args:
        request: The current request.
        pin_slug: Slug of the pin whose Ownership/Sale History card is being viewed.

    Returns:
        The requester's own Pin, with ``location`` pre-selected.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return get_object_or_404(Pin.objects.select_related("location"), slug=pin_slug, profile=profile)


def _visibility_from_post(request: HttpRequest) -> str:
    """Return the visibility the submitter chose, defaulting to SHARED.

    Args:
        request: The POST request carrying a ``visibility`` field (any
            truthy value means PRIVATE - the form sends it as a checkbox).

    Returns:
        An ``OwnerVisibility`` value.
    """
    return OwnerVisibility.PRIVATE if request.POST.get("visibility") == "private" else OwnerVisibility.SHARED


def _owner_fields_from_post(request: HttpRequest) -> dict[str, str]:
    """Extract and strip the owner-form fields common to create and update.

    Args:
        request: The POST request carrying the owner form.

    Returns:
        Dict of stripped field values, ready to pass to ``Owner(**...)``.
    """
    return {
        "name": (request.POST.get("name") or "").strip(),
        "company_name": (request.POST.get("company_name") or "").strip(),
        "address": (request.POST.get("address") or "").strip(),
        "phone": (request.POST.get("phone") or "").strip(),
        "email": (request.POST.get("email") or "").strip(),
        "notes": (request.POST.get("notes") or "").strip(),
    }


def _parse_owner_names(raw: str) -> list[str]:
    """Split a comma-separated owner-name field into a deduped, ordered list.

    Args:
        raw: The raw form value (e.g. ``"Alice Smith, Bob Jones"``).

    Returns:
        Stripped, non-empty names in first-seen order, duplicates removed.
    """
    names = (name.strip() for name in raw.split(","))
    return list(dict.fromkeys(name for name in names if name))


def _get_or_create_owners(pin: Pin, names: list[str], visibility: str, profile: Profile) -> list[Owner]:
    """Find-or-create owners by name, scoped to a sale's own visibility.

    Matching is scoped to owners already in the same scope (this pin's
    private owners, or this location's shared owners) - not a site-wide name
    search - so the same name on two unrelated properties never accidentally
    merges two different real-world people.

    Args:
        pin: The pin providing the location (SHARED) or private scope (PRIVATE).
        names: Already-parsed, non-empty owner names.
        visibility: The scope to search/create within.
        profile: The requester, recorded as ``created_by`` on new owners.

    Returns:
        The matching or newly created Owner for each name, linked to `pin`
        (PRIVATE) or `pin.location` (SHARED).
    """
    owners = []
    for name in names:
        if visibility == OwnerVisibility.PRIVATE:
            owner = Owner.objects.filter(visibility=OwnerVisibility.PRIVATE, pins=pin, name__iexact=name).first()
            if owner is None:
                owner = Owner.objects.create(name=name, visibility=OwnerVisibility.PRIVATE, created_by=profile)
            owner.pins.add(pin)
        else:
            owner = Owner.objects.filter(visibility=OwnerVisibility.SHARED, locations=pin.location, name__iexact=name).first()
            if owner is None:
                owner = Owner.objects.create(name=name, visibility=OwnerVisibility.SHARED, created_by=profile)
            owner.locations.add(pin.location)
        owners.append(owner)
    return owners


def _unlink_current_owner(pin: Pin, owner: Owner) -> None:
    """Remove `owner` from a property's *current* ownership after a sale.

    Args:
        pin: The pin providing the location (SHARED) or private scope (PRIVATE).
        owner: The (former) owner to unlink - matches `owner.visibility`.
    """
    if owner.visibility == OwnerVisibility.PRIVATE:
        owner.pins.remove(pin)
    else:
        owner.locations.remove(pin.location)


def _render_ownership_panel(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's Ownership card."""
    response = render(
        request,
        "dashboard/partials/pins/_ownership_panel.html",
        {
            "pin": pin,
            "owners": Owner.objects.visible_on(pin),
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class OwnershipPanelView(LoginRequiredMixin, View):
    """GET: the pin's Ownership card.  POST: add a new owner for this property."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        return _render_ownership_panel(request, pin)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_ownership_panel(request, pin, error="Owner name is required.")
        profile, _ = Profile.objects.get_or_create(user=request.user)
        visibility = _visibility_from_post(request)
        owner = Owner.objects.create(**fields, visibility=visibility, created_by=profile)
        if visibility == OwnerVisibility.PRIVATE:
            owner.pins.add(pin)
        else:
            owner.locations.add(pin.location)
        return _render_ownership_panel(request, pin)


class OwnerUpdateView(LoginRequiredMixin, View):
    """POST: edit an existing owner's details (not its visibility, and never an OFFICIAL record)."""

    def post(self, request: HttpRequest, pin_slug: str, owner_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        owner = get_object_or_404(Owner.objects.visible_on(pin), id=owner_id)
        if owner.source == OwnerSource.OFFICIAL:
            return _render_ownership_panel(request, pin, error="Official data can't be edited directly.")
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_ownership_panel(request, pin, error="Owner name is required.")
        for attr, value in fields.items():
            setattr(owner, attr, value)
        owner.save(update_fields=[*fields.keys(), "updated"])
        return _render_ownership_panel(request, pin)


class OwnerRemoveView(LoginRequiredMixin, View):
    """DELETE: unlink an owner from this property (the Owner record itself is kept - they
    may own other properties, or be referenced by this property's own sale history)."""

    def delete(self, request: HttpRequest, pin_slug: str, owner_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        owner = get_object_or_404(Owner.objects.visible_on(pin), id=owner_id)
        if owner.source == OwnerSource.OFFICIAL:
            response = _render_ownership_panel(request, pin)
            return _show_toast(response, "Official data can't be removed directly.", level="error")
        owner_name = owner.name
        _unlink_current_owner(pin, owner)
        response = _render_ownership_panel(request, pin)
        return _show_toast(response, f"Removed “{owner_name}” from this property.")


def _render_sale_tab(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's Sale History tab."""
    response = render(
        request,
        "dashboard/partials/pins/_property_sale_tab.html",
        {
            "pin": pin,
            "sales": PropertySale.objects.visible_on(pin).prefetch_related("previous_owners", "new_owners"),
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class PropertySaleTabView(LoginRequiredMixin, View):
    """GET: the pin's Sale History tab.  POST: record a new sale."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        return _render_sale_tab(request, pin)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        visibility = _visibility_from_post(request)

        raw_price = (request.POST.get("sale_price") or "").strip()
        sale_price: Decimal | None = None
        if raw_price:
            try:
                sale_price = Decimal(raw_price)
            except InvalidOperation:
                return _render_sale_tab(request, pin, error="Invalid sale price.")
            if sale_price < 0:
                return _render_sale_tab(request, pin, error="Sale price can't be negative.")

        raw_date = (request.POST.get("sale_date") or "").strip()
        sale_date: datetime.date | None = None
        if raw_date:
            try:
                sale_date = datetime.date.fromisoformat(raw_date)
            except ValueError:
                return _render_sale_tab(request, pin, error="Invalid sale date.")

        previous_names = _parse_owner_names(request.POST.get("previous_owners") or "")
        new_names = _parse_owner_names(request.POST.get("new_owners") or "")
        if {name.lower() for name in previous_names} & {name.lower() for name in new_names}:
            return _render_sale_tab(request, pin, error="Previous and new owner can't be the same.")

        previous_owners = _get_or_create_owners(pin, previous_names, visibility, profile)
        new_owners = _get_or_create_owners(pin, new_names, visibility, profile)

        # A sale updates who currently owns the property - a seller no
        # longer does (they may still show up elsewhere, e.g. as an owner of
        # a different property, or referenced by this property's own history).
        for owner in previous_owners:
            _unlink_current_owner(pin, owner)

        sale = PropertySale.objects.create(
            location=pin.location,
            pin=pin if visibility == OwnerVisibility.PRIVATE else None,
            visibility=visibility,
            created_by=profile,
            sale_price=sale_price,
            sale_date=sale_date,
            notes=(request.POST.get("notes") or "").strip(),
        )
        sale.previous_owners.set(previous_owners)
        sale.new_owners.set(new_owners)
        return _render_sale_tab(request, pin)


class PropertySaleDeleteView(LoginRequiredMixin, View):
    """DELETE: remove a sale record (never an OFFICIAL one)."""

    def delete(self, request: HttpRequest, pin_slug: str, sale_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        sale = get_object_or_404(PropertySale.objects.visible_on(pin), id=sale_id)
        if sale.source == OwnerSource.OFFICIAL:
            response = _render_sale_tab(request, pin)
            return _show_toast(response, "Official data can't be removed directly.", level="error")
        sale.delete()
        return _render_sale_tab(request, pin)
