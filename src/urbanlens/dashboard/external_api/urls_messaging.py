"""External-API routes for messaging beyond the first generation.

The original messaging routes (``messages/conversations/``, ``messages/groups/…``,
``messages/<peer_slug>/…``) predate the split and still live in ``urls.py``; every new messaging
endpoint belongs here instead.
Use ``path()`` (``re_path()`` cannot be ordered and is rejected at import time) and keep every
``name=`` unique across the whole external API.

- **Reserved peer slugs.** ``messages/<str:peer_slug>/`` is a catch-all segment, so any new literal
  under ``messages/`` is a potential collision with a real pr...
- **Scope kind, not just scope value.** ``messages:read``/``messages:write`` are in
  ``permissions.OAUTH2_ONLY_SCOPES``: a PAT-style key can never reach them, o...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_messaging

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Adding, say, ``messages/archive/`` later *would* require reserving
#: ``"archive"`` - a user whose profile slug is "archive" would otherwise shadow it.
urlpatterns: list[URLPattern] = [
    path("messages/groups/<uuid:group_uuid>/leave/", views_messaging.GroupLeaveView.as_view(), name="messages.groups.leave"),
    path("messages/groups/<uuid:group_uuid>/mute/", views_messaging.GroupMuteView.as_view(), name="messages.groups.mute"),
    path("messages/groups/<uuid:group_uuid>/messages/<int:message_id>/", views_messaging.GroupMessageDetailView.as_view(), name="messages.groups.messages.detail"),
    path("messages/groups/<uuid:group_uuid>/messages/<int:message_id>/react/", views_messaging.GroupMessageReactionView.as_view(), name="messages.groups.messages.react"),
    path("messages/<str:peer_slug>/mute/", views_messaging.ConversationMuteView.as_view(), name="messages.mute"),
]
