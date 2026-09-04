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

import copy
import logging
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from django.db.models import Q

from urbanlens.dashboard.models.abstract.versioned import concrete_field, resolve_fields
from urbanlens.dashboard.models.abstract.versioning import WriteSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.article.model import Article, ArticleRevision
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

    **Currently always False.** The threshold is a reputation score scaled by
    the wiki's community-voted vulnerability, and cannot be chosen before there
    is real score data to choose it against - the ledger is collecting that now.
    Nothing below leaks in production today; every statement here is about the
    day this boolean starts returning True.

    What flipping it does, as of the 2026-08-25 pass (verified against the
    code, not carried over from an earlier draft of this docstring - an
    earlier version of this paragraph was itself found stale by the second
    adversarial review, which is the reason to distrust prose here over code):

    - **Wiki field values** are substituted for every surface, because
      ``wiki_access.resolve_visible_wiki`` conceals what it returns and is the
      one gate all wiki-scoped call sites pass through - including every
      external API handler, each of which calls ``WikiApiView.resolve`` as its
      first statement.
    - **Related rows** - comments (+ reactions), photos, aliases, links, edit
      history, custom layers, map overlays, markup, detail pins, albums,
      ownership/sale records - are filtered by :func:`conceal_rows` /
      :func:`visible_rows` at their call sites, every one of them keyed on
      the same per-model actor field (:data:`_ACTOR_FIELDS`): own+friends'
      rows and automatic/provider rows survive, everyone else's is filtered.
    - **The Article tab** - body, history, and by-id diff/restore - goes
      through :func:`conceal_article` / :func:`visible_article_revision`.
    - **Writes** go to the real row via :func:`writable_wiki`, enforced by
      ``bin/check_concealed_writes.py``; write-path re-renders and by-id
      write endpoints (revert, alias/link/layer delete, etc.) are scoped
      through the same viewer-aware queryset as their GET twin.
    - **Wiki-scoped boundary customization** falls back to the place/circle
      polygon (``Boundary.objects.resolve_for_wiki``) rather than surfacing a
      user-drawn one - that row records no author at all, so it cannot get
      the own-contribution treatment and is hidden outright, the same
      ruling markup and layers had before their own-contribution fix.

    What it does **not** yet cover, so nobody reads a flip as complete:

    - the **provider Image classification** in :data:`_ACTOR_FIELDS` treats
      every non-``UPLOAD`` row as automatic, which is only as sound as every
      writer that materializes a wiki-scoped ``Image`` without a user action -
      audit new writers against that premise before trusting it further;
    - the reputation-ledger **threshold formula** itself does not exist - this
      function needs real logic, not just a flipped boolean.

    Search and autocomplete (``services/global_search/providers.py``,
    ``services/map_pins/autocomplete.py``) are also concealment-aware now: row
    -level providers (comments, articles) filter in SQL the same way
    :func:`conceal_rows` does; the wiki name/description/alias substring match
    re-verifies each candidate against the concealed values before it survives
    a page, since that match cannot be expressed as a single SQL predicate.

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
#: Models where a null actor means the account was deleted, not that nothing
#: authored the row - so the generic "null is automatic" fallback would surface
#: a departed stranger's contribution on the strength of them having left.
#:
#: ``WikiEdit`` qualifies on both counts: its ``editor`` is ``SET_NULL``, and no
#: production path creates one without an editor, so null has exactly one
#: meaning. ``WikiLink.created_by`` is also ``SET_NULL`` and is deliberately
#: *not* here - ``services.locations.external_links`` creates provider links
#: with no author, so null is genuinely ambiguous there, and the product owner
#: ruled links low-stakes because which ones a page carries already varies with
#: search results.
_NULL_ACTOR_IS_A_DELETED_ACCOUNT: frozenset[str] = frozenset({"WikiEdit"})

_ACTOR_FIELDS: dict[str, str] = {
    # A child Wiki is a detail pin. Null means mirrored from building data
    # (services.pins.pin_restructure), which a brand-new wiki would carry, so
    # the generic null-is-automatic rule is right here.
    "Wiki": "created_by_id",
    # Markup items record who drew them. A concealed viewer keeps their own
    # drawings and their friends' - see the note in controllers/markup.py about
    # why that does not reopen what hiding markup was for.
    "PinMarkup": "profile_id",
    "Comment": "profile_id",
    "WikiEdit": "editor_id",
    "WikiLink": "created_by_id",
    "WikiStatVote": "profile_id",
    "Floorplan": "profile_id",
    "MarkupMap": "profile_id",
    # Layers/overlays and reactions carry the same "own contribution is not
    # what concealment exists to hide" reasoning as markup - see
    # controllers/custom_layers.py and controllers/map_overlays.py. `profile`
    # is non-null CASCADE on all three, so the isnull half of conceal_rows'
    # generic clause never fires; that mirrors MarkupMap and is not a bug.
    "CustomLayer": "profile_id",
    "MapImageOverlay": "profile_id",
    "Reaction": "profile_id",
    "Album": "profile_id",
    # Ownership/sale records. `created_by` is SET_NULL on account deletion -
    # but unlike WikiEdit, `plugins.builtin.property_records` also writes real
    # OFFICIAL-sourced rows (deed/mailing-address lookups) with no
    # `created_by` at all, so a null actor here is genuinely ambiguous the
    # same way WikiLink's is - the generic null-is-automatic default is the
    # only one of the two that doesn't need to be wrong for either cause.
    "WikiOwner": "created_by_id",
    "WikiPropertySale": "created_by_id",
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
        # Provider rows stay: they are what a fresh wiki shows. A photo the
        # profile actually contributed stays only when that profile is the
        # viewer or a friend.
        #
        # Keyed on media_source_key rather than on source: a photo out of the
        # uploader's own Immich server, Google Photos library or Flickr account
        # carries that provider's name in `source` while still being their own
        # picture, so `source == UPLOAD` showed a stranger's personal photos to
        # a concealed viewer.
        from urbanlens.dashboard.models.images.queryset import _own_contribution_q

        return queryset.filter(~_own_contribution_q() | Q(profile_id__in=allowed))

    if model_name == "WikiAlias":
        # `created_by` is the intuitive discriminator here and it is wrong: it
        # is NULL for the geocoder backfill *and* for the alias Wiki.save()
        # auto-creates on every rename, so filtering on it would re-expose a
        # name concealed as a field, as an alias row. The durable answer is the
        # alias's own source.
        from urbanlens.dashboard.models.aliases.model import AliasSource

        return queryset.filter(~Q(source=AliasSource.USER) | Q(created_by_id__in=allowed))

    if model_name == "ArticleRevision":
        # A null `editor` means one of two things and the generic
        # null-is-automatic rule gets one of them badly wrong: a system seed
        # from Wikipedia, which a fresh wiki would carry, and an account that
        # has since been deleted, whose prose is a stranger's. The model's own
        # `editor_display_name` distinguishes them by edit summary; so does this.
        from urbanlens.dashboard.models.article.model import SYSTEM_EDIT_SUMMARIES

        return queryset.filter(Q(editor_id__in=allowed) | Q(editor_id__isnull=True, edit_summary__in=SYSTEM_EDIT_SUMMARIES))

    actor_field = _ACTOR_FIELDS.get(model_name)
    if actor_field is None:
        logger.error("conceal_rows: no provenance rule for %s; refusing to guess", model_name)
        return queryset.none()

    if model_name in _NULL_ACTOR_IS_A_DELETED_ACCOUNT:
        return queryset.filter(**{f"{actor_field}__in": allowed})

    return queryset.filter(Q(**{f"{actor_field}__isnull": True}) | Q(**{f"{actor_field}__in": allowed}))


def visible_rows(queryset: Any, wiki: Wiki, viewer: Profile | None) -> Any:
    """A wiki-scoped queryset narrowed to what *viewer* may see.

    :func:`conceal_rows` plus the gate check, which every caller was writing out
    as the same three lines. Worth one name for two reasons beyond brevity: it
    is what a by-id lookup should be scoped to, and spelling that out per call
    site is how nine of them ended up scoped to the wiki instead of the viewer -
    an existence oracle that answers "is there a row N here" for rows
    concealment has already decided the account cannot see, and, on the
    mutating routes, lets it act on one.

    Args:
        queryset: Rows already scoped to one wiki.
        wiki: The wiki they belong to, for the concealment decision.
        viewer: Who is looking, or None when signed out.

    Returns:
        The queryset, narrowed when this viewer is concealed.
    """
    return conceal_rows(queryset, viewer) if concealment_active(wiki, viewer) else queryset


def _real_row(wiki: Wiki) -> Wiki:
    """Re-read *wiki* from the database, discarding any concealment applied to it."""
    from urbanlens.dashboard.models.wiki.model import Wiki as WikiModel

    return WikiModel.objects.select_related("location", "place").get(pk=wiki.pk)


def _refuse_write(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Refuse a write through a concealed projection.

    Loud on purpose. A projection carries concealed values for fields this
    viewer may not see, so persisting it would write those values over the real
    row - a concealment bug that silently destroys community content. Write
    paths must act on a freshly fetched row, or use ``queryset.update()``.
    """
    raise TypeError("this Wiki is a concealed projection and must not be written; re-fetch the row")


def conceal_wiki(wiki: Wiki, viewer: Profile | None) -> Wiki:
    """Return *wiki* itself, or a concealed projection of it.

    **A real ``Wiki`` instance, not a wrapper.** An earlier version returned a
    proxy object with ``__getattr__`` delegation, which failed *open*: anything
    the proxy did not explicitly override - and there is far more of that than
    of what it did - fell through to the real row. It also could not be used as
    a foreign key value, so every write path had to be taught about it.

    Returning a copy of the row with substituted field values inverts that.
    Templates, serializers and FK assignment all work unchanged because it *is*
    a Wiki, with the real primary key; and the substitution is a property of the
    object rather than something each reader has to remember to ask for.

    Writes are refused (see :func:`_refuse_write`), because the one thing that
    must never happen is a concealed value being persisted over a real one.

    Related managers are **not** concealed by this - ``projection.comments`` is
    keyed on the primary key and returns every row. Rows are filtered by
    :func:`conceal_rows` at the queryset, which is a separate mechanism.

    Args:
        wiki: The real row.
        viewer: Who is looking, or None when signed out.

    Returns:
        The real wiki, or a write-refusing projection of it.
    """
    viewer_key = viewer.pk if viewer else None
    if is_concealed(wiki):
        # Idempotent, and cheaply so: several surfaces still call this on a wiki
        # that resolve_visible_wiki already concealed, and re-resolving the
        # write history to reach the same answer is pure cost.
        if wiki._ul_concealed_for == viewer_key:  # noqa: SLF001
            return wiki
        # Built for somebody else. One viewer's projection must never be handed
        # to another, and it must not be re-concealed either - it no longer
        # carries the values a rebuild needs. Go back to the row.
        wiki = _real_row(wiki)

    if not concealment_active(wiki, viewer):
        return wiki

    projection = copy.copy(wiki)
    # A shallow copy shares ``_state`` with the row it came from, and ``_state``
    # is where Django caches fetched relations. Every versioned field is scalar
    # today, so nothing would notice - but the day one becomes a foreign key,
    # assigning it below would reach through and overwrite the *real* row's
    # cached relation. ``fields_cache`` needs copying separately: copying the
    # state object alone leaves both pointing at the same cache dict.
    projection._state = copy.copy(wiki._state)  # noqa: SLF001
    projection._state.fields_cache = dict(wiki._state.fields_cache)  # noqa: SLF001
    for name, value in concealed_field_values(wiki, viewer).items():
        setattr(projection, name, value)

    # Marked so a reader can assert on it, and so tests can tell a projection
    # from the row it came from.
    projection._ul_concealed = True  # noqa: SLF001
    projection._ul_concealed_for = viewer_key  # noqa: SLF001
    projection.save = _refuse_write  # type: ignore[method-assign]
    projection.delete = _refuse_write  # type: ignore[method-assign]
    return projection


class Concealable(Protocol):
    """A model that can exist as a concealed projection of itself.

    Two do - ``Wiki`` and ``Article`` - and both declare the marker on the model
    rather than having it set on them ad hoc, so the distinction is visible
    where the row is defined. Stated as a protocol because "carries the marker"
    is the actual requirement; the two share no base class that would be true
    of, and inventing one would claim every model is concealable.
    """

    _ul_concealed: bool


def is_concealed(row: Concealable) -> bool:
    """Whether this object is a concealed projection rather than a real row."""
    return row._ul_concealed  # noqa: SLF001


def writable_wiki(wiki: Wiki) -> Wiki:
    """Return a row that may be written: *wiki* itself, or a fresh fetch of it.

    Every write path downstream of ``resolve_visible_wiki`` needs this. That
    function now returns a concealed projection to viewers who are gated, and a
    projection carries substituted values for the fields its viewer may not see
    - so saving it would write concealment over real community content, which is
    why the projection refuses ``save()`` outright.

    Refusing is the right default but the wrong ending: a gated viewer setting a
    cover photo would get a 500, and a 500 only this account receives is exactly
    the tell concealment exists to avoid. Re-reading the row gives the write real
    values to act on, and leaves the viewer's own reads concealed.

    Args:
        wiki: A wiki, possibly a projection.

    Returns:
        A wiki safe to mutate and save.
    """
    if not is_concealed(wiki):
        return wiki

    return _real_row(wiki)


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


def visible_article_revision(article: Article, viewer: Profile | None) -> ArticleRevision | None:
    """The newest article revision *viewer* is entitled to see, or None.

    An article is entirely user-contributed prose, so unlike a wiki's fields it
    cannot be resolved write-by-write - there is nothing to merge on. What it
    has instead is better: every ``ArticleRevision`` stores the *complete*
    Markdown source as of that revision, so showing a concealed viewer the
    newest revision they may see needs no reconstruction at all. This is the
    "view the wiki at this revision" idea applied to the one model that already
    supports it.

    A null ``editor`` is deliberately **not** treated as automatic. It means one
    of two things - a system-initiated seed from Wikipedia, or an account that
    has since been deleted - and the model's own ``editor_display_name`` goes to
    the trouble of telling them apart. Collapsing them here would show a
    concealed viewer a stranger's prose on the strength of that stranger having
    closed their account.

    Args:
        article: The article being rendered.
        viewer: Who is looking, or None when signed out.

    Returns:
        The newest revision this viewer may see, or None when there is none -
        which is what a place nobody has written up looks like.
    """
    return conceal_rows(article.revisions.all(), viewer).order_by("-created", "-pk").first()


def conceal_article(article: Article | None, wiki: Wiki, viewer: Profile | None) -> Article | None:
    """Return *article* itself, a projection of it, or None.

    None is a real answer here, and the common one: a place nobody the viewer
    can see has written up has no article, and that is exactly the state a
    brand-new wiki is in. Returning an empty article instead would be its own
    tell - the panel renders an always-editable canvas either way, so absence
    and emptiness look the same to the viewer, but only one of them is honest
    about what the row contains.

    **Known limitation.** A friend's revision is shown verbatim, and a friend
    editing on top of a stranger's text carries that text forward. This is the
    same trade the field rules already make - ``resolve_fields`` returns the
    friend's value, whatever they based it on - and closing it properly needs
    the merge mechanics tracked in ``docs/designs/versioned-content.md``, not a
    special case here.

    Args:
        article: The live article row, or None when none exists.
        wiki: The wiki hosting it, for the concealment decision.
        viewer: Who is looking, or None when signed out.

    Returns:
        An article safe to render to this viewer, or None.
    """
    if article is None or not concealment_active(wiki, viewer):
        return article

    revision = visible_article_revision(article, viewer)
    if revision is None:
        return None

    # The overwhelmingly common case once a viewer has any visible revision:
    # theirs is also the newest, so the live row already says what to show and
    # re-rendering it would be pure cost.
    if revision.content == article.content:
        return article

    from urbanlens.dashboard.services.wiki.articles import render_article

    rendered = render_article(revision.content)
    projection = copy.copy(article)
    projection._state = copy.copy(article._state)  # noqa: SLF001
    projection._state.fields_cache = dict(article._state.fields_cache)  # noqa: SLF001
    projection.content = revision.content
    projection.content_html = rendered.html
    projection.toc = rendered.toc
    projection._ul_concealed = True  # noqa: SLF001
    projection.save = _refuse_write  # type: ignore[method-assign]
    projection.delete = _refuse_write  # type: ignore[method-assign]
    return projection
