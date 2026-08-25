"""Field-granular revision history, and the interception that keeps it honest.

One row per ``(target, field, write)``. That granularity is what lets a viewer
be shown a *subset* of the history - automatic writes, plus their own, plus
their friends' - as a single indexed query rather than a replay:

    SELECT DISTINCT ON (field_name) field_name, value
    FROM   <model>_field_revisions
    WHERE  target_id = %s AND (source = 'automatic' OR actor_id = ANY(%s))
    ORDER  BY field_name, id DESC

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
from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.db.models import (
    CASCADE,
    SET_NULL,
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
    unversioned,
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
        (Ordering is by primary key. An explicit per-target sequence column was
        tried first and was a race: computing ``Max(sequence) + 1`` and then
        inserting is read-modify-write, so two concurrent writes to one target
        pick the same number and one loses to the unique constraint. The table's
        own auto-increment is already monotonic in insert order, needs no extra
        query, and orders correctly within a target once filtered to it.)
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

        # Snapshot the pks before the write - the update may change the very
        # fields this queryset filters on, so re-evaluating afterwards can match
        # a different set of rows - but do it under a lock, and inside the same
        # transaction as the write.
        #
        # Without that, a compare-and-set loses silently in the worst possible
        # direction. `enrich_wiki_location` renames a wiki with
        # `filter(pk=..., name=wiki.name).update(name=...)`, whose `name=` term
        # exists precisely so a concurrent user rename is not clobbered. If the
        # user's rename lands between the SELECT and the UPDATE, the UPDATE
        # matches nothing - and we would still write an AUTOMATIC revision for a
        # value that never reached the row. AUTOMATIC is the one source shown to
        # *every* viewer, so a phantom row would outrank the user's real rename
        # for everybody.
        with transaction.atomic(using=self.db):
            pks = list(self.select_for_update().values_list("pk", flat=True))
            if not pks:
                return 0
            updated = super().update(**kwargs)
            if updated:
                for pk in pks:
                    _record_fields(model, pk, touched)
        return updated

    def bulk_create(self, objs: Sequence[Any], *args: Any, **kwargs: Any) -> list[Any]:
        """Create in bulk, recording each new row's initial field state.

        A create is revision 1: the whole versioned field set is the state the
        row started in, with whatever source the context says made it. Without
        this, a bulk-created row has no provenance at all, and every one of its
        fields resolves to a default for a concealed viewer - which reads as an
        empty wiki rather than the one that was actually created.
        """
        created = super().bulk_create(objs, *args, **kwargs)

        versioned = getattr(self.model, "versioned_fields", ())
        if not versioned or is_unversioned():
            return created

        for obj in created:
            if obj.pk is not None:
                _record_fields(self.model, obj.pk, {name: getattr(obj, name, None) for name in versioned})
        return created

    def bulk_update(self, objs: Sequence[Any], fields: Sequence[str], batch_size: int | None = None) -> int:
        """Apply the bulk update, recording a revision per versioned field touched."""
        # Django implements bulk_update by ending with
        # `queryset.filter(pk__in=...).update(**case_expressions)` on a clone of
        # this very class, so `update()` above re-enters and would record a
        # `Case` expression's repr as the value. Suppress that inner pass and
        # record the real values here.
        with unversioned(reason="bulk_update records its own revisions"):
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

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any) -> Any:
        """Load, snapshotting the versioned fields so ``save()`` can diff them."""
        instance = super().from_db(db, field_names, values)
        instance._version_snapshot = {name: getattr(instance, name, None) for name in cls.versioned_fields}  # noqa: SLF001
        return instance

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, recording a revision only for versioned fields that changed.

        Recording *every* versioned field on an untargeted save would be worse
        than noisy - it would be wrong. A bare ``save()`` after editing one
        field would stamp the saver's name and source onto all the others, so a
        value a stranger contributed would be re-attributed to whoever saved
        next. A concealed viewer resolves by author, so that is a leak: a
        friend's ordinary save would hand them a stranger's contribution.
        """
        update_fields = kwargs.get("update_fields")
        snapshot = getattr(self, "_version_snapshot", None)

        if update_fields is not None:
            touched = [name for name in update_fields if name in self.versioned_fields]
        elif snapshot is None:
            # No snapshot means this instance was never loaded from the
            # database - a create. Its whole field set is the initial state.
            touched = list(self.versioned_fields)
        else:
            touched = [name for name in self.versioned_fields if getattr(self, name, None) != snapshot.get(name)]

        super().save(*args, **kwargs)

        if touched and not is_unversioned():
            _record_fields(type(self), self.pk, {name: getattr(self, name) for name in touched})

        # Re-snapshot, so a second save on the same instance does not re-record
        # what the first one already did.
        self._version_snapshot = {name: getattr(self, name, None) for name in self.versioned_fields}


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
    """Write one revision row per field written.

    Never raises in production. A provenance record that takes down the write
    it was describing would be worse than the gap it leaves - but in DEBUG and
    under test it re-raises, so a bypass is loud where somebody is looking.
    """
    if pk is None:
        return

    try:
        revision_model = _revision_model(model)
        if revision_model is None:
            return
        source = current_write_source()
        actor_id = current_write_actor() if source == WriteSource.USER else None

        rows = []
        for name, value in sorted(values.items()):
            rows.append(
                revision_model(
                    target_id=pk,
                    field_name=name,
                    value="" if value is None else _serialise(model, name, value),
                    is_null=value is None,
                    source=source,
                    actor_id=actor_id,
                )
            )
        # Its own savepoint. On PostgreSQL a failed INSERT aborts the whole
        # transaction at the server, so catching the Python exception is not
        # enough - the caller's next statement would fail with "current
        # transaction is aborted", and several callers (wiki_creation,
        # pin_wiki_sync, the external API's wiki edit) run inside one. The
        # codebase already uses nested atomic() for exactly this, in
        # wiki_aliases and pin_subresources.
        with transaction.atomic(using=revision_model.objects.db):
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
    try:
        field = model._meta.get_field(field_name)  # noqa: SLF001
    except FieldDoesNotExist:
        # `field_name` is historical text in an append-only table, so a field
        # that is later renamed or dropped leaves rows naming a column that no
        # longer exists. Without this, the first such rename turns every
        # resolve_fields call on an affected row into a 500 on the concealed
        # render path.
        return None
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
    up_to_revision: int | None = None,
) -> dict[str, Any]:
    """Return the latest qualifying value of every versioned field.

    The one query every filtered view of a versioned row goes through.

    Args:
        target: The row whose fields to resolve.
        sources: Write sources that qualify - normally just AUTOMATIC.
        actor_ids: Profiles whose writes also qualify - the viewer, and their
            friends, so that a friend saying "I put a load of stuff on the
            wiki" is not contradicted by the page.
        up_to_revision: Ignore revisions newer than this row id, for viewing
            an earlier state.

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
    if up_to_revision is not None:
        rows = rows.filter(pk__lte=up_to_revision)

    # DISTINCT ON needs the ordering to lead with the distinct column; the
    # database then hands back the newest qualifying row per field directly.
    rows = rows.order_by("field_name", "-pk").distinct("field_name")

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


def purge_recorded_value(target: Model, field_name: str, value: Any) -> int:
    """Delete revision rows on *target* that stored this exact value for a field.

    The revision log is append-only by design, and that is at odds with one
    existing user-facing promise: permanently deleting your own wiki edit
    ("intended for cases like accidentally pasting private information into a
    public wiki field") is supposed to leave *no copy anywhere*. Adding
    provenance recording quietly made that false, because the pasted string
    survives in a revision row along with the name of who wrote it.

    Matching on the value is deliberate and is narrower than it looks: the row
    being erased is the one whose stored value *is* the text the user is trying
    to unsay. Rows holding the pre-edit value are a different value and are left
    alone, which is right - that text was never the secret.

    Args:
        target: The row the revisions belong to.
        field_name: Which field.
        value: The stored value to erase, or None for a recorded NULL.

    Returns:
        How many rows were deleted.
    """
    revision_model = _revision_model(type(target))
    if revision_model is None:
        return 0

    rows = revision_model.objects.filter(target_id=target.pk, field_name=field_name)
    rows = rows.filter(is_null=True) if value is None else rows.filter(is_null=False, value=_serialise(type(target), field_name, value))
    deleted, _ = rows.delete()
    return deleted
