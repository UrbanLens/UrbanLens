"""Queue the property bootstrap a new root pin gets (``services.pins.bootstrap``) for an existing root pin.

For a pin made before bootstraps existed, or one whose property should be filled now rather than when somebody opens
it. Every stage skips what is already stored, so a property that is complete costs nothing.
"""

from __future__ import annotations

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.pins.bootstrap import BootstrapStage, NextStep, bootstrap_eligible, enqueue_step


class Command(BaseCommand):
    """Queue one root pin's property bootstrap."""

    help = "Queue the parcel, buildings, building pins and wikis, site panels and build dates for an existing root pin."

    def add_arguments(self, parser):
        parser.add_argument("pins", nargs="+", help="Root pins, by uuid or slug.")

    def handle(self, *args, **options):
        for reference in options["pins"]:
            pin = _find(reference)
            if pin is None:
                raise CommandError(f"No pin {reference!r}.")
            if not bootstrap_eligible(pin):
                raise CommandError(f"Pin {reference!r} is not a root pin with coordinates whose owner allows external lookups.")
            enqueue_step(pin.pk, NextStep(BootstrapStage.BOUNDARY))
            self.stdout.write(f"Queued the bootstrap of pin {pin.pk}.")


def _find(reference: str) -> Pin | None:
    """A pin by uuid or slug."""
    pins = Pin.objects.select_related("location", "profile")
    try:
        return pins.filter(uuid=UUID(reference)).first()
    except ValueError:
        return pins.filter(slug=reference).first()
