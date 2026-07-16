"""Property owner and sale-history views - the pin detail page's Ownership card.

Owner and sale data is a fact about the pin's shared ``Location``, not the pin
itself (see the module docstring on ``models.property_owner.model``). Every
view here resolves through the requester's own ``Pin`` (never a bare location
slug), so a wrong pin slug 404s the same way any other pin-scoped panel does -
this doubles as the edit-permission gate, matching how the aliases and wiki
controllers treat other location-scoped shared facts (you may only see/edit
this data for places you've actually pinned).
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
from urbanlens.dashboard.models.property_owner.model import Owner, PropertySale

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

    from urbanlens.dashboard.models.location.model import Location

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


def _get_or_create_owner(location: Location, name: str) -> Owner | None:
    """Find an existing owner of `location` by name, or create a bare new one.

    Matching is scoped to owners already linked to this location (not a
    site-wide name search), so entering "John Smith" on two unrelated
    properties never accidentally merges two different real-world people.

    Args:
        location: The location the (possibly new) owner should be linked to.
        name: The owner's name, already stripped.

    Returns:
        The matching or newly created Owner, linked to `location`; None if
        `name` is blank.
    """
    if not name:
        return None
    owner = Owner.objects.for_location(location).filter(name__iexact=name).first()
    if owner is None:
        owner = Owner.objects.create(name=name)
    owner.locations.add(location)
    return owner


def _render_ownership_panel(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's Ownership card."""
    response = render(
        request,
        "dashboard/partials/pins/_ownership_panel.html",
        {
            "pin": pin,
            "owners": Owner.objects.for_location(pin.location),
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
        owner = Owner.objects.create(**fields)
        owner.locations.add(pin.location)
        return _render_ownership_panel(request, pin)


class OwnerUpdateView(LoginRequiredMixin, View):
    """POST: edit an existing owner's details."""

    def post(self, request: HttpRequest, pin_slug: str, owner_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        owner = get_object_or_404(Owner.objects.for_location(pin.location), id=owner_id)
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
        owner = get_object_or_404(Owner.objects.for_location(pin.location), id=owner_id)
        owner_name = owner.name
        owner.locations.remove(pin.location)
        response = _render_ownership_panel(request, pin)
        return _show_toast(response, f"Removed “{owner_name}” from this property.")


def _render_sale_tab(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's Sale History tab."""
    response = render(
        request,
        "dashboard/partials/pins/_property_sale_tab.html",
        {
            "pin": pin,
            "sales": PropertySale.objects.for_location(pin.location).select_related("previous_owner", "new_owner"),
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
        location = pin.location

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

        previous_owner = _get_or_create_owner(location, (request.POST.get("previous_owner") or "").strip())
        new_owner = _get_or_create_owner(location, (request.POST.get("new_owner") or "").strip())
        if previous_owner is not None and new_owner is not None and previous_owner.pk == new_owner.pk:
            return _render_sale_tab(request, pin, error="Previous and new owner can't be the same.")

        # A sale updates who currently owns the property - the seller no
        # longer does (they may still show up elsewhere, e.g. as an owner of
        # a different property, or referenced by this property's own history).
        if previous_owner is not None:
            previous_owner.locations.remove(location)

        PropertySale.objects.create(
            location=location,
            sale_price=sale_price,
            sale_date=sale_date,
            previous_owner=previous_owner,
            new_owner=new_owner,
            notes=(request.POST.get("notes") or "").strip(),
        )
        return _render_sale_tab(request, pin)


class PropertySaleDeleteView(LoginRequiredMixin, View):
    """DELETE: remove a sale record."""

    def delete(self, request: HttpRequest, pin_slug: str, sale_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        sale = get_object_or_404(PropertySale.objects.for_location(pin.location), id=sale_id)
        sale.delete()
        return _render_sale_tab(request, pin)
