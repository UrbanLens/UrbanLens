"""External-API routes for the social graph.

Owns everything about the relationships between people rather than between a person and a place:
friendships and their state transitions, invitations, blocks and mutes, follows, and whatever a
profile chooses to expose to the people connected to it.
The first generation of these routes (``friends/``, ``friends/<uuid>/accept/``, ``friend-invites/``)
predates this split and still lives in ``urls.py``; every new social endpoint belongs here instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import path

from urbanlens.dashboard.external_api import views_social

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern

#: Routes contributed by this domain. ``friends/<uuid>/unblock/`` uses a uuid like its sibling transitions
#: rather than the slug the profile routes use; the friendship surface has always addressed the other party by
#: uuid, and switching identifier mid-domain would force clients to hold both.
urlpatterns: list[URLPattern] = [
    path("friends/<uuid:profile_uuid>/unblock/", views_social.FriendUnblockView.as_view(), name="friends.unblock"),
    path("profiles/<str:profile_slug>/avatar/", views_social.ProfileAvatarView.as_view(), name="profiles.avatar"),
    path("profiles/<str:profile_slug>/avatar/emoji/", views_social.ProfileAvatarEmojiView.as_view(), name="profiles.avatar.emoji"),
    path("profiles/<str:profile_slug>/annotations/", views_social.ProfileAnnotationsView.as_view(), name="profiles.annotations"),
    path("profiles/<str:profile_slug>/nickname/", views_social.ProfileNicknameView.as_view(), name="profiles.nickname"),
    path("profiles/<str:profile_slug>/trust/", views_social.ProfileTrustView.as_view(), name="profiles.trust"),
    path("profiles/<str:profile_slug>/social-links/", views_social.ProfileSocialLinksView.as_view(), name="profiles.social_links"),
]
