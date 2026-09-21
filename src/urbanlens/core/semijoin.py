"""Bounding a filter that crosses a to-many relation to rows the caller can already see.

A predicate written through a to-many path - ``aliases__name__icontains`` - compiles to a join, and
Postgres is free to drive that join from whichever side its cost estimate likes. The far side is
the whole site's copy of that table, so a plan that starts there makes one account's query cost
proportional to every other account's data. That is a capacity defect rather than a latency one:
adding users makes everyone slower. See ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for
the measurements, and ``docs/PROBLEMS.md`` P132 for what it still costs at capacity scale.

The mechanism lives here rather than in the one service that first needed it because the shape is
not search-specific: any query bounded by "what this profile owns" and filtered through a relation
somebody else also has rows in has it. :class:`~urbanlens.dashboard.models.abstract.queryset.DashboardQuerySet`
exposes it as ``own_pks()`` and ``semijoin()``, so every model in the app has it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import TYPE_CHECKING, Any

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.models import Q
from django.db.models.fields.reverse_related import ForeignObjectRel

import urbanlens.core.lookups  # noqa: F401  - registers the __anyof every bound below uses

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.models import Model, QuerySet


def crosses_many(model: type[Model], path: str) -> bool:
    """Whether an ORM path from *model* passes through a relation that can match several rows.

    Args:
        model: The model the path starts from.
        path: A ``__``-separated field path, without a lookup.

    Returns:
        True when some step is a many-to-many or reverse foreign key."""
    for part in path.split("__"):
        try:
            field = model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            return False
        if field.many_to_many or field.one_to_many:
            return True
        related = field.related_model
        if not isinstance(related, type):
            return False
        model = related
    return False


@dataclass(frozen=True)
class Crossing:
    """Where an ORM path leaves the model it started from, and how to ask the far table directly."""

    #: The to-one steps before the crossing, ``""`` when it is the first step.
    prefix: str
    #: The table the probe drives from: the model the crossing reaches, or a many-to-many's own
    #: join table, which is the only side of one that is indexed by the caller's own rows.
    related_model: type[Model]
    #: The foreign key on :attr:`related_model` pointing back at the prefix's model.
    back_field: str
    #: What to remove from a condition's keys to restate it against :attr:`related_model`.
    strip: str
    #: What to put in its place; the join table's foreign key to the far model, or nothing.
    rewrite: str = ""


def _many_to_many_crossing(walked: list[str], part: str, field: Any) -> Crossing | None:
    """The crossing for a many-to-many step, driven from its join table.

    The far model is the wrong side to drive from - filtering every label on the site by name and
    joining back is the cost this module exists to avoid - but the join table is indexed by the
    caller's own rows, so bounding it there reads only theirs. Measured on the capacity population
    for a viewer with 1,859 pins: 27.46 ms joined from the pins, 16.21 ms from the join table.

    Args:
        walked: The to-one steps already taken.
        part: The many-to-many step's name.
        field: The ``ManyToManyField`` or ``ManyToManyRel`` at that step.

    Returns:
        The crossing, or None when the relation carries no resolvable join table."""
    declared = field if hasattr(field, "m2m_field_name") else getattr(field, "field", None)
    through = getattr(field.remote_field, "through", None) if hasattr(field, "remote_field") else getattr(field, "through", None)
    if declared is None or not isinstance(through, type):
        return None
    # The declaring side's "forward" is our source only when we are on that side of the relation.
    forward = declared is field
    source = declared.m2m_field_name() if forward else declared.m2m_reverse_field_name()
    target = declared.m2m_reverse_field_name() if forward else declared.m2m_field_name()
    return Crossing(prefix="__".join(walked), related_model=through, back_field=source, strip="__".join([*walked, part]) + "__", rewrite=f"{target}__")


def resolve_crossing(model: type[Model], path: str) -> Crossing | None:
    """Split *path* at the to-many step a probe should drive from, when it has one this shape.

    Args:
        model: The model the path starts from.
        path: A ``__``-separated field path, without a lookup.

    Returns:
        The crossing, or None when no step is to-many, or the path cannot be walked."""
    walked: list[str] = []
    for part in path.split("__"):
        try:
            field = model._meta.get_field(part)  # noqa: SLF001
        except FieldDoesNotExist:
            return None
        if field.many_to_many:
            return _many_to_many_crossing(walked, part, field)
        related = field.related_model
        if not isinstance(related, type):
            return None
        if field.one_to_many and isinstance(field, ForeignObjectRel):
            return Crossing(prefix="__".join(walked), related_model=related, back_field=field.field.name, strip="__".join([*walked, part]) + "__")
        walked.append(part)
        model = related
    return None


def restate(condition: Q, strip: str, rewrite: str = "") -> Q | None:
    """*condition*, rewritten to read against the table the probe drives from.

    Args:
        condition: A predicate whose every key runs through the crossing.
        strip: :attr:`Crossing.strip`, the path prefix to remove.
        rewrite: :attr:`Crossing.rewrite`, what to put in its place.

    Returns:
        The rewritten predicate, or None when some key does not go through the crossing - a
        condition mixing both sides has to stay a join."""
    children: list[Any] = []
    for child in condition.children:
        if isinstance(child, Q):
            inner = restate(child, strip, rewrite)
            if inner is None:
                return None
            children.append(inner)
            continue
        if not isinstance(child, tuple) or len(child) != 2:
            return None
        key, value = child
        if not isinstance(key, str) or not key.startswith(strip) or len(key) == len(strip):
            return None
        children.append((rewrite + key[len(strip) :], value))
    rewritten = Q()
    rewritten.connector = condition.connector
    rewritten.negated = condition.negated
    rewritten.children = children
    return rewritten


#: Per thread, because both the connection the scope sets and the querysets it memoises are.
_probe_state = threading.local()


class ProbeScope:
    """What every semi-join probe in one operation shares: the setting, and the caller's own pks."""

    def __init__(self) -> None:
        #: Keyed by id, holding the queryset too so the id cannot be reused while the entry lives.
        self._outer_pks: dict[int, tuple[object, list[Any]]] = {}
        #: Same trick, keyed additionally by the to-one prefix the anchors were read through.
        self._anchors: dict[tuple[type[Model], str, int], tuple[object, dict[Any, list[Any]]]] = {}

    def pks_of(self, queryset: QuerySet[Any, Any]) -> list[Any]:
        """The primary keys *queryset* selects, fetched once per scope.

        A caller probes once per term per field path against the same access-scoped queryset, and
        each probe needs the same list to bound itself by. Fetching it per probe was a dozen
        identical round trips for a list that cannot change inside one operation.

        Args:
            queryset: The access-scoped queryset being searched.

        Returns:
            Its primary keys, in whatever order the database returned them.
        """
        cached = self._outer_pks.get(id(queryset))
        if cached is not None:
            return cached[1]
        pks = list(queryset.values_list("pk", flat=True))
        self._outer_pks[id(queryset)] = (queryset, pks)
        return pks

    def anchors_of(self, model: type[Model], prefix: str, outer_pks: list[Any]) -> dict[Any, list[Any]]:
        """Which of *outer_pks* reach each row at the end of the to-one *prefix*, read once per scope.

        A probe that crosses further out than its own model - a pin matching through its location's
        wiki's aliases - has to bound the far table by wiki ids, not pin ids, and then map the
        answer back. Both halves come from this one mapping, which is the same for every term and
        every path sharing the prefix.

        Args:
            model: The model *outer_pks* belong to.
            prefix: The to-one path from it to the anchor, e.g. ``"location__wiki"``.
            outer_pks: The caller's own primary keys.

        Returns:
            Anchor primary key to the primary keys reaching it; rows reaching nothing are absent.
        """
        key = (model, prefix, id(outer_pks))
        cached = self._anchors.get(key)
        if cached is not None:
            return cached[1]
        mapping: dict[Any, list[Any]] = {}
        reaching = model._base_manager.filter(**{"pk__anyof": outer_pks, f"{prefix}__isnull": False})  # noqa: SLF001
        for pk, anchor in reaching.values_list("pk", f"{prefix}__pk"):
            mapping.setdefault(anchor, []).append(pk)
        self._anchors[key] = (outer_pks, mapping)
        return mapping


def current_scope() -> ProbeScope | None:
    """The probe scope this thread is inside, if any."""
    scope: ProbeScope | None = getattr(_probe_state, "scope", None)
    return scope


@contextmanager
def probe_scope() -> Iterator[ProbeScope]:
    """Hold ``enable_seqscan = off`` and one pk cache for the block, entering once however nested.

    An operation issues a semi-join probe per term per field path. Setting and resetting the
    session around each made two thirds of one search's statements session settings rather than
    queries, and re-fetching the caller's pks for each made a dozen more. Both are identical
    across one operation, so both belong to the scope rather than to the probe. Why the setting is
    wanted at all is :func:`probe_statement`'s business; this only decides how often it is applied.

    Yields:
        The scope, with the setting in force.
    """
    scope = current_scope()
    if scope is not None:
        yield scope
        return
    scope = _probe_state.scope = ProbeScope()
    with connection.cursor() as cursor:
        cursor.execute("SET enable_seqscan = off")
    try:
        yield scope
    finally:
        _probe_state.scope = None
        with connection.cursor() as cursor:
            cursor.execute("RESET enable_seqscan")


def probe_statement(model: type[Model], condition: Q, outer_pks: list[Any]) -> list[Any]:
    """Primary keys among *outer_pks* whose rows satisfy *condition*, as a join from *model*.

    The general form, for the paths :func:`probe_relation` cannot restate. It still joins, so it
    still lets the planner start from the far table; what it does not do is let the bound be
    anything but a literal list.

    Resolved as its own statement rather than left as a subquery for the caller to combine.
    Measured directly (docs/archive/PROBLEMS-ARCHIVE.md, formerly P123): the bound alone is not
    enough while it stays a nested subquery - inverting the query to drive from the related model
    still lets Postgres choose to scan that model's whole table once the crossing relation's own
    table is small (a correct choice at that size, but one that reintroduces growth as the
    population the caller cannot see grows), and even the *un-inverted* bounded subquery regresses
    the moment a caller OR-combines it with a sibling condition: Postgres then plans it as a hashed
    SubPlan and, inside that plan, drops the crossing table's own index in favour of scanning it
    whole - a plan it does not choose when the identical subquery runs alone.

    ``outer_pks`` has to be a literal list and not ``queryset.values("pk")``, even though the
    subquery form is far cheaper to send and to plan: 17,010 bytes and 6.97 ms of planning become
    361 bytes and 0.61 ms on the capacity population. Tried, and it fails the regression net P123
    left behind - ``test_search_does_not_read_another_accounts_*``, nine of them. Given a subquery
    the planner is free to drive from the far side of the join instead, filtering ``dashboard_labels``
    by name and joining back, which reads every label on the site: 545 rows where the literal list
    reads 5. The crossing table stays fine either way; it is the table the *condition* names that
    gets scanned. Cheaper to plan, and the wrong plan.

    That statement's own plan is a second problem, which is what the scope's ``enable_seqscan``
    answers: this is exactly the shape Postgres's cost-based planner tie-breaks towards a
    sequential scan of the crossing table once that table is small enough to fit in a handful of
    pages, regardless of how selective ``pk__in=outer_pks`` actually is - legitimate, documented
    behaviour for tiny tables, but one that reintroduces exactly the cost P123 is about for as long
    as that population stays in the small-table range. Measured directly: at ~400 unrelated rows
    Postgres chooses ``Seq Scan ... filter=(pin_id=1) removed=402`` over the available FK index; at
    20,000 rows the identical statement chooses ``Index Only Scan idx_cond=(pin_id=1) removed=0``.
    Since the statement is provably bounded (equality on a foreign key against a handful of
    already-known pks) and provably high-selectivity regardless of table size, forcing the planner
    away from that tie-break for this one statement is safe where it would not be generally:
    ``enable_seqscan=off`` only penalises sequential scans in cost estimation, it does not forbid
    them, so a table with no usable index still gets scanned, just without artificially preferring
    to when an index exists.

    Args:
        model: The model *condition* and *outer_pks* are both written against.
        condition: The predicate, crossing a to-many relation.
        outer_pks: The candidate primary keys to bound the match to.

    Returns:
        The subset of *outer_pks* that match.
    """
    matches = model._base_manager.filter(condition, pk__anyof=outer_pks)  # noqa: SLF001
    return list(matches.values_list("pk", flat=True))


def probe_relation(model: type[Model], path: str, condition: Q, outer_pks: list[Any]) -> list[Any] | None:
    """The same question as :func:`probe_statement`, asked of the crossing's own table.

    ``pk__in=outer_pks`` bounds which rows of *model* may answer, but it does not bound what the
    planner reads to decide: with the condition written through the relation, both tables are in
    one statement and Postgres is free to start from the one the condition names. It does, and that
    table is the whole site's. The bound then removes what it read - the plan is correct and its
    cost is another account's row count. Asking the far table directly, bounded by the same pks,
    removes the choice: the only entry point is its foreign key index.

    Measured on the capacity population (1,004 accounts, 502,058 pins), one viewer's 1,859 pins,
    term ``river``, planning plus execution:

    ============================================  ==========  ===============
    probe                                          joined      driven from it
    ============================================  ==========  ===============
    ``aliases__name`` (55,084 alias rows read)     64.99 ms    5.50 ms
    ``notes__text``                                 5.86 ms    2.25 ms
    ``location__wiki__aliases__name`` (51,001)     44.93 ms    0.11 ms
    ``labels__name`` (a many-to-many)              27.46 ms    16.21 ms
    ============================================  ==========  ===============

    Rows read and discarded go from 55,084 to 4, and from 51,001 to 4 - which is the point, since
    that count is what grows as accounts are added. The many-to-many is the exception that proves
    it: both plans already read only the viewer's own rows, so it is the one that gains a little
    rather than an order of magnitude. The ``enable_seqscan`` hint the scope holds costs 0.23 ms in
    this shape against roughly half the runtime in the old one, because the bounded probe reads the
    index either way.

    Args:
        model: The model *condition* and *outer_pks* are written against.
        condition: The predicate, crossing a to-many relation.
        path: The ORM path it crosses, without a lookup.
        outer_pks: The candidate primary keys to bound the match to.

    Returns:
        The subset of *outer_pks* that match, or None when the path or the condition is not of a
        shape this can restate - a path that never crosses, or a condition naming both sides of
        the crossing, which has to stay a join.
    """
    crossing = resolve_crossing(model, path)
    if crossing is None:
        return None
    restated = restate(condition, crossing.strip, crossing.rewrite)
    if restated is None:
        return None
    related = crossing.related_model
    bound = f"{crossing.back_field}__pk__anyof"
    if not crossing.prefix:
        matched = related._base_manager.filter(restated, **{bound: outer_pks})  # noqa: SLF001
        return list(dict.fromkeys(matched.values_list(crossing.back_field, flat=True)))

    scope = current_scope()
    anchors = (scope or ProbeScope()).anchors_of(model, crossing.prefix, outer_pks)
    if not anchors:
        return []
    matched = related._base_manager.filter(restated, **{bound: list(anchors)})  # noqa: SLF001
    reached = set(matched.values_list(crossing.back_field, flat=True))
    return [pk for anchor, pks in anchors.items() if anchor in reached for pk in pks]
