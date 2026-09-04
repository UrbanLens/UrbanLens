"""Fail the build when a bulk write silently skips a model's ``post_save`` receivers.

``bulk_create``/``bulk_update`` issue raw SQL and never call ``save()``, so no
``post_save``/``post_delete`` fires. When a model has receivers that maintain derived
state - a cache, a denormalised counter, a queued sync - a bulk write leaves that state
stale with no error and no log line. Four such sites were found by hand in 8ed25a93; the
map pin cache had been serving stale icons after every label reorder.

Whether a bulk write is dangerous is a property of the *model*, not of the call site, so
a site that is safe today becomes a bug the moment somebody adds a receiver to the model
it writes. Nothing about that change would look wrong in review. This test is the thing
that notices.

Adding a bulk write on a model with receivers is not forbidden - it is often right. It
just has to be a decision somebody made on purpose, recorded in ``REVIEWED`` below.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.apps import apps
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from urbanlens.core.tests.testcase import SimpleTestCase

#: The ``urbanlens`` package, derived from this file rather than the cwd so the test does
#: not depend on where pytest was invoked from (or on the checkout being at any
#: particular path - it differs between the host and the app container).
PACKAGE_ROOT = Path(__file__).resolve().parents[3]

#: Bulk writes on models that do have receivers, reviewed and deliberately kept.
#: Keyed by (path relative to src/urbanlens, model, operation) - not by line number,
#: which would churn on every edit above the call and train people to update it blindly.
REVIEWED: dict[tuple[str, str, str], str] = {
    (
        "dashboard/controllers/organize.py",
        "Label",
        "bulk_update",
    ): "Reordering labels; calls refresh_map_pin_cache_for_label_ids() immediately after, because order decides which label supplies a pin's icon.",
    (
        "dashboard/external_api/views_labels_bulk.py",
        "Label",
        "bulk_update",
    ): "The API's reorder and bulk-edit endpoints; both call refresh_map_pin_cache_for_label_ids() immediately after.",
    (
        "dashboard/services/sharing/pin_sharing.py",
        "Image",
        "bulk_create",
    ): (
        "Copying a shared pin's photos to the recipient. Image has an achievements post_save "
        "(connected dynamically via _SUBSCRIPTIONS, not a @receiver decorator) that records a "
        "photo-upload streak day. Not firing it is correct here: the recipient received these "
        "photos, they did not take them, and crediting an upload streak for accepting a share "
        "would be wrong. The recipient's photo-count metric is consequently not invalidated at "
        'copy time; it self-heals on their next photo action. See "A guard for the '
        'bulk-write class, which immediately found a fifth site" in docs/archive/PROBLEMS-ARCHIVE.md.'
    ),
    (
        "dashboard/services/undo/handlers/markup_map.py",
        "PinMarkup",
        "bulk_create",
    ): (
        "Undo restore recreating a map's annotations. The skipped per-item signals all do one "
        "thing - defer a pin-inference resync of the parent map - and the handler calls "
        "defer_pin_inference_sync(map.pk) itself right after the bulk_create, which also fixes "
        "an ordering hazard: the map's own created-save defers its resync before the items "
        "exist under autocommit."
    ),
    (
        "dashboard/controllers/labels.py",
        "Label",
        "bulk_update",
    ): "The Display Order tab's drag-and-drop reorder; calls refresh_map_pin_cache_for_label_ids() immediately after, and only for labels whose order actually moved - the refresh costs work per pin carrying the label.",
    (
        "dashboard/services/trips/trip_activities.py",
        "TripActivity",
        "bulk_update",
    ): "Reordering a trip's activities; calls queue_calendar_push(trip.pk) once afterwards, matching pin_list_trip.py below - the receiver fires per activity but the push sends the whole trip.",
    (
        "dashboard/services/pins/pin_list_trip.py",
        "TripActivity",
        "bulk_create",
    ): "Copying a pin list into a trip; calls queue_calendar_push(trip.pk) once afterwards rather than once per activity.",
}

#: Signals whose receivers a bulk write bypasses.
WATCHED_SIGNALS = (
    ("post_save", post_save),
    ("pre_save", pre_save),
    ("post_delete", post_delete),
    ("pre_delete", pre_delete),
)


def _bulk_write_sites() -> list[tuple[str, int, str, str]]:
    """Every ``bulk_create``/``bulk_update`` call in application code.

    Migrations are excluded: they run against historical schema, deliberately do not
    fire current receivers, and rewriting them is not an option anyway. Tests are
    excluded because a test setting up fixtures in bulk is not a production code path.

    Returns:
        Tuples of (relative path, line number, model name, operation).
    """
    sites: list[tuple[str, int, str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if "/tests/" in f"/{relative}" or "/migrations/" in f"/{relative}":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - unparseable file is its own failure
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"bulk_create", "bulk_update"}:
                continue
            model = _receiver_root(node.func.value)
            if model:
                sites.append((relative, node.lineno, model, node.func.attr))
    return sites


def _receiver_root(node: ast.expr) -> str | None:
    """The leftmost name of an attribute chain, when it looks like a model.

    ``Label.objects.bulk_update(...)`` and ``Label.bulk_update(...)`` both give
    ``"Label"``. Anything not starting with a capitalised bare name - a local queryset
    variable, or a chain rooted in a call such as ``super()`` - returns None, and the
    call site is simply excluded from the scan; there is no separate report for it.
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name) and node.id[:1].isupper():
        return node.id
    return None


def _receivers_by_model_name() -> dict[str, list[str]]:
    """Map each model's class name to the signals it has live receivers for.

    Read from Django's own signal registry rather than by grepping for ``@receiver``,
    so receivers connected any other way are counted too.
    """
    found: dict[str, list[str]] = {}
    for model in apps.get_models():
        connected = [name for name, signal in WATCHED_SIGNALS if signal.has_listeners(model)]
        if connected:
            found.setdefault(model.__name__, []).extend(connected)
    return found


class BulkWriteSignalGuardTests(SimpleTestCase):
    """Bulk writes on models with receivers must be reviewed, not accidental."""

    def test_every_bulk_write_on_a_model_with_receivers_is_reviewed(self):
        receivers = _receivers_by_model_name()
        unreviewed: list[str] = []

        for relative, line, model, operation in _bulk_write_sites():
            connected = receivers.get(model)
            if not connected:
                continue
            if (relative, model, operation) in REVIEWED:
                continue
            unreviewed.append(
                f"  {relative}:{line}\n      {model}.{operation}() skips these signals: {', '.join(sorted(set(connected)))}"
            )

        self.assertEqual(
            unreviewed,
            [],
            "Bulk write on a model whose receivers maintain derived state:\n\n"
            + "\n".join(unreviewed)
            + "\n\nbulk_create/bulk_update never fire these signals, so whatever they maintain "
            "(a cache, a counter, a queued sync) will silently go stale.\n"
            "Either do that work explicitly right after the bulk call, or - if the receivers "
            "genuinely do not matter here - add an entry to REVIEWED in this file saying why.",
        )

    def test_the_reviewed_list_has_no_stale_entries(self):
        """A reviewed entry that no longer matches real code is worse than none.

        It reads as coverage while guarding nothing, and hides the next real instance
        if the call site comes back.
        """
        actual = {(relative, model, operation) for relative, _line, model, operation in _bulk_write_sites()}
        stale = sorted(key for key in REVIEWED if key not in actual)

        self.assertEqual(stale, [], f"REVIEWED names call sites that no longer exist; delete them: {stale}")

    def test_the_scan_still_finds_call_sites(self):
        """Guards the guard.

        If the AST walk or the path handling ever breaks, every other assertion here
        passes vacuously - a green test proving nothing. This fails instead.
        """
        self.assertGreater(len(_bulk_write_sites()), 5)

    def test_the_signal_registry_lookup_returns_something(self):
        """Also guards the guard: no receivers found means the app registry is not
        loaded, and every bulk site would be waved through."""
        self.assertNotEqual(_receivers_by_model_name(), {})

    def test_receiver_root_extracts_the_model_from_a_manager_chain(self):
        """The one thing this whole scan hinges on: telling ``Model.objects.bulk_x()``
        apart from a local variable's ``.bulk_x()``. This discriminator is only
        exercised indirectly by the whole-codebase tests above - a broken uppercase
        check wouldn't reliably fail them, since a fabricated non-model "name" almost
        never collides with a real model's ``__name__`` in the receiver registry."""
        call = ast.parse("Label.objects.bulk_update(rows)").body[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        self.assertEqual(_receiver_root(call.func.value), "Label")

    def test_receiver_root_extracts_the_model_from_a_bare_classmethod_call(self):
        call = ast.parse("Label.bulk_update(rows)").body[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        self.assertEqual(_receiver_root(call.func.value), "Label")

    def test_receiver_root_returns_none_for_a_lowercase_local_variable(self):
        call = ast.parse("queryset.bulk_update(rows)").body[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        self.assertIsNone(_receiver_root(call.func.value))

    def test_receiver_root_returns_none_for_a_super_call(self):
        """``super().bulk_create(...)`` inside a QuerySet override (real examples:
        ``VersionedQuerySet``, ``PinMarkupQuerySet``) can't be resolved to a model name
        from the AST alone. That's safe only because the guard instead catches the
        *caller's* ``Model.objects.bulk_create(...)`` line, which is what actually
        decided to do the write - not this internal delegation."""
        call = ast.parse("super().bulk_create(objs)").body[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        self.assertIsNone(_receiver_root(call.func.value))
