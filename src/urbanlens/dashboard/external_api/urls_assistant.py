"""External-API routes for the AI assistant.

Everything AI-shaped is collected here rather than being scattered next to the resource it talks
about, because these calls share properties nothing else in this API has.
They cost real money per invocation and must record that cost; they are slow enough to need a
job-shaped request/poll flow with a client progress indicator; they need their own throttle, since
the relevant limit is spend rather than requests per minute; and their output is a *suggestion*,
never a fact - so no route here may write to a pin or a wiki without a separate, explicit
confirmation step from the user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_assistant

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
urlpatterns: list[URLPattern] = [
    path("assistant/message/", views_assistant.AssistantMessageView.as_view(), name="assistant.message"),
    path("assistant/turn/<str:turn_id>/", views_assistant.AssistantTurnPollView.as_view(), name="assistant.turn"),
    path("assistant/turn/<str:turn_id>/confirm/<int:n>/", views_assistant.AssistantProposalConfirmView.as_view(), name="assistant.proposal.confirm"),
    path("assistant/reset/", views_assistant.AssistantResetView.as_view(), name="assistant.reset"),
]
