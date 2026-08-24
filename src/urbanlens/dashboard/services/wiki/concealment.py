"""Deciding what a wiki shows to a viewer who has not earned its detail yet.

The goal, in the product owner's words: make *"users have gone to this place and
edited this wiki"* indistinguishable from *"no users have gone to this place or
edited this wiki"*.

Not the same as hiding the wiki. A wiki row exists for every place, so absence
is itself a tell - and so is any visible difference in how the site behaves for
this account. A concealed wiki renders, in the state it would plausibly have had
when first created. See ``docs/designs/reputation-and-gating.md`` R16.

What a concealed viewer sees is the union of three things:

1. **automatic** writes - provider and enrichment data, which a brand-new wiki
   would carry and which is public information the site merely relays;
2. **their own** contributions, as everywhere else in the app;
3. **their friends'** contributions - because friends talk offline, and "just
   check the wiki, I put a load of stuff up there" has to work, or concealment
   breaks the product for the people it is not aimed at.

That third clause is why this cannot be a stored projection: the visible set
differs for every viewer. It is a filter over recorded writes, resolved per
request. See ``docs/designs/versioned-content.md``.

**This module never grants access.** Whether a viewer may reach a wiki at all is
the place-domain rule in ``wiki_access``; this decides only what a wiki they can
already reach shows them. The two are conjunctive and independent, and keeping
them so is what stops concealment becoming an access-control bypass.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db.models import Q

from urbanlens.dashboard.models.abstract.versioned import concrete_field, resolve_fields
from urbanlens.dashboard.models.abstract.versioning import WriteSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Fields forced to their unset value regardless of who wrote them. Security
#: indicators are the one category the product owner ruled on directly: always
#: unset, whether a person or a provider supplied them, because the whole point
#: of concealment is that a place must not read as one people have surveyed.
ALWAYS_UNSET: tuple[str, ...] = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

#: Cached per request on the Profile instance, like
#: ``visible_wiki_location_ids_cached`` - a Profile is loaded fresh per request,
#: so the entry cannot outlive one, and nothing has to invalidate it.
_FRIEND_CACHE_ATTR = "_ul_accepted_friend_ids"


def accepted_friend_ids(profile: Profile) -> set[int]:
    """Return the profile pks this profile has an accepted friendship with.

    Both directions: a ``Friendship`` row is one relationship, and which side
    sent the request says nothing about whether they are friends now. Mute is
    deliberately not consulted - it is a notification-volume control, and a
    muted friend is still a friend.

    Args:
        profile: The viewer.

    Returns:
        Profile pks, not including the viewer's own.
    """
    cached = getattr(profile, _FRIEND_CACHE_ATTR, None)
    if cached is not None:
        return cached

    from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus

    rows = Friendship.objects.filter(
        Q(from_profile=profile) | Q(to_profile=profile),
        status=FriendshipStatus.ACCEPTED,
    ).values_list("from_profile_id", "to_profile_id")

    ids = {pk for row in rows for pk in row if pk != profile.pk}
    setattr(profile, _FRIEND_CACHE_ATTR, ids)
    return ids


def visible_actor_ids(profile: Profile | None) -> set[int]:
    """Return whose contributions a concealed viewer may still see.

    Args:
        profile: The viewer, or None for a signed-out caller.

    Returns:
        The viewer's own pk plus their accepted friends'.
    """
    if profile is None:
        return set()
    return {profile.pk} | accepted_friend_ids(profile)


def concealed_field_values(wiki: Wiki, viewer: Profile | None) -> dict[str, Any]:
    """Return the field values a concealed viewer should be shown.

    Reads the recorded write history rather than the live row: the live row is
    the union of everybody's edits, and this viewer is entitled to a subset of
    them.

    Args:
        wiki: The wiki being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        ``{field_name: value}`` covering every versioned field. A field nobody
        the viewer can see has ever written resolves to the model default,
        which is what a brand-new wiki would show.
    """
    resolved = resolve_fields(
        wiki,
        sources=(WriteSource.AUTOMATIC,),
        actor_ids=visible_actor_ids(viewer),
    )

    values: dict[str, Any] = {}
    for name in wiki.versioned_fields:
        field = concrete_field(type(wiki), name)
        if field is None:
            continue
        if name not in ALWAYS_UNSET and name in resolved:
            values[name] = resolved[name]
            continue
        # Either the field is unset by rule, or nobody this viewer can see has
        # written it. Either way fall back to the field's default rather than
        # the live value - the live value is precisely what is being concealed.
        values[name] = field.get_default()
    return values


def concealment_active(wiki: Wiki, viewer: Profile | None) -> bool:
    """Whether this viewer should be shown the concealed form of this wiki.

    **Currently always False.** The predicate is a reputation threshold scaled
    by the wiki's community-voted vulnerability, and the threshold cannot be
    chosen before there is real score data to choose it against - see the
    reputation ledger, which is collecting that now. Landing the mechanism
    behind a stub keeps every call site written and exercised in the meantime,
    so turning it on later is a change to one function rather than a sweep.

    Deliberately the *only* place this decision is made. The tells audit found
    82 ways a concealed wiki could give itself away, and most of the classes
    behind them exist because a rule was spelled out per call site instead of
    once.

    Args:
        wiki: The wiki being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        Whether to conceal.
    """
    return False


def concealed_community_summary() -> dict[str, Any]:
    """The community card as it reads for a place nobody has pinned but you.

    Returns the empty state **without calling**
    ``services.wiki.community_counts.approximate_pin_count``, and that is the
    whole point of this function existing rather than the caller filtering an
    input queryset.

    That fuzz caches its value for a day keyed **only on the id passed in**,
    with no viewer in the key. So a concealed viewer who reached it would be
    handed the number an ordinary viewer had already populated - defeating
    concealment silently, only under concurrency, and only in the window after
    somebody else loaded the page. Re-keying the cache per viewer would not fix
    it either: it would just make the fuzz per-viewer averageable, which is the
    attack the fuzz exists to stop.

    Returns:
        The same shape ``wiki_community_summary`` returns, in its empty state.
    """
    return {
        "pin_count_low": True,
        "pin_count_approx": None,
        "first_pinned": None,
        "first_pinned_precision": "month",
    }
