"""Public running-costs transparency page."""

from __future__ import annotations

from django.http import Http404
from django.views.generic import TemplateView


class CostsView(TemplateView):
    """Render the public page showing UrbanLens's estimated running costs.

    Gated behind ``SiteSettings.public_costs_page_enabled`` (off by default) - the
    page 404s until an admin turns it on from the site-admin cost tracking page.

    Combines the site admin's hardware/operating cost tracking with the same
    per-service API cost estimates the site-admin API usage report is built from
    (``ApiCallLog.objects.summary_by_service()`` + ``ServiceDefaults.cost_per_call``),
    aggregated without any per-user or per-request detail - only service-level and
    site-wide totals are ever shown here.
    """

    template_name = "dashboard/pages/costs/index.html"

    def get_context_data(self, **kwargs):
        """Add the combined cost breakdown, chart data, and per-service detail to the context.

        Args:
            **kwargs: Standard ``TemplateView`` keyword arguments.

        Returns:
            Template context including ``priced_services``, ``unpriced_service_count``,
            ``total_cost_30d``, ``breakdown``, ``cost_per_user``, ``active_user_count``,
            and the monthly chart series.
        """
        from urbanlens.dashboard.models.api_call_log import ApiCallLog
        from urbanlens.dashboard.models.site_settings import SiteSettings
        from urbanlens.dashboard.services.admin.cost_tracking import (
            active_user_count,
            cost_per_user,
            effective_monthly_cost,
            monthly_cost_series,
        )
        from urbanlens.dashboard.services.core.json_safety import safe_json_for_script
        from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

        if not SiteSettings.get_current().public_costs_page_enabled:
            raise Http404

        context = super().get_context_data(**kwargs)
        context["page_name"] = "costs"

        breakdown = effective_monthly_cost()
        series = monthly_cost_series()
        context["breakdown"] = breakdown
        context["cost_per_user"] = cost_per_user()
        context["active_user_count"] = active_user_count()
        context["chart_labels"] = safe_json_for_script(series["labels"])
        context["chart_hardware"] = safe_json_for_script(series["hardware"])
        context["chart_operating"] = safe_json_for_script(series["operating"])
        context["chart_api"] = safe_json_for_script(series["api"])

        service_defaults = all_service_defaults()
        summaries = {row["service"]: row for row in ApiCallLog.objects.summary_by_service()}

        priced_services = []
        unpriced_service_count = 0
        total_cost_30d = None
        for service, defaults in service_defaults.items():
            if defaults.cost_per_call is None:
                unpriced_service_count += 1
                continue
            row = summaries.get(service, {})
            cost_30d = row.get("total_cost")
            if cost_30d is None:
                continue
            total_cost_30d = cost_30d if total_cost_30d is None else total_cost_30d + cost_30d
            priced_services.append({"display_name": defaults.display_name, "cost_30d": cost_30d})

        priced_services.sort(key=lambda entry: entry["cost_30d"], reverse=True)
        context["priced_services"] = priced_services
        context["unpriced_service_count"] = unpriced_service_count
        context["total_cost_30d"] = total_cost_30d
        return context
