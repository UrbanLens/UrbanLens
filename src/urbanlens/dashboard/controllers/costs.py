"""Public running-costs transparency page."""

from __future__ import annotations

from django.views.generic import TemplateView


class CostsView(TemplateView):
    """Render the public page showing UrbanLens's estimated running costs.

    Reuses the same per-service cost estimates the site-admin API usage
    report is built from (``ApiCallLog.objects.summary_by_service()`` +
    ``ServiceDefaults.cost_per_call``), aggregated without any per-user or
    per-request detail - only service-level totals are ever shown here.
    """

    template_name = "dashboard/pages/costs/index.html"

    def get_context_data(self, **kwargs):
        """Add the last-30-days cost breakdown to the template context.

        Args:
            **kwargs: Standard ``TemplateView`` keyword arguments.

        Returns:
            Template context including ``priced_services``, ``unpriced_service_count``,
            and ``total_cost_30d``.
        """
        from urbanlens.dashboard.models.api_call_log import ApiCallLog
        from urbanlens.dashboard.services.rate_limiter import all_service_defaults

        context = super().get_context_data(**kwargs)
        context["page_name"] = "costs"

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
