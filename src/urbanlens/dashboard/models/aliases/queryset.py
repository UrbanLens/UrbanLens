"""QuerySets and managers for PinAlias and WikiAlias."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.db.models import Model

    from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias, _AliasBase  # noqa: F401 - mypy needs these; ruff does not

_AliasT = TypeVar("_AliasT", bound="_AliasBase")


class AliasQuerySet(abstract.DashboardQuerySet[_AliasT]):
    """Alias lookups shared by pin and wiki aliases, which differ only in the owner they hang off."""

    #: The foreign key naming the alias's owner.
    owner_field: ClassVar[str] = ""

    def resolve_or_create(self, owner: Model | int, name: str, *, defaults: dict[str, Any] | None = None) -> tuple[_AliasT | None, bool]:
        """Return the owner's alias for *name*, creating it when there is none.

        The name is sanitized the way ``save()`` will store it before the lookup, and matched case-insensitively
        like the ``lower(name)`` constraint, so an alias that differs only by case, whitespace or a dropped symbol
        is found rather than colliding on insert.

        Args:
            owner: The pin or wiki, or its primary key.
            name: The alias text, as supplied.
            defaults: Field values applied only to a created alias.

        Returns:
            ``(alias, created)``; ``(None, False)`` when *name* sanitizes to nothing.

        Raises:
            IntegrityError: The insert failed for a reason other than a concurrent insert of the same name.
        """
        from urbanlens.dashboard.services.locations.naming import sanitize_name

        clean = sanitize_name(name) or ""
        if not clean:
            return None, False
        owner_key = {f"{self.owner_field}_id": owner} if isinstance(owner, int) else {self.owner_field: owner}
        if (existing := self.filter(**owner_key, name__iexact=clean).first()) is not None:
            return existing, False
        try:
            with transaction.atomic():
                return self.create(**owner_key, name=clean, **(defaults or {})), True
        except IntegrityError:
            if (existing := self.filter(**owner_key, name__iexact=clean).first()) is None:
                raise
            return existing, False


class PinAliasQuerySet(AliasQuerySet["PinAlias"]):
    owner_field = "pin"


class WikiAliasQuerySet(AliasQuerySet["WikiAlias"]):
    owner_field = "wiki"


class PinAliasManager(abstract.DashboardManager.from_queryset(PinAliasQuerySet)):
    pass


class WikiAliasManager(abstract.DashboardManager.from_queryset(WikiAliasQuerySet)):
    pass
