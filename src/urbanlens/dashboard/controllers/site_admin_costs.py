"""Site-admin controller for cost tracking: hardware/operating cost CRUD, stats, and charts."""

# Generic imports
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import logging
from typing import TYPE_CHECKING, Any

# Django Imports
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

# App Imports
from urbanlens.dashboard.models.costs import CostComponent, OperatingCost
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.admin.cost_tracking import (
    active_user_count,
    average_monthly_expense,
    cost_per_user,
    effective_monthly_cost,
    monthly_cost_series,
    total_recorded_expenses,
)
from urbanlens.dashboard.services.core.json_safety import safe_json_for_script

logger = logging.getLogger(__name__)

_BODY_PARTIAL = "dashboard/partials/admin/_cost_admin_body.html"


class _CostAdminMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Shared permission gate for the site-admin cost tracking pages."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated non-admins."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()


def _admin_context(**extra: Any) -> dict[str, Any]:
    """Build the shared context for the site-admin cost tracking page and its body partial."""
    series = monthly_cost_series()

    context: dict[str, Any] = {
        "components": CostComponent.objects.all(),
        "operating_costs": OperatingCost.objects.all(),
        "breakdown": effective_monthly_cost(),
        "total_recorded_expenses": total_recorded_expenses(),
        "average_monthly_expense": average_monthly_expense(),
        "cost_per_user": cost_per_user(),
        "active_user_count": active_user_count(),
        "chart_labels": safe_json_for_script(series["labels"]),
        "chart_hardware": safe_json_for_script(series["hardware"]),
        "chart_operating": safe_json_for_script(series["operating"]),
        "chart_api": safe_json_for_script(series["api"]),
        "public_costs_page_enabled": SiteSettings.get_current().public_costs_page_enabled,
        "active": "costs",
    }
    context.update(extra)
    return context


def _toast_response(request: HttpRequest, *, level: str, message: str, status: int = 200) -> HttpResponse:
    """Re-render the shared body partial with a toast fired via HX-Trigger."""
    response = render(request, _BODY_PARTIAL, _admin_context(), status=status)
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


def _apply_component_form(component: CostComponent, request: HttpRequest) -> None:
    """Copy submitted form values onto *component* without saving it.

    Args:
        component: The instance to populate.
        request: The request carrying the POSTed form.

    Raises:
        ValidationError: When a required field is missing or unparseable.
    """
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValidationError({"name": "Name is required."})

    try:
        replacement_cost = Decimal((request.POST.get("replacement_cost") or "").strip())
    except InvalidOperation as exc:
        raise ValidationError({"replacement_cost": "Replacement cost must be a number."}) from exc

    try:
        deprecation_years = Decimal((request.POST.get("deprecation_years") or "").strip())
    except InvalidOperation as exc:
        raise ValidationError({"deprecation_years": "Deprecation years must be a number."}) from exc

    component.name = name
    component.replacement_cost = replacement_cost
    component.deprecation_years = deprecation_years
    component.notes = (request.POST.get("notes") or "").strip()
    component.order = int(request.POST.get("order") or 0)
    component.retired_at = None if request.POST.get("is_active") == "on" else (component.retired_at or timezone.now())

    component.full_clean()


def _apply_operating_cost_form(cost: OperatingCost, request: HttpRequest) -> None:
    """Copy submitted form values onto *cost* without saving it.

    Args:
        cost: The instance to populate.
        request: The request carrying the POSTed form.

    Raises:
        ValidationError: When a required field is missing or unparseable.
    """
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValidationError({"name": "Name is required."})

    try:
        monthly_cost = Decimal((request.POST.get("monthly_cost") or "").strip())
    except InvalidOperation as exc:
        raise ValidationError({"monthly_cost": "Monthly cost must be a number."}) from exc

    cost.name = name
    cost.monthly_cost = monthly_cost
    cost.notes = (request.POST.get("notes") or "").strip()
    cost.order = int(request.POST.get("order") or 0)
    cost.retired_at = None if request.POST.get("is_active") == "on" else (cost.retired_at or timezone.now())

    cost.full_clean()


class SiteAdminCostsView(_CostAdminMixin, View):
    """Site-admin page for cost tracking: components, operating costs, stats, and charts.

    GET /site-admin/costs/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "dashboard/pages/site_admin_costs.html", _admin_context())


class SiteAdminCostComponentsView(_CostAdminMixin, View):
    """Create a depreciating cost component.

    POST /site-admin/costs/components/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        component = CostComponent()
        try:
            _apply_component_form(component, request)
        except ValidationError as exc:
            return render(request, _BODY_PARTIAL, _admin_context(component_form_errors=exc.message_dict), status=400)

        component.save()
        messages.success(request, f"Added cost component “{component.name}”.")
        return _toast_response(request, level="success", message=f"Added cost component “{component.name}”.")


class SiteAdminCostComponentEditView(_CostAdminMixin, View):
    """Edit or delete one cost component.

    POST   /site-admin/costs/components/<id>/  → save changes
    DELETE /site-admin/costs/components/<id>/  → delete
    """

    def post(self, request: HttpRequest, component_id: int) -> HttpResponse:
        component = get_object_or_404(CostComponent, pk=component_id)
        try:
            _apply_component_form(component, request)
        except ValidationError as exc:
            return render(request, _BODY_PARTIAL, _admin_context(component_form_errors=exc.message_dict), status=400)

        component.save()
        messages.success(request, f"Updated cost component “{component.name}”.")
        return _toast_response(request, level="success", message=f"Updated cost component “{component.name}”.")

    def delete(self, request: HttpRequest, component_id: int) -> HttpResponse:
        component = get_object_or_404(CostComponent, pk=component_id)
        name = component.name
        component.delete()
        messages.success(request, f"Deleted cost component “{name}”.")
        return _toast_response(request, level="info", message=f"Deleted cost component “{name}”.")


class SiteAdminOperatingCostsView(_CostAdminMixin, View):
    """Create a recurring operating cost.

    POST /site-admin/costs/operating/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        cost = OperatingCost()
        try:
            _apply_operating_cost_form(cost, request)
        except ValidationError as exc:
            return render(request, _BODY_PARTIAL, _admin_context(operating_form_errors=exc.message_dict), status=400)

        cost.save()
        messages.success(request, f"Added operating cost “{cost.name}”.")
        return _toast_response(request, level="success", message=f"Added operating cost “{cost.name}”.")


class SiteAdminOperatingCostEditView(_CostAdminMixin, View):
    """Edit or delete one operating cost.

    POST   /site-admin/costs/operating/<id>/  → save changes
    DELETE /site-admin/costs/operating/<id>/  → delete
    """

    def post(self, request: HttpRequest, cost_id: int) -> HttpResponse:
        cost = get_object_or_404(OperatingCost, pk=cost_id)
        try:
            _apply_operating_cost_form(cost, request)
        except ValidationError as exc:
            return render(request, _BODY_PARTIAL, _admin_context(operating_form_errors=exc.message_dict), status=400)

        cost.save()
        messages.success(request, f"Updated operating cost “{cost.name}”.")
        return _toast_response(request, level="success", message=f"Updated operating cost “{cost.name}”.")

    def delete(self, request: HttpRequest, cost_id: int) -> HttpResponse:
        cost = get_object_or_404(OperatingCost, pk=cost_id)
        name = cost.name
        cost.delete()
        messages.success(request, f"Deleted operating cost “{name}”.")
        return _toast_response(request, level="info", message=f"Deleted operating cost “{name}”.")


class SiteAdminCostsPublicToggleView(_CostAdminMixin, View):
    """Toggle whether the public /costs/ page is visible.

    POST /site-admin/costs/toggle-public/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        settings_obj = SiteSettings.get_current()
        settings_obj.public_costs_page_enabled = not settings_obj.public_costs_page_enabled
        settings_obj.save(update_fields=["public_costs_page_enabled"])

        message = "Public costs page enabled." if settings_obj.public_costs_page_enabled else "Public costs page disabled."
        messages.success(request, message)
        return _toast_response(request, level="success", message=message)
