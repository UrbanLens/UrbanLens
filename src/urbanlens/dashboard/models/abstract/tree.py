"""Subtree and ancestor expansion for models that nest under themselves through one foreign key."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from django.db import connection
from django.db.models import ForeignKey, Model, QuerySet
from django.db.models.expressions import RawSQL

if TYPE_CHECKING:
    from collections.abc import Sequence


class TreeQuerySetMixin(QuerySet[Any, Any]):
    """``with_descendants`` / ``with_ancestors`` as one recursive query each, whatever the depth.

    The expansion is returned as ``pk IN (WITH RECURSIVE ...)`` over a fresh queryset, so callers keep
    composing it and nothing is evaluated until they do. ``UNION`` rather than ``UNION ALL`` is what
    makes a corrupted or transient cycle terminate (``pin_edit`` briefly self-parents a pin while
    promoting children).
    """

    #: Name of the self-referencing foreign key, e.g. ``"parent_pin"``.
    tree_parent_field: ClassVar[str]

    def with_descendants(self) -> Self:
        """This queryset's rows plus every row nested below them.

        Returns:
            A fresh queryset over the rows and their full subtrees.
        """
        return self._tree_closure("down")

    def with_ancestors(self) -> Self:
        """This queryset's rows plus every row above them, up to the roots.

        Returns:
            A fresh queryset over the rows and their full ancestor chains.
        """
        return self._tree_closure("up")

    def ancestors_of(self, node: Model, *, select_related: Sequence[str] = ()) -> list[Any]:
        """*node*'s ancestors, nearest parent first, read in one query.

        Args:
            node: The row whose chain to read.
            select_related: Relations to fetch with each ancestor.

        Returns:
            The ancestors in order, stopping short of any cycle; empty for a root.
        """
        attname = self._parent_field().attname
        parent_id = getattr(node, attname)
        if parent_id is None:
            return []
        by_pk = {row.pk: row for row in self.filter(pk=parent_id).with_ancestors().select_related(*select_related)}
        chain: list[Any] = []
        seen = {node.pk}
        while parent_id is not None and parent_id not in seen and parent_id in by_pk:
            seen.add(parent_id)
            chain.append(by_pk[parent_id])
            parent_id = getattr(by_pk[parent_id], attname)
        return chain

    def lineage_ids(self, node: Model) -> set[object]:
        """*node*'s primary key and its ancestors': the rows that may not be nested beneath it.

        Args:
            node: The prospective parent.

        Returns:
            The key set; reading it costs no query when *node* is a root.
        """
        if getattr(node, self._parent_field().attname) is None:
            return {node.pk}
        return set(self.filter(pk=node.pk).with_ancestors().values_list("pk", flat=True))

    def would_close_cycle(self, node: Model, new_parent: Model | None) -> bool:
        """Whether making *new_parent* the parent of *node* would make *node* its own ancestor.

        Args:
            node: The row being reparented.
            new_parent: Its proposed parent, or None.

        Returns:
            True when *new_parent* is *node* or sits below it.
        """
        if new_parent is None or node.pk is None or new_parent.pk is None:
            return False
        if new_parent.pk == node.pk:
            return True
        if getattr(new_parent, self._parent_field().attname) is None:
            return False
        return self.filter(pk=new_parent.pk).with_ancestors().filter(pk=node.pk).exists()

    def _parent_field(self) -> ForeignKey:
        field = self.model._meta.get_field(self.tree_parent_field)  # noqa: SLF001 - fields are only exposed through _meta
        if not isinstance(field, ForeignKey):
            raise TypeError(f"{self.model.__name__}.{self.tree_parent_field} is not a foreign key.")
        return field

    def _tree_closure(self, direction: Literal["down", "up"]) -> Self:
        quote = connection.ops.quote_name
        table = quote(self.model._meta.db_table)  # noqa: SLF001 - the table name is only exposed through _meta
        pk_column = self._parent_field().target_field.column
        if pk_column is None:
            raise TypeError(f"{self.model.__name__}.{self.tree_parent_field} targets no column.")
        pk = quote(pk_column)
        parent = quote(self._parent_field().column)
        seed = self.values("pk") if self.query.is_sliced else self.order_by().values("pk")
        seed_sql, seed_params = seed.query.sql_with_params()
        if direction == "down":
            step = f"SELECT row.{pk} FROM {table} row JOIN tree ON row.{parent} = tree.id"  # noqa: S608
        else:
            step = f"SELECT row.{parent} FROM {table} row JOIN tree ON row.{pk} = tree.id WHERE row.{parent} IS NOT NULL"  # noqa: S608
        # Identifiers come from model metadata, quoted by the backend; the seed is Django-compiled SQL with its params bound.
        sql = f"WITH RECURSIVE tree(id) AS (SELECT * FROM ({seed_sql}) seed UNION {step}) SELECT id FROM tree"  # noqa: S608
        return type(self)(model=self.model, using=self.db).filter(pk__in=RawSQL(sql, seed_params))  # noqa: S611
