"""Rows a statement reads, as opposed to rows it returns.

A query counter, a row-count wrapper over ``cursor.rowcount`` and a response-size budget all measure what came
back. None of them can see a plan that reads a whole table to return one row, which is the shape a correlated
``Exists`` takes when its subquery carries no predicate of its own. That is only visible in the plan.

A test built on this reads a real, live plan - so it inherits whatever the planner's cost estimates say, and
those estimates are not scoped to the test's own transaction. ``ANALYZE`` run inside one test's rolled-back
transaction, and dead tuples/pages an earlier large insert-then-rollback left behind, can both outlive that
rollback and skew ``pg_class.reltuples``/``relpages`` for a later test in the same session against the same
table - observed directly: a matching-labels test asserting a small, stable row count passed in isolation 4/4
runs, then failed 2/2 times immediately after a *different* file's test inserted and rolled back 400+ rows into
the same table. Treat a failure of this kind of test as inconclusive, not a confirmed regression, until it
reproduces with the file run alone.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from django.db import connection

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def scanned(node: dict[str, Any]) -> int:
    """Rows *node* and everything under it actually read.

    Counts rows discarded by a filter as read, because they were: the cost being measured is the reading, not
    the answer. Only nodes naming a relation contribute, so an aggregate or a sort is not double-counted over
    the scan feeding it.

    Args:
        node: A node of an ``EXPLAIN (ANALYZE, FORMAT JSON)`` plan.

    Returns:
        Rows read by this subtree.
    """
    own = 0
    if "Relation Name" in node:
        own = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * node.get("Actual Loops", 1)
    return own + sum(scanned(child) for child in node.get("Plans", []))


def session_preamble(
    captured: Sequence[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
    index: int,
) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
    """The ``SET``/``RESET`` statements *captured* before position *index*.

    A statement's plan depends on session GUCs active when it ran, not just its SQL text - a
    scan node bracketed by ``SET enable_seqscan = off`` / ``RESET enable_seqscan`` (see
    ``_semijoin``) plans differently while that bracket is open than it does afterward. Passed as
    ``plan_of``'s *preamble*, this replays whatever GUC state was active for the statement at
    *index* when it really ran, rather than whatever the connection's default is by the time a
    test gets around to re-``EXPLAIN``-ing it.

    Args:
        captured: Every statement executed while a real search ran, in order.
        index: The position of the statement being explained.

    Returns:
        Its preceding ``SET``/``RESET`` statements, in the order they ran.
    """
    return [(sql, params) for sql, params in captured[:index] if sql.lstrip().upper().startswith(("SET ", "RESET"))]


def plan_of(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    preamble: Sequence[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = (),
) -> dict[str, Any]:
    """The executed plan of *sql*.

    Args:
        sql: A statement, with ``%s`` placeholders if it takes parameters.
        params: The parameters, as captured alongside the statement.
        preamble: ``SET``/``RESET`` statements to replay on the same cursor immediately before the
            ``EXPLAIN``, so a session GUC scoped around the real statement is active for this one
            too. See ``session_preamble``. Every touched GUC is ``RESET`` again afterward, so this
            call does not itself leak session state to whatever runs on this connection next.

    Returns:
        The root plan node.
    """
    touched = {pre_sql.split()[1].rstrip(";").lower() for pre_sql, _ in preamble}
    with connection.cursor() as cursor:
        for pre_sql, pre_params in preamble:
            cursor.execute(pre_sql, pre_params)
        try:
            cursor.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", params)  # noqa: S608 - a captured statement, not built from input
            row = cursor.fetchone()
        finally:
            for name in touched:
                cursor.execute(f"RESET {name}")
    raw = row[0] if row else "[]"
    plan = raw if isinstance(raw, list) else json.loads(raw)
    return dict(plan[0]["Plan"])


def rows_examined(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    preamble: Sequence[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = (),
) -> int:
    """How many rows Postgres reads to answer *sql*.

    Args:
        sql: A statement, with ``%s`` placeholders if it takes parameters.
        params: The parameters, as captured alongside the statement.
        preamble: See ``plan_of``.

    Returns:
        Rows read, including rows a filter then discarded.
    """
    return scanned(plan_of(sql, params, preamble=preamble))


def relations_read(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    preamble: Sequence[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = (),
) -> dict[str, int]:
    """Rows read per relation, for saying *which* table a statement walked.

    Args:
        sql: A statement, with ``%s`` placeholders if it takes parameters.
        params: The parameters, as captured alongside the statement.
        preamble: See ``plan_of``.

    Returns:
        Relation name to rows read, for every relation the plan touched.
    """
    totals: dict[str, int] = {}

    def walk(node: dict[str, Any]) -> None:
        name = node.get("Relation Name")
        if name:
            read = (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0)) * node.get("Actual Loops", 1)
            totals[name] = totals.get(name, 0) + read
        for child in node.get("Plans", []):
            walk(child)

    walk(plan_of(sql, params, preamble=preamble))
    return totals
