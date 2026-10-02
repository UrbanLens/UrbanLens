"""Adding a community name to a wiki, and promoting one of its alternate names to be its name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.core.text_limits import column_length_error
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, normalize_name_for_comparison, sanitize_name
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


class WikiAliasError(ValueError):
    """An alias was refused; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class WikiAliasNameError(WikiAliasError):
    """The name is empty once sanitized, or too long for its column."""


class WikiAliasExistsError(WikiAliasError):
    """The wiki already has this name, case-insensitively."""

    def __init__(self) -> None:
        super().__init__("That alias already exists.")


def create_wiki_alias(wiki: Wiki, profile: Profile, *, name: str, kind: str = AliasType.ALTERNATE) -> WikiAlias:
    """Add a community name to *wiki* and record it in the wiki's history and the editor's undo stack.

    Args:
        wiki: The wiki to name; the real row, never a concealed projection.
        profile: The contributor, recorded as the alias's ``created_by`` and the edit's editor.
        name: The submitted name, sanitized here the way ``WikiAlias.save()`` would.
        kind: An :class:`AliasType` value.

    Returns:
        The created alias.

    Raises:
        WikiAliasNameError: Nothing of *name* survives sanitizing, or it is too long.
        WikiAliasExistsError: The wiki already has this name, case-insensitively.
    """
    from urbanlens.dashboard.services.undo.mutations import stash_wiki_alias_add

    cleaned = sanitize_name((name or "").strip()) or ""
    if not cleaned:
        raise WikiAliasNameError("Name is required.")
    if length_error := column_length_error(WikiAlias, "name", cleaned, "Alias"):
        raise WikiAliasNameError(length_error)
    try:
        with transaction.atomic():
            alias = WikiAlias.objects.create(wiki=wiki, name=cleaned, kind=kind, created_by=profile)
    except IntegrityError as exc:
        raise WikiAliasExistsError from exc
    stash_wiki_alias_add(wiki, profile, alias)
    WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"alias_added": {"from": None, "to": cleaned}})
    return alias


def alias_is_current_name(alias: WikiAlias, wiki: Wiki | None) -> bool:
    """Whether *alias* is the name the wiki currently goes by.
    The comparison is loose - :func:`normalize_name_for_comparison` folds case, spacing and punctuation - because that is the same rule the alias uniqueness constraint and the "you can't delete the current name" guard use.

    Args:
        alias: The alias under consideration.
        wiki: The wiki it belongs to, or None when the caller has no wiki in hand (in which case nothing can be the current name).

    Returns:
        True when *alias* names the wiki as it currently stands."""
    if wiki is None:
        return False
    current = normalize_name_for_comparison(wiki.name)
    return bool(current) and normalize_name_for_comparison(alias.name) == current


def promote_wiki_alias_to_name(wiki: Wiki, profile: Profile, alias: WikiAlias) -> WikiEdit | None:
    """Promoting the alias that is *already* the name is a no-op rather than an error: "use this name" is an idempotent statement of intent, and a client that retries a request whose response it never saw must not be punished for it.

    Args:
        wiki: The wiki to rename.
        profile: The profile performing the rename, recorded as the edit's editor.
        alias: The alias to promote.

    Returns:
        The recorded :class:`~urbanlens.dashboard.models.wiki_edit.WikiEdit`, or None when *alias* was already the name and nothing changed.

    Raises:
        WikiEditValidationError: The alias's name is empty only once sanitized - stored aliases are sanitized on save,
            so only a row written before that rule can be."""
    outgoing = (wiki.name or "").strip()
    if is_meaningful_name(outgoing):
        WikiAlias.objects.resolve_or_create(wiki, outgoing)

    return apply_wiki_edit(wiki, profile, {"name": alias.name})
