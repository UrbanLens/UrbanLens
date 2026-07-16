"""Property owner and sale-history views.

Pin-scoped views (``Pin*``) manage ``PinOwner``/``PinPropertySale`` - private
to one pin, never shared with other users or the wiki, never intermingled
with wiki data on the pin detail page. Wiki-scoped views (``Wiki*``) manage
``WikiOwner``/``WikiPropertySale`` - shared with everyone who has this
location pinned, visible only on the wiki page, never on any individual
pin's own card. This mirrors the ``PinAlias``/``WikiAlias`` split
(``models.aliases.model``, ``controllers.aliases``) exactly - two separate
models, two separate view groups, one shared template per concept
configured entirely via context (see ``partials/pins/_ownership_panel.html``'s
own header comment).

``WikiOwner``/``WikiPropertySale`` carry a ``source`` distinguishing
user-contributed data from a future automated source (``source=OFFICIAL``) -
never directly user-editable (see the guards in ``WikiOwnerUpdateView``/
``WikiOwnerRemoveView``/``WikiPropertySaleDeleteView``). Nothing creates
OFFICIAL records yet. ``PinOwner``/``PinPropertySale`` have no such concept -
private, per-pin data is definitionally user-entered.
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
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale, WikiOwner, WikiPropertySale
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

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
        Dict of stripped field values, ready to pass to an Owner model's constructor.
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


def _parse_sale_owner_names(request: HttpRequest) -> tuple[list[str], list[str], str | None]:
    """Parse and cross-validate a sale form's previous/new owner-name fields.

    Args:
        request: The POST request carrying ``previous_owners``/``new_owners``.

    Returns:
        ``(previous_names, new_names, error)`` - error is None on success,
        and both lists are empty when an error is returned.
    """
    previous_names = _parse_owner_names(request.POST.get("previous_owners") or "")
    new_names = _parse_owner_names(request.POST.get("new_owners") or "")
    if {name.lower() for name in previous_names} & {name.lower() for name in new_names}:
        return [], [], "Previous and new owner can't be the same."
    return previous_names, new_names, None


def _parse_sale_price_and_date(request: HttpRequest) -> tuple[Decimal | None, datetime.date | None, str | None]:
    """Parse and validate a sale form's price/date fields.

    Args:
        request: The POST request carrying ``sale_price``/``sale_date``.

    Returns:
        ``(sale_price, sale_date, error)`` - error is None on success.
    """
    raw_price = (request.POST.get("sale_price") or "").strip()
    sale_price: Decimal | None = None
    if raw_price:
        try:
            sale_price = Decimal(raw_price)
        except InvalidOperation:
            return None, None, "Invalid sale price."
        if sale_price < 0:
            return None, None, "Sale price can't be negative."

    raw_date = (request.POST.get("sale_date") or "").strip()
    sale_date: datetime.date | None = None
    if raw_date:
        try:
            sale_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return None, None, "Invalid sale date."

    return sale_price, sale_date, None


# ======================================================================
# Pin-scoped (private) - PinOwner/PinPropertySale, never shared
# ======================================================================


def _render_pin_ownership_panel(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's private Ownership card."""
    response = render(
        request,
        "dashboard/partials/pins/_ownership_panel.html",
        {
            "owners": PinOwner.objects.for_pin(pin),
            "panel_id": "pin-ownership-panel",
            "collapse_scope": "pin",
            "show_official_badge": False,
            "obj_slug": pin.slug,
            "url_add": "pin.ownership",
            "url_edit": "pin.ownership.edit",
            "url_remove": "pin.ownership.remove",
            "url_sales": "pin.sales",
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class PinOwnershipPanelView(LoginRequiredMixin, View):
    """GET: the pin's private Ownership card.  POST: add a new owner, private to this pin."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        return _render_pin_ownership_panel(request, pin)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_pin_ownership_panel(request, pin, error="Owner name is required.")
        PinOwner.objects.create(pin=pin, **fields)
        return _render_pin_ownership_panel(request, pin)


class PinOwnerUpdateView(LoginRequiredMixin, View):
    """POST: edit one of this pin's private owners."""

    def post(self, request: HttpRequest, pin_slug: str, owner_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        owner = get_object_or_404(PinOwner, id=owner_id, pin=pin)
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_pin_ownership_panel(request, pin, error="Owner name is required.")
        for attr, value in fields.items():
            setattr(owner, attr, value)
        owner.save(update_fields=[*fields.keys(), "updated"])
        return _render_pin_ownership_panel(request, pin)


class PinOwnerRemoveView(LoginRequiredMixin, View):
    """DELETE: remove one of this pin's private owners.

    Unlike the shared/wiki side, there is nowhere else a PinOwner could be
    "still current" - it belongs to exactly this one pin, so removal is a
    real delete rather than an unlink.
    """

    def delete(self, request: HttpRequest, pin_slug: str, owner_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        owner = get_object_or_404(PinOwner, id=owner_id, pin=pin)
        owner_name = owner.name
        owner.delete()
        response = _render_pin_ownership_panel(request, pin)
        return _show_toast(response, f"Removed “{owner_name}”.")


def _render_pin_sale_tab(request: HttpRequest, pin: Pin, error: str | None = None) -> HttpResponse:
    """Render the pin detail page's private Sale History tab."""
    response = render(
        request,
        "dashboard/partials/pins/_property_sale_tab.html",
        {
            "sales": PinPropertySale.objects.for_pin(pin).prefetch_related("previous_owners", "new_owners"),
            "show_official_badge": False,
            "obj_slug": pin.slug,
            "url_add": "pin.sales",
            "url_delete": "pin.sales.delete",
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class PinPropertySaleTabView(LoginRequiredMixin, View):
    """GET: the pin's private Sale History tab.  POST: record a new sale, private to this pin."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        return _render_pin_sale_tab(request, pin)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = _get_pin(request, pin_slug)

        sale_price, sale_date, price_date_error = _parse_sale_price_and_date(request)
        if price_date_error:
            return _render_pin_sale_tab(request, pin, error=price_date_error)

        previous_names, new_names, names_error = _parse_sale_owner_names(request)
        if names_error:
            return _render_pin_sale_tab(request, pin, error=names_error)

        def get_or_create(name: str) -> PinOwner:
            owner = PinOwner.objects.for_pin(pin).filter(name__iexact=name).first()
            return owner or PinOwner.objects.create(pin=pin, name=name)

        previous_owners = [get_or_create(name) for name in previous_names]
        new_owners = [get_or_create(name) for name in new_names]

        sale = PinPropertySale.objects.create(pin=pin, sale_price=sale_price, sale_date=sale_date, notes=(request.POST.get("notes") or "").strip())
        sale.previous_owners.set(previous_owners)
        sale.new_owners.set(new_owners)
        return _render_pin_sale_tab(request, pin)


class PinPropertySaleDeleteView(LoginRequiredMixin, View):
    """DELETE: remove one of this pin's private sale records."""

    def delete(self, request: HttpRequest, pin_slug: str, sale_id: int) -> HttpResponse:
        pin = _get_pin(request, pin_slug)
        sale = get_object_or_404(PinPropertySale, id=sale_id, pin=pin)
        sale.delete()
        return _render_pin_sale_tab(request, pin)


# ======================================================================
# Wiki-scoped (shared) - WikiOwner/WikiPropertySale, visible to everyone
# with a pin at this location
# ======================================================================


def _render_wiki_ownership_panel(request: HttpRequest, location: Location, error: str | None = None) -> HttpResponse:
    """Render the wiki page's shared Ownership card."""
    response = render(
        request,
        "dashboard/partials/pins/_ownership_panel.html",
        {
            "owners": WikiOwner.objects.for_location(location),
            "panel_id": "location-ownership-panel",
            "collapse_scope": "wiki",
            "show_official_badge": True,
            "obj_slug": location.slug,
            "url_add": "location.wiki.ownership",
            "url_edit": "location.wiki.ownership.edit",
            "url_remove": "location.wiki.ownership.remove",
            "url_sales": "location.wiki.sales",
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class WikiOwnershipPanelView(LoginRequiredMixin, View):
    """GET: the wiki's shared Ownership card.  POST: add a new owner, shared with the wiki."""

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        return _render_wiki_ownership_panel(request, location)

    def post(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, _wiki, profile = resolve_visible_wiki(request, location_slug)
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_wiki_ownership_panel(request, location, error="Owner name is required.")
        owner = WikiOwner.objects.create(created_by=profile, **fields)
        owner.locations.add(location)
        return _render_wiki_ownership_panel(request, location)


class WikiOwnerUpdateView(LoginRequiredMixin, View):
    """POST: edit a shared owner (not an OFFICIAL record)."""

    def post(self, request: HttpRequest, location_slug: str, owner_id: int) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        owner = get_object_or_404(WikiOwner.objects.for_location(location), id=owner_id)
        if owner.source == OwnerSource.OFFICIAL:
            return _render_wiki_ownership_panel(request, location, error="Official data can't be edited directly.")
        fields = _owner_fields_from_post(request)
        if not fields["name"]:
            return _render_wiki_ownership_panel(request, location, error="Owner name is required.")
        for attr, value in fields.items():
            setattr(owner, attr, value)
        owner.save(update_fields=[*fields.keys(), "updated"])
        return _render_wiki_ownership_panel(request, location)


class WikiOwnerRemoveView(LoginRequiredMixin, View):
    """DELETE: unlink a shared owner from this property (the record itself is kept - they
    may own other properties, or be referenced by this property's own sale history);
    never an OFFICIAL record."""

    def delete(self, request: HttpRequest, location_slug: str, owner_id: int) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        owner = get_object_or_404(WikiOwner.objects.for_location(location), id=owner_id)
        if owner.source == OwnerSource.OFFICIAL:
            response = _render_wiki_ownership_panel(request, location)
            return _show_toast(response, "Official data can't be removed directly.", level="error")
        owner_name = owner.name
        owner.locations.remove(location)
        response = _render_wiki_ownership_panel(request, location)
        return _show_toast(response, f"Removed “{owner_name}” from this property.")


def _render_wiki_sale_tab(request: HttpRequest, location: Location, error: str | None = None) -> HttpResponse:
    """Render the wiki page's shared Sale History tab."""
    response = render(
        request,
        "dashboard/partials/pins/_property_sale_tab.html",
        {
            "sales": WikiPropertySale.objects.for_location(location).prefetch_related("previous_owners", "new_owners"),
            "show_official_badge": True,
            "obj_slug": location.slug,
            "url_add": "location.wiki.sales",
            "url_delete": "location.wiki.sales.delete",
        },
    )
    if error:
        return _show_toast(response, error, level="error")
    return response


class WikiPropertySaleTabView(LoginRequiredMixin, View):
    """GET: the wiki's shared Sale History tab.  POST: record a new sale, shared with the wiki."""

    def get(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        return _render_wiki_sale_tab(request, location)

    def post(self, request: HttpRequest, location_slug: str) -> HttpResponse:
        location, _wiki, profile = resolve_visible_wiki(request, location_slug)

        sale_price, sale_date, price_date_error = _parse_sale_price_and_date(request)
        if price_date_error:
            return _render_wiki_sale_tab(request, location, error=price_date_error)

        previous_names, new_names, names_error = _parse_sale_owner_names(request)
        if names_error:
            return _render_wiki_sale_tab(request, location, error=names_error)

        def get_or_create(name: str) -> WikiOwner:
            owner = WikiOwner.objects.for_location(location).filter(name__iexact=name).first()
            if owner is None:
                owner = WikiOwner.objects.create(name=name, created_by=profile)
            owner.locations.add(location)
            return owner

        previous_owners = [get_or_create(name) for name in previous_names]
        new_owners = [get_or_create(name) for name in new_names]

        # A sale updates who currently owns the property - a seller no
        # longer does (they may still show up elsewhere, e.g. as an owner of
        # a different property, or referenced by this property's own history).
        for owner in previous_owners:
            owner.locations.remove(location)

        sale = WikiPropertySale.objects.create(location=location, created_by=profile, sale_price=sale_price, sale_date=sale_date, notes=(request.POST.get("notes") or "").strip())
        sale.previous_owners.set(previous_owners)
        sale.new_owners.set(new_owners)
        return _render_wiki_sale_tab(request, location)


class WikiPropertySaleDeleteView(LoginRequiredMixin, View):
    """DELETE: remove a shared sale record (never an OFFICIAL one)."""

    def delete(self, request: HttpRequest, location_slug: str, sale_id: int) -> HttpResponse:
        location, _wiki, _profile = resolve_visible_wiki(request, location_slug)
        sale = get_object_or_404(WikiPropertySale.objects.for_location(location), id=sale_id)
        if sale.source == OwnerSource.OFFICIAL:
            response = _render_wiki_sale_tab(request, location)
            return _show_toast(response, "Official data can't be removed directly.", level="error")
        sale.delete()
        return _render_wiki_sale_tab(request, location)
