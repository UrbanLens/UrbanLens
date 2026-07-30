"""External-API routes for messaging beyond the first generation.

Owns the direct-message and group-chat endpoints added after ``urls.py`` was
split: per-thread mute, group membership exits, group message deletion and
reactions. The original messaging routes (``messages/conversations/``,
``messages/groups/…``, ``messages/<peer_slug>/…``) predate the split and still
live in ``urls.py``; every new messaging endpoint belongs here instead.

Two constraints this domain carries that the others do not:

- **Reserved peer slugs.** ``messages/<str:peer_slug>/`` is a catch-all
  segment, so any new literal under ``messages/`` is a potential collision with
  a real profile whose slug happens to match. ``views_messaging.RESERVED_PEER_SLUGS``
  refuses those slugs as peers, and
  :func:`~urbanlens.dashboard.external_api.urls.order_by_specificity` sorts
  literals ahead of converters - both defenses have to fail before a request
  can be misrouted. A new literal directly under ``messages/`` must be added to
  ``RESERVED_PEER_SLUGS`` as well.
- **Scope kind, not just scope value.** ``messages:read``/``messages:write``
  are in ``permissions.OAUTH2_ONLY_SCOPES``: a PAT-style key can never reach
  them, only a consented OAuth2 token. Nothing routed here should assume a
  bearer credential of any kind will do.

Wiring: ``urls.py`` concatenates the ``urlpatterns`` below into the flat
``external_api:`` namespace and re-sorts the combined list with
:func:`~urbanlens.dashboard.external_api.urls.order_by_specificity`, so
declaration order inside this module only breaks ties between routes of
identical shape. Use ``path()`` (``re_path()`` cannot be ordered and is
rejected at import time) and keep every ``name=`` unique across the whole
external API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_messaging

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
#:
#: None of these introduce a new literal *directly* under ``messages/``, so
#: ``views_messaging.RESERVED_PEER_SLUGS`` needs no addition: the group routes
#: all sit under the already-reserved ``groups`` segment, and the one-to-one
#: mute route's first segment is the ``<str:peer_slug>`` converter itself.
#: Adding, say, ``messages/archive/`` later *would* require reserving
#: ``"archive"`` - a user whose profile slug is "archive" would otherwise
#: shadow it.
urlpatterns: list[URLPattern] = [
    path("messages/groups/<uuid:group_uuid>/leave/", views_messaging.GroupLeaveView.as_view(), name="messages.groups.leave"),
    path("messages/groups/<uuid:group_uuid>/mute/", views_messaging.GroupMuteView.as_view(), name="messages.groups.mute"),
    path("messages/groups/<uuid:group_uuid>/messages/<int:message_id>/", views_messaging.GroupMessageDetailView.as_view(), name="messages.groups.messages.detail"),
    path("messages/groups/<uuid:group_uuid>/messages/<int:message_id>/react/", views_messaging.GroupMessageReactionView.as_view(), name="messages.groups.messages.react"),
    path("messages/<str:peer_slug>/mute/", views_messaging.ConversationMuteView.as_view(), name="messages.mute"),
]
