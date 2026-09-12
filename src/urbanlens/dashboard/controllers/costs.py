"""Public running-costs transparency page."""

from __future__ import annotations

from django.http import Http404
from django.views.generic import TemplateView

#: Figures are site-wide, so one key serves every caller.
_FIGURES_CACHE_KEY = "public-costs-page-figures-v1"


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
            ``cost_per_user``, ``active_user_count``, ``cost_per_supporter``,
            ``active_supporter_count``, and the monthly total chart series.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        # The gate is checked per request and never cached: a page an admin
        # turns off must stop being reachable now, not when a window expires.
        if not SiteSettings.get_current().public_costs_page_enabled:
            raise Http404

        context = super().get_context_data(**kwargs)
        context["page_name"] = "costs"
        context.update(self._figures())
        return context

    @staticmethod
    def _figures() -> dict:
        """The aggregate figures, computed at most once per cache window.

        Every one is a trailing thirty-day or monthly total, and
        ``active_user_count`` joins every user against every pin they own. The
        page is anonymous, so without this each caller pays for that join -
        twice over, since ``cost_per_user`` recomputes it.

        Returns:
            The figures and chart series, as template context.
        """
        from django.conf import settings
        from django.core.cache import cache

        from urbanlens.dashboard.services.admin.cost_tracking import (
            active_supporter_count,
            active_user_count,
            cost_per_supporter,
            cost_per_user,
            effective_monthly_cost,
            monthly_cost_series,
            total_hardware_cost,
        )
        from urbanlens.dashboard.services.core.json_safety import safe_json_for_script

        cached = cache.get(_FIGURES_CACHE_KEY)
        if cached is not None:
            return cached

        series = monthly_cost_series()
        figures = {
            "breakdown": effective_monthly_cost(),
            "total_hardware_cost": total_hardware_cost(),
            "cost_per_user": cost_per_user(),
            "active_user_count": active_user_count(),
            "cost_per_supporter": cost_per_supporter(),
            "active_supporter_count": active_supporter_count(),
            "chart_labels": safe_json_for_script(series["labels"]),
            "chart_total": safe_json_for_script([h + o + a for h, o, a in zip(series["hardware"], series["operating"], series["api"], strict=True)]),
        }
        cache.set(_FIGURES_CACHE_KEY, figures, settings.PUBLIC_COSTS_PAGE_CACHE_SECONDS)
        return figures
