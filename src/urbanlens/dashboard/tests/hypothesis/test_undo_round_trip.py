"""Every undo handler must restore its model's own fields intact.

The framework's contract is deliberately narrow - cascade-deleted children are
gone before ``serialize`` ever runs, so a handler only promises to bring back the
instance's *own* fields plus a few cheap relations (see ``UndoHandler``'s
docstring). This asserts that narrow promise actually holds, for every registered
handler at once.

The failure this guards against is a handler that quietly omits a field: the undo
appears to work, the row comes back, and one column silently reverts to its
default. Per-handler tests don't catch it on the handler nobody wrote one for,
which is why this iterates the registry instead - and why it fails when a handler
is registered with no builder here, rather than skipping it.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import _HANDLERS, get_handler

#: Identity and bookkeeping columns that are *expected* to change on restore -
#: a restored row is a new row, and `slug` is regenerated rather than reused so
#: it cannot collide with something created since the delete.
_EXPECTED_TO_DIFFER = frozenset({"id", "pk", "created", "updated", "uuid", "slug"})


class UndoRoundTripTests(TestCase):
    """serialize → delete → restore must not lose a field."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = baker.make(Location, official_name="Undo Round Trip Place")

    def _build(self, model_label: str) -> Any:
        """Return a populated instance for ``model_label``, or None if unhandled.

        Values are set explicitly rather than via ``_fill_optional``: baker cannot
        generate the PostGIS geometry columns several of these models carry.
        """
        builders = {
            "pin": lambda: baker.make(
                Pin, profile=self.profile, location=self.location,
                name="Undo Pin", description="personal notes", priority=3, danger=2, vulnerability=1,
            ),
            "pin_list": lambda: baker.make(PinList, profile=self.profile, name="Undo List", description="list notes"),
            "trip": lambda: baker.make(Trip, creator=self.profile, name="Undo Trip", description="trip notes"),
            "saved_filter": lambda: baker.make(SavedFilter, profile=self.profile, name="Undo Filter"),
            "markup_map": lambda: baker.make(MarkupMap, profile=self.profile, title="Undo Map"),
            "safety_checkin": lambda: baker.make(SafetyCheckin, profile=self.profile, title="Undo Checkin"),
            "label": lambda: baker.make(Label, profile=self.profile, kind="tag", name="Undo Label"),
            "wiki": lambda: baker.make(Wiki, location=self.location, name="Undo Wiki", description="wiki body"),
        }
        builder = builders.get(model_label)
        return builder() if builder else None

    def test_every_registered_handler_has_a_builder_here(self) -> None:
        """A new handler must come with coverage, rather than silently skipping it."""
        missing = [label for label in sorted(_HANDLERS) if self._build(label) is None]

        self.assertEqual(missing, [], f"undo handlers with no round-trip coverage: {missing}")

    def test_no_handler_loses_a_field_across_a_round_trip(self) -> None:
        losses: list[str] = []
        checked = 0

        for model_label in sorted(_HANDLERS):
            instance = self._build(model_label)
            if instance is None:
                continue  # reported by the companion test above
            handler = get_handler(model_label)
            model = type(instance)
            before = {field.name: getattr(instance, field.attname, None) for field in model._meta.local_fields}

            payload = handler.serialize([instance])
            instance.delete()
            restored = handler.restore(payload)
            self.assertTrue(restored, f"{model_label}: restore() returned nothing")

            for field in model._meta.local_fields:
                if field.name in _EXPECTED_TO_DIFFER:
                    continue
                checked += 1
                old_value = before.get(field.name)
                new_value = getattr(restored[0], field.attname, None)
                if old_value != new_value:
                    losses.append(f"{model_label}.{field.name}: {old_value!r} became {new_value!r}")

        # Guards the sweep itself: a registry or model refactor that stopped
        # producing comparisons would otherwise leave this passing vacuously.
        self.assertGreater(checked, 100, f"only {checked} fields compared - the sweep is not exercising the handlers")
        self.assertEqual(losses, [], "\n".join(losses))
