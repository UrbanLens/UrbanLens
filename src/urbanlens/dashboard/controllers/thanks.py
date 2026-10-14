"""Thanks / credits page."""

from __future__ import annotations

from dataclasses import replace

from django.views.generic import TemplateView

from urbanlens.dashboard.plugins.points import SLOT_THANKS_ATTRIBUTIONS
from urbanlens.dashboard.services.apis.infra.github.contributors import (
    GITHUB_REPO_URL,
    get_github_contributors,
)
from urbanlens.dashboard.services.credits import credits_page_categories, map_cards
from urbanlens.dashboard.services.media.remote_copies import RemoteImage, copy_urls


class ThanksView(TemplateView):
    """Render the thanks page with GitHub contributor data."""

    template_name = "dashboard/pages/thanks/index.html"

    def get_context_data(self, **kwargs):
        """
        Add GitHub contributor metadata to the template context.

        Args:
            **kwargs: Standard ``TemplateView`` keyword arguments.

        Returns:
            Template context including ``github_contributors`` and ``github_repo_url``.
        """
        context = super().get_context_data(**kwargs)
        context["page_name"] = "thanks"
        contributors = get_github_contributors()
        copies = copy_urls(RemoteImage(contributor.avatar_url, "github", contributor.profile_url) for contributor in contributors)
        context["github_contributors"] = [replace(contributor, avatar_url=copies.get(contributor.avatar_url, contributor.avatar_url)) for contributor in contributors]
        context["github_repo_url"] = GITHUB_REPO_URL
        context["credit_groups"] = credits_page_categories()
        context["map_cards"] = map_cards()
        context["has_attributions"] = any(True for _ in SLOT_THANKS_ATTRIBUTIONS)
        return context
