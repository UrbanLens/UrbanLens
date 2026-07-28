"""Promoting one of a wiki's alternate names to be its community name.

The wiki counterpart of ``services.pin_subresources.promote_alias_to_name``, and
extracted for the same reason: the operation used to live only inside
``controllers.aliases.LocationAliasUseView``, so the external API had nowhere to
call it from and would otherwise have grown a second copy. A second copy is not
a cosmetic problem here - a wiki rename is an *audited community edit*, and the
audit trail is the thing people use to argue about and undo each other's
changes. Two implementations means one of them eventually renames a wiki without
writing history, and the change becomes invisible and unrevertible.

Everything below is therefore expressed in terms of
:func:`~urbanlens.dashboard.services.wiki_edits.apply_wiki_edit`, the same
function the "Suggest edits" form goes through, rather than by assigning
``wiki.name`` and hand-rolling a ``WikiEdit`` row. That buys the no-op rule, the
audit format, and the ``Wiki.save()`` alias sync for free, and guarantees a
rename via "use this name" is indistinguishable in history from a rename typed
into the edit form - because it is the same write.

This module deliberately does **not** load or authorize anything. Callers hand
in an already-resolved wiki and an alias they have already scoped to it; see
``external_api/views_wiki.py``'s module docstring for why alias ids must never
be looked up bare.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, normalize_name_for_comparison
from urbanlens.dashboard.services.wiki_edits import apply_wiki_edit

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit


def alias_is_current_name(alias: WikiAlias, wiki: Wiki | None) -> bool:
    """Whether *alias* is the name the wiki currently goes by.

    The comparison is loose - :func:`normalize_name_for_comparison` folds case,
    spacing and punctuation - because that is the same rule the alias uniqueness
    constraint and the "you can't delete the current name" guard use. A stricter
    comparison here would let a UI show two aliases that the delete guard treats
    as one, and the user would be told they cannot remove a name that is not
    visibly the current one.

    *wiki* is passed in rather than read off ``alias.wiki`` so that flagging a
    whole list of aliases costs no per-row query.

    Args:
        alias: The alias under consideration.
        wiki: The wiki it belongs to, or None when the caller has no wiki in
            hand (in which case nothing can be the current name).

    Returns:
        True when *alias* names the wiki as it currently stands.
    """
    if wiki is None:
        return False
    current = normalize_name_for_comparison(wiki.name)
    return bool(current) and normalize_name_for_comparison(alias.name) == current


def promote_wiki_alias_to_name(wiki: Wiki, profile: Profile, alias: WikiAlias) -> WikiEdit | None:
    """Make *alias* the wiki's community name, recording it in the edit history.

    Promoting the alias that is *already* the name is a no-op rather than an
    error: "use this name" is an idempotent statement of intent, and a client
    that retries a request whose response it never saw must not be punished for
    it. ``apply_wiki_edit`` supplies that behavior by refusing to record a
    change whose new value equals the old one. Note that comparison is exact
    while :func:`alias_is_current_name` is loose, and the gap between them is
    reachable: the alias table cannot hold two names differing only in case, but
    the *wiki's* name can drift out of case with its alias (a PATCH rename
    touches no alias row). Such an alias reads as current and promoting it still
    records an edit, which is right - recasing a place's name is a deliberate
    change to how it is displayed.

    The outgoing name is re-asserted as an alias first. ``Wiki.save()`` already
    ensures an alias row for whatever name is being set, and the outgoing name
    got its own row the same way when it was set - so in practice this is a
    no-op ``get_or_create``. It is here anyway because losing the previous name
    is unrecoverable (nothing else records it) and preventing that costs one
    indexed lookup. ``created_by`` is left unset: the outgoing name was not
    contributed by whoever is renaming away from it, and claiming otherwise
    would put a wrong name on the attribution.

    Args:
        wiki: The wiki to rename. Mutated and saved in place.
        profile: The profile performing the rename, recorded as the edit's
            editor.
        alias: The alias to promote. The caller is responsible for having
            looked it up scoped to *wiki*.

    Returns:
        The recorded :class:`~urbanlens.dashboard.models.wiki_edit.WikiEdit`, or
        None when *alias* was already the name and nothing changed.

    Raises:
        WikiEditValidationError: Never in practice - ``name`` has no
            strict-mode validation in ``apply_wiki_edit`` (only security levels
            and dates do) - but propagated rather than swallowed so a future
            rule added there is not silently ignored here.
    """
    outgoing = (wiki.name or "").strip()
    if is_meaningful_name(outgoing):
        try:
            # atomic() gives the IntegrityError its own savepoint: without it, a
            # concurrent writer winning this race would leave the surrounding
            # transaction unusable, and the caller could not even build its
            # response afterwards.
            with transaction.atomic():
                # Case-insensitive lookup matches the alias uniqueness rule, so
                # a differently-cased row already covering this name is reused
                # rather than racing the DB constraint.
                WikiAlias.objects.get_or_create(wiki=wiki, name__iexact=outgoing, defaults={"name": outgoing})
        except IntegrityError:
            # Another writer created it concurrently - which is the state we
            # wanted, so there is nothing left to do.
            pass

    # strict=True is the external API's setting and makes no difference for
    # ``name``; passing it unconditionally keeps both callers on one code path
    # rather than adding a flag that would only ever have one meaningful value.
    return apply_wiki_edit(wiki, profile, {"name": alias.name}, strict=True)
