"""Field-granular revision history, and the interception that keeps it honest.

One row per ``(target, field, write)``. That granularity is what lets a viewer
be shown a *subset* of the history - automatic writes, plus their own, plus
their friends' - as a single indexed query rather than a replay:

    SELECT DISTINCT ON (field_name) field_name, value
    FROM   <model>_field_revisions
    WHERE  target_id = %s AND (source = 'automatic' OR actor_id = ANY(%s))
    ORDER  BY field_name, sequence DESC

The latest *qualifying* write per field. Correct about ordering for free: a
stranger's later edit is filtered out and a friend's earlier one is still the
newest qualifying row, while a subsequent enrichment write is newer than both
and wins.

**The live model row remains HEAD.** Ordinary reads touch none of this.

Writes are intercepted rather than funnelled. A funnel every caller must
remember to use is what decayed last time - three writers already bypass the
existing edit history, one of them a bulk ``update()`` that misses ``save()``
and signals alike. Django offers no hook there at all, so the queryset is the
only place to stand.

See ``docs/designs/versioned-content.md``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from django.conf import settings as django_settings
from django.db.models import (
    CASCADE,
    SET_NULL,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    Field,
    ForeignKey,
    Index,
    Manager,
    Model,
    QuerySet,
    TextField,
    UniqueConstraint,
)

from urbanlens.dashboard.models.abstract.versioning import (
    WriteSource,
    current_write_actor,
    current_write_source,
    is_unversioned,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

class AbstractFieldRevision(Model):
    """One recorded write of one field.

    Concrete subclasses add ``target`` as a real ForeignKey to their own model -
    per-model tables rather than one contenttypes table, so each gets a typed
    FK, its own indexes and its own retention policy.

    Attributes:
        field_name: Which field was written.
        value: The value after the write, serialised to text. Text rather than
            JSON: a JSON column fights searching, indexing and encryption, and
            nothing here needs to query inside the value.
        is_null: Whether the write set the field to NULL. A separate flag
            because an empty string is a legitimate value for most CharFields
            here, so conflating the two would resurrect a concealed value as
            "" rather than None.
        sequence: Monotonic per target. Orders writes without depending on
            timestamps, which collide under load and go backwards under clock
            adjustment.
        source: Whether a person, an automatic process, or the system wrote it.
        actor: The profile responsible, when there is one.
        recorded_at: Wall-clock time, for display only - never for ordering.
    """

    #: Declared on the abstract base so the resolver's `.objects` is typed.
    #: Django gives concrete subclasses a default manager either way; naming it
    #: here is what lets a helper return `type[AbstractFieldRevision]` and use
    #: it, instead of returning `type[Model]` and being unable to query.
    objects = Manager()

    field_name = CharField(max_length=64, db_index=True)
    value = TextField(blank=True, default="")
    is_null = BooleanField(default=False)
    sequence = BigIntegerField()
    source = CharField(max_length=16, choices=WriteSource.choices, db_index=True)
    actor = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")
    recorded_at = DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        app_label = "dashboard"


class VersionedQuerySet(QuerySet):
    """A QuerySet that records revisions for bulk writes.

    ``update()`` is the reason this class exists. It bypasses ``save()`` *and*
    every signal, so a model-level hook cannot see it - which is exactly how
    the three existing bypasses became invisible. Overriding it here is the
    only interception point Django offers.
    """

    def update(self, **kwargs: Any) -> int:
        """Apply the update, recording a revision per versioned field touched."""
        model = self.model
        versioned = getattr(model, "versioned_fields", ())
        touched = {name: value for name, value in kwargs.items() if name in versioned}

        if not touched or is_unversioned():
            return super().update(**kwargs)

        # Snapshot the pks before the write: the update may change the very
        # fields this queryset filters on, so re-evaluating afterwards can
        # match a different set of rows.
        pks = list(self.values_list("pk", flat=True))
        updated = super().update(**kwargs)
        for pk in pks:
            _record_fields(model, pk, touched)
        return updated

    def bulk_update(self, objs: Sequence[Any], fields: Sequence[str], batch_size: int | None = None) -> int:
        """Apply the bulk update, recording a revision per versioned field touched."""
        result = super().bulk_update(objs, fields, batch_size=batch_size)
        versioned = getattr(self.model, "versioned_fields", ())
        touched_fields = [name for name in fields if name in versioned]
        if not touched_fields or is_unversioned():
            return result
        for obj in objs:
            _record_fields(self.model, obj.pk, {name: getattr(obj, name) for name in touched_fields})
        return result


class VersionedModel(Model):
    """Mixin for a model whose scalar fields carry provenance.

    Declare ``versioned_fields`` and ``revision_model`` on the concrete model.
    Every write to a listed field records who made it, so that a filtered view
    of the field set can be assembled later for a viewer entitled to only part
    of it.
    """

    #: Field names whose writes are recorded. Declared, never inferred - a
    #: model should not start versioning a new column by accident.
    versioned_fields: tuple[str, ...] = ()

    class Meta:
        abstract = True
        app_label = "dashboard"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, recording a revision for each versioned field written."""
        update_fields = kwargs.get("update_fields")
        if update_fields is None:
            touched = list(self.versioned_fields)
        else:
            touched = [name for name in update_fields if name in self.versioned_fields]

        super().save(*args, **kwargs)

        if touched and not is_unversioned():
            _record_fields(type(self), self.pk, {name: getattr(self, name) for name in touched})


def _revision_model(model: type[Model]) -> type[AbstractFieldRevision] | None:
    """Resolve a model's revision table, which is declared as a label string.

    A string rather than a class so the model module does not have to import
    its own revision module at definition time, which would be circular - the
    revision model has a ForeignKey back.
    """
    from django.apps import apps

    declared = getattr(model, "revision_model", None)
    if declared is None:
        return None
    resolved = apps.get_model(declared) if isinstance(declared, str) else declared
    if not issubclass(resolved, AbstractFieldRevision):
        logger.error("%s.revision_model is not an AbstractFieldRevision", model.__name__)
        return None
    return resolved


def _record_fields(model: type[Model], pk: Any, values: dict[str, Any]) -> None:
    """Write one revision row per field, at the next sequence for this target.

    Never raises in production. A provenance record that takes down the write
    it was describing would be worse than the gap it leaves - but in DEBUG and
    under test it re-raises, so a bypass is loud where somebody is looking.
    """
    from django.db.models import Max

    revision_model = _revision_model(model)
    if revision_model is None or pk is None:
        return

    try:
        highest = revision_model.objects.filter(target_id=pk).aggregate(top=Max("sequence"))["top"] or 0
        source = current_write_source()
        actor_id = current_write_actor() if source == WriteSource.USER else None

        rows = []
        for offset, (name, value) in enumerate(sorted(values.items()), start=1):
            rows.append(
                revision_model(
                    target_id=pk,
                    field_name=name,
                    value="" if value is None else _serialise(model, name, value),
                    is_null=value is None,
                    sequence=highest + offset,
                    source=source,
                    actor_id=actor_id,
                )
            )
        revision_model.objects.bulk_create(rows)
    except Exception:
        logger.exception("Could not record field revisions for %s pk=%s", model.__name__, pk)
        if django_settings.DEBUG or getattr(django_settings, "TESTING", False):
            raise


def concrete_field(model: type[Model], field_name: str) -> Field | None:
    """Return a versioned field's definition, or None if it is not a real column.

    ``_meta.get_field`` also returns reverse relations, which have no
    ``to_python`` and cannot be versioned. Narrowing here keeps the two call
    sites from each having to know that.
    """
    field = model._meta.get_field(field_name)  # noqa: SLF001
    return field if isinstance(field, Field) else None


def _serialise(model: type[Model], field_name: str, value: Any) -> str:
    """Render a field value as text for storage.

    Foreign keys store their pk. Everything else goes through ``str``; reading
    goes back through the field's own ``to_python``, which is what stops this
    inheriting the existing revert path's bug of assigning a stringified value
    straight back onto a typed field.
    """
    field = concrete_field(model, field_name)
    if field is not None and field.is_relation:
        return str(getattr(value, "pk", value))
    return str(value)


def resolve_fields(
    target: Model,
    *,
    sources: Iterable[str] = (WriteSource.AUTOMATIC,),
    actor_ids: Iterable[int] = (),
    up_to_sequence: int | None = None,
) -> dict[str, Any]:
    """Return the latest qualifying value of every versioned field.

    The one query every filtered view of a versioned row goes through.

    Args:
        target: The row whose fields to resolve.
        sources: Write sources that qualify - normally just AUTOMATIC.
        actor_ids: Profiles whose writes also qualify - the viewer, and their
            friends, so that a friend saying "I put a load of stuff on the
            wiki" is not contradicted by the page.
        up_to_sequence: Ignore anything newer, for viewing an earlier state.

    Returns:
        ``{field_name: value}`` for every versioned field that has a
        qualifying write. A field with none is absent, which the caller should
        read as "never set by anyone this viewer can see".
    """
    from django.db.models import Q

    model = type(target)
    revision_model = _revision_model(model)
    if revision_model is None:
        return {}

    predicate = Q(source__in=list(sources))
    actor_ids = list(actor_ids)
    if actor_ids:
        predicate |= Q(actor_id__in=actor_ids)

    rows = revision_model.objects.filter(target_id=target.pk).filter(predicate)
    if up_to_sequence is not None:
        rows = rows.filter(sequence__lte=up_to_sequence)

    # DISTINCT ON needs the ordering to lead with the distinct column; the
    # database then hands back the newest qualifying row per field directly.
    rows = rows.order_by("field_name", "-sequence").distinct("field_name")

    resolved: dict[str, Any] = {}
    for row in rows:
        if row.is_null:
            resolved[row.field_name] = None
            continue
        field = concrete_field(model, row.field_name)
        if field is None:
            continue
        # A relation stores its pk; everything else goes back through the
        # field's own to_python, which is what stops this inheriting the
        # existing revert path's bug of assigning a stringified value onto a
        # typed field.
        resolved[row.field_name] = int(row.value) if field.is_relation else field.to_python(row.value)
    return resolved
