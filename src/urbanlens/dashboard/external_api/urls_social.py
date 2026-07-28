"""External-API routes for the social graph.

Owns everything about the relationships between people rather than between a
person and a place: friendships and their state transitions, invitations,
blocks and mutes, follows, and whatever a profile chooses to expose to the
people connected to it. The first generation of these routes (``friends/``,
``friends/<uuid>/accept/``, ``friend-invites/``) predates this split and still
lives in ``urls.py``; every new social endpoint belongs here instead.

Because this domain exposes other people's identities, its views carry the
same anti-enumeration obligation as the trip and wiki surfaces: a profile the
caller may not see must be indistinguishable from a profile that does not
exist. Route naming should keep that in mind - never expose a numeric pk in a
path when a uuid or slug will do.

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

from urbanlens.dashboard.external_api import views_social

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. Appended to the flat ``external_api:``
#: namespace by ``urls.py`` - see this module's docstring before adding to it.
#:
#: The ``profiles/<str:profile_slug>/...`` routes deliberately sit under the
#: same prefix as ``profiles.detail`` in ``urls.py``, which is a bare
#: ``<str:profile_slug>/``. ``order_by_specificity`` keeps the longer, literal
#: -suffixed routes ahead of it, so ``avatar``/``annotations``/``nickname``/
#: ``trust`` cannot be swallowed as slugs no matter how the modules concatenate.
#:
#: ``friends/<uuid>/unblock/`` uses a uuid like its sibling transitions rather
#: than the slug the profile routes use; the friendship surface has always
#: addressed the other party by uuid, and switching identifier mid-domain would
#: force clients to hold both.
urlpatterns: list[URLPattern] = [
    path("friends/<uuid:profile_uuid>/unblock/", views_social.FriendUnblockView.as_view(), name="friends.unblock"),
    path("profiles/<str:profile_slug>/avatar/", views_social.ProfileAvatarView.as_view(), name="profiles.avatar"),
    path("profiles/<str:profile_slug>/avatar/emoji/", views_social.ProfileAvatarEmojiView.as_view(), name="profiles.avatar.emoji"),
    path("profiles/<str:profile_slug>/annotations/", views_social.ProfileAnnotationsView.as_view(), name="profiles.annotations"),
    path("profiles/<str:profile_slug>/nickname/", views_social.ProfileNicknameView.as_view(), name="profiles.nickname"),
    path("profiles/<str:profile_slug>/trust/", views_social.ProfileTrustView.as_view(), name="profiles.trust"),
]
