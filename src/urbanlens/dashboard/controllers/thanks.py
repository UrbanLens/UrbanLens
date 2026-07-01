"""Thanks / credits page."""

from __future__ import annotations

from django.views.generic import TemplateView

from urbanlens.dashboard.services.apis.infra.github.contributors import (
    GITHUB_REPO_URL,
    get_github_contributors,
)


class ThanksView(TemplateView):
    """Render the thanks page with live GitHub contributor data."""

    template_name = "dashboard/pages/thanks/index.html"

    def get_context_data(self, **kwargs):
        """Add GitHub contributor metadata to the template context.

        Args:
            **kwargs: Standard ``TemplateView`` keyword arguments.

        Returns:
            Template context including ``github_contributors`` and ``github_repo_url``.
        """
        context = super().get_context_data(**kwargs)
        context["page_name"] = "thanks"
        context["github_contributors"] = get_github_contributors()
        context["github_repo_url"] = GITHUB_REPO_URL
        return context
