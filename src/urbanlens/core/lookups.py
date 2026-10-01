"""Set membership sent as one array rather than one parameter per element.

``field__in=[...]`` compiles to ``field IN (%s, %s, ... )``. With server-side binding on - this
project's setting - that is one bound parameter per element, and the cost lands before the planner
is reached at all. Bounding a query by a list the caller already holds is how this codebase keeps
one account's work off everybody else's tables (:mod:`urbanlens.core.semijoin`), so those lists are
long and there are several per request.

Measured on the capacity population, a 1,859-element bound, median round trip:

=====================================  ==========  ==================  ==============
form                                    planning    bound, no filter    bounded probe
=====================================  ==========  ==================  ==============
``IN (%s, %s, ...)``                     5.17 ms           14.71 ms         22.99 ms
``= ANY(%s)``                            2.87 ms            5.21 ms          3.99 ms
``= ANY('{...}'::bigint[])``             1.32 ms            2.91 ms          1.82 ms
=====================================  ==========  ==================  ==============

The middle row is why the array is written into the statement rather than bound to it. As a
parameter the array's contents are not available when the statement is planned, so Postgres falls
back to a default selectivity estimate - and on a small table that is enough to tip it into
scanning the table the *filter* names instead of the index the *bound* names. That is exactly the
plan ``test_search_does_not_read_another_accounts_*`` exists to catch, and nine of them caught it.
Written in, the values are a constant the planner can see, the same as ``IN``.

Only integers are written in, which is what a primary-key bound is here; anything else falls back
to ``IN`` rather than reaching a quoting decision. Importing this module registers ``__anyof`` on
every field, which happens through :mod:`urbanlens.core.semijoin`, imported by the base queryset,
so it is in place before any model can be queried.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import EmptyResultSet
from django.db.models import Field, Lookup

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.sql.compiler import SQLCompiler


@Field.register_lookup
class AnyOf(Lookup):
    """``field = ANY(%s)``: the same rows as ``__in``, at one parameter rather than N.

    Postgres only. Every other backend gets the ``IN`` form, which is what ``__in`` would have
    produced anyway, so a caller never has to ask which database it is on.
    """

    lookup_name = "anyof"

    def get_prep_lookup(self) -> list[Any]:
        """The right-hand side as a list, each element prepared by the field it is compared to."""
        values = list(self.rhs)
        prepare = getattr(self.lhs.output_field, "get_prep_value", None)
        if prepare is None:
            return values
        return [prepare(value) for value in values]

    def as_sql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, tuple[Any, ...]]:
        """The portable form, for backends without array parameters.

        Args:
            compiler: The SQL compiler.
            connection: The database wrapper.

        Returns:
            SQL and parameters.

        Raises:
            EmptyResultSet: The list is empty, so nothing can match."""
        if not self.rhs:
            raise EmptyResultSet
        lhs, lhs_params = self.process_lhs(compiler, connection)
        placeholders = ", ".join(["%s"] * len(self.rhs))
        return f"{lhs} IN ({placeholders})", (*lhs_params, *self.rhs)

    def as_postgresql(self, compiler: SQLCompiler, connection: BaseDatabaseWrapper) -> tuple[str, tuple[Any, ...]]:
        """The array form, with the values written into the statement so the planner can see them.

        Args:
            compiler: The SQL compiler.
            connection: The database wrapper.

        Returns:
            SQL and parameters.

        Raises:
            EmptyResultSet: The list is empty, so nothing can match - and an empty array literal
                has no element type for Postgres to compare against."""
        if not self.rhs:
            raise EmptyResultSet
        if not all(type(value) is int for value in self.rhs):
            return self.as_sql(compiler, connection)
        lhs, lhs_params = self.process_lhs(compiler, connection)
        values = ",".join(str(int(value)) for value in self.rhs)
        return f"{lhs} = ANY('{{{values}}}'::bigint[])", tuple(lhs_params)
