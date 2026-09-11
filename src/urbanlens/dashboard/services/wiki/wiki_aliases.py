"""Promoting one of a wiki's alternate names to be its community name.

The wiki counterpart of ``services.pins.pin_subresources.promote_alias_to_name``.
a wiki rename is an *audited community edit*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, normalize_name_for_comparison
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit


def alias_is_current_name(alias: WikiAlias, wiki: Wiki | None) -> bool:
    """Whether *alias* is the name the wiki currently goes by.
    The comparison is loose - :func:`normalize_name_for_comparison` folds case, spacing and punctuation - because that is the same rule the alias uniqueness constraint and the "you can't delete the current name" guard use.

    Args:
        alias: The alias under consideration.
        wiki: The wiki it belongs to, or None when the caller has no wiki in
            hand (in which case nothing can be the current name).

    Returns:
        True when *alias* names the wiki as it currently stands."""
    if wiki is None:
        return False
    current = normalize_name_for_comparison(wiki.name)
    return bool(current) and normalize_name_for_comparison(alias.name) == current


def promote_wiki_alias_to_name(wiki: Wiki, profile: Profile, alias: WikiAlias) -> WikiEdit | None:
    """Make *alias* the wiki's community name, recording it in the edit history.
    Promoting the alias that is *already* the name is a no-op rather than an error: "use this name" is an idempotent statement of intent, and a client that retries a request whose response it never saw must not be punished for it.

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
        WikiEditValidationError: Never in practice - ``apply_wiki_edit``
            validates only security levels, dates and description length, and
            ``name`` is none of them - but propagated rather than swallowed so a
            future rule added there is not silently ignored here."""
    outgoing = (wiki.name or "").strip()
    if is_meaningful_name(outgoing):
        try:
            # atomic() gives the IntegrityError its own savepoint: without it, a concurrent writer
            # winning this race would leave the surrounding transaction unusable, and the caller
            # could not even build its response afterwards.
            with transaction.atomic():
                # Case-insensitive lookup matches the alias uniqueness rule, so
                # a differently-cased row already covering this name is reused
                # rather than racing the DB constraint.
                WikiAlias.objects.get_or_create(wiki=wiki, name__iexact=outgoing, defaults={"name": outgoing})
        except IntegrityError:
            # Another writer created it concurrently - which is the state we
            # wanted, so there is nothing left to do.
            pass

    return apply_wiki_edit(wiki, profile, {"name": alias.name})
