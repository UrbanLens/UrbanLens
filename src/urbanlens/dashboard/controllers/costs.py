"""Public running-costs transparency page."""

from __future__ import annotations

from django.http import Http404
from django.views.generic import TemplateView


class CostsView(TemplateView):
    """Render the public page showing UrbanLens's estimated running costs.

    Gated behind ``SiteSettings.public_costs_page_enabled`` (off by default) - the
    page 404s until an admin turns it on from the site-admin cost tracking page.

    Shows only the aggregate monthly total and its trend - the per-service external
    API spend breakdown lives on the site-admin cost tracking page instead.
    """

    template_name = "dashboard/pages/costs/index.html"

    def get_context_data(self, **kwargs):
        """Add the cost breakdown and monthly total chart series to the context.

        Args:
            **kwargs: Standard ``TemplateView`` keyword arguments.

        Returns:
            Template context including ``breakdown``, ``total_hardware_cost``,
            ``cost_per_user``, ``active_user_count``, and the monthly total chart series.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.services.admin.cost_tracking import (
            active_user_count,
            cost_per_user,
            effective_monthly_cost,
            monthly_cost_series,
            total_hardware_cost,
        )
        from urbanlens.dashboard.services.core.json_safety import safe_json_for_script

        if not SiteSettings.get_current().public_costs_page_enabled:
            raise Http404

        context = super().get_context_data(**kwargs)
        context["page_name"] = "costs"

        breakdown = effective_monthly_cost()
        series = monthly_cost_series()
        context["breakdown"] = breakdown
        context["total_hardware_cost"] = total_hardware_cost()
        context["cost_per_user"] = cost_per_user()
        context["active_user_count"] = active_user_count()
        context["chart_labels"] = safe_json_for_script(series["labels"])
        context["chart_total"] = safe_json_for_script(
            [h + o + a for h, o, a in zip(series["hardware"], series["operating"], series["api"], strict=True)]
        )
        return context
