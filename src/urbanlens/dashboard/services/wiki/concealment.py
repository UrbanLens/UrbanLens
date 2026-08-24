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

    # `name` has no model default, so the fallback above yields "". No wiki ever
    # looks like that: every creation path names it from the location
    # (WikiManager.claim_for_location, get_or_create_draft_for_location). A
    # blank title would announce the concealment as loudly as a leak would, so
    # reproduce what a brand-new wiki would have shown.
    if not values.get("name"):
        from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

        location = wiki.location
        values["name"] = (location.official_name if location else "") or WikiModel.objects._placeholder_name(location)  # noqa: SLF001
    return values


def concealment_active(wiki: Wiki, viewer: Profile | None) -> bool:
    """Whether this viewer should be shown the concealed form of this wiki.

    **Currently always False, and turning it True is NOT sufficient.** Read this
    before flipping it.

    Four things are wired to it today: the ``viewed_by_other`` write, the
    community pin-count summary, the stat composites and the boundary-vote
    dialog. Everything else on the wiki page still renders from the live row -
    the name, description, dates, the eight security chips, comments, images,
    aliases, links and the whole edit history. ``concealed_field_values`` and
    ``conceal_rows`` exist, are tested, and have **no production callers**;
    ``services/wiki/wiki_detail.build_wiki_detail``, which serves the external
    API, does not consult this module at all.

    So flipping this boolean conceals four things and leaves the rest visible,
    which is worse than not concealing: it is the *inconsistent* state, where a
    place looks unvisited in its counts and fully documented in its content.

    The threshold itself is a reputation score scaled by the wiki's
    community-voted vulnerability, and cannot be chosen before there is real
    score data to choose it against - the ledger is collecting that now.

    Deliberately the only place this decision is made. The tells audit found 82
    ways a concealed wiki could give itself away, and most of the classes behind
    them exist because a rule was spelled out per call site instead of once.

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


#: How to tell, per related model, whether a row was contributed by a person
#: and if so by whom.
#:
#: Recorded here as a table rather than as a rule each queryset re-states,
#: because the failure mode this whole feature exists to avoid is a rule spelled
#: out per call site and forgotten at one of them.
#:
#: The shape is uniform: a row is **automatic** when it has no actor, and
#: **user-contributed** when it does. Two models need a second clause because
#: their actor column means something other than authorship:
#:
#: - ``Image.profile`` is the *up-voter* on a materialised provider row, not the
#:   photographer, so authorship is only meaningful when ``source == UPLOAD``.
#: - ``WikiAlias`` carries its own ``source``, and the geocoder backfill writes
#:   ``created_by=NULL`` with an official source.
_ACTOR_FIELDS: dict[str, str] = {
    "Comment": "profile_id",
    "WikiEdit": "editor_id",
    "WikiLink": "created_by_id",
    "WikiStatVote": "profile_id",
    "Floorplan": "profile_id",
    "MarkupMap": "profile_id",
}


def conceal_rows(queryset: Any, viewer: Profile | None) -> Any:
    """Narrow a wiki-scoped queryset to what a concealed viewer may see.

    Keeps rows nobody contributed (provider and enrichment data, which a
    brand-new wiki would carry) and rows contributed by the viewer or one of
    their friends. Drops everybody else's.

    Args:
        queryset: Rows already scoped to one wiki.
        viewer: Who is looking, or None when signed out.

    Returns:
        The narrowed queryset. Unchanged when the model is not in
        :data:`_ACTOR_FIELDS` and has no special case, so a caller cannot
        silently get an unfiltered result for a model this does understand -
        see the KeyError path.
    """
    from urbanlens.dashboard.models.images.model import Image, ImageSource

    model_name = queryset.model.__name__
    allowed = visible_actor_ids(viewer)

    if queryset.model is Image:
        # Provider rows stay: they are what a fresh wiki shows. Uploads stay
        # only when the uploader is the viewer or a friend.
        return queryset.filter(~Q(source=ImageSource.UPLOAD) | Q(profile_id__in=allowed))

    if model_name == "WikiAlias":
        # `created_by` is the intuitive discriminator here and it is wrong: it
        # is NULL for the geocoder backfill *and* for the alias Wiki.save()
        # auto-creates on every rename, so filtering on it would re-expose a
        # name concealed as a field, as an alias row. The durable answer is the
        # alias's own source.
        from urbanlens.dashboard.models.aliases.model import AliasSource

        return queryset.filter(~Q(source=AliasSource.USER) | Q(created_by_id__in=allowed))

    actor_field = _ACTOR_FIELDS.get(model_name)
    if actor_field is None:
        logger.error("conceal_rows: no provenance rule for %s; refusing to guess", model_name)
        return queryset.none()

    return queryset.filter(Q(**{f"{actor_field}__isnull": True}) | Q(**{f"{actor_field}__in": allowed}))


class ConcealedWiki:
    """A read-only stand-in for a Wiki, showing only what a viewer may see.

    Everything not overridden delegates to the real row, so a template or
    serializer can use this wherever it used a ``Wiki`` and pick up concealment
    without knowing it exists. That is the point: the tells audit found 82 ways
    a concealed wiki gives itself away, and nearly every class behind them comes
    from a rule applied at some call sites and forgotten at others.

    **Writes raise.** Not tidiness - it is the mechanism that turns a
    write-through-the-proxy bug into a loud failure instead of a silent one that
    persists a concealed value back over the real row.
    """

    def __init__(self, wiki: Wiki, viewer: Profile | None) -> None:
        """Wrap *wiki* as *viewer* is entitled to see it.

        Args:
            wiki: The real row.
            viewer: Who is looking, or None when signed out.
        """
        self._wiki = wiki
        self._viewer = viewer
        self._values = concealed_field_values(wiki, viewer)

    def __getattr__(self, name: str) -> Any:
        """Return a concealed field value, or delegate to the real row."""
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]

        wiki = object.__getattribute__(self, "_wiki")
        # get_<field>_display() reads the model's own attribute, so delegating
        # it would hand back the real value's label for a field we just
        # concealed - the security chips render through exactly this.
        if name.startswith("get_") and name.endswith("_display"):
            field_name = name[len("get_") : -len("_display")]
            if field_name in values:
                field = concrete_field(type(wiki), field_name)
                choices = dict(getattr(field, "choices", None) or [])
                return lambda: choices.get(values[field_name], values[field_name])
        return getattr(wiki, name)

    def __str__(self) -> str:
        return str(self.name)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Refuse. A concealed projection must never be written back."""
        raise TypeError("ConcealedWiki is read-only; write to the real Wiki row")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Refuse, for the same reason as :meth:`save`."""
        raise TypeError("ConcealedWiki is read-only; delete the real Wiki row")


def conceal_wiki(wiki: Wiki, viewer: Profile | None) -> Wiki | ConcealedWiki:
    """Return *wiki* itself, or a concealed stand-in when the viewer is gated.

    The one call every renderer should make. Returning the real row unchanged
    when concealment is off keeps the ordinary path free of both a wrapper and
    a branch at each call site.

    Args:
        wiki: The row being rendered.
        viewer: Who is looking.

    Returns:
        The real wiki, or a read-only concealed view of it.
    """
    return ConcealedWiki(wiki, viewer) if concealment_active(wiki, viewer) else wiki


def redact_edit_changes(changes: Any) -> dict[str, Any]:
    """Strip the pre-edit value out of an edit's diff.

    ``WikiEdit.changes`` is ``{"field": {"from": old, "to": new}}``, and the
    history page renders both halves. That is a leak a read gate cannot close,
    because it lands in the viewer's *own* edit row - content the rules promise
    always to show them. Type one character into a description that looks empty,
    open your own history, and read the hidden value out of the "from" side.

    The "to" side is kept: it is what this viewer wrote, and concealing it would
    make their own history unreadable for no gain.

    Args:
        changes: The edit's stored diff.

    Returns:
        The same shape with every ``from`` replaced.
    """
    if not isinstance(changes, dict):
        return {}
    redacted: dict[str, Any] = {}
    for field_name, diff in changes.items():
        if isinstance(diff, dict):
            redacted[field_name] = {**diff, "from": None}
        else:
            redacted[field_name] = diff
    return redacted
