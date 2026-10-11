"""Move building places, floorplans and swept-building records off Overture's legacy content-hash refs (REData P98).

Each legacy key is resolved through REData's ``/buildings/resolve/``; one REData never recorded is matched by footprint
against its parcel's current buildings. Ambiguous matches, and stable refs another place already holds (duplicates, to be merged), are reported and left alone. Never deletes, and safe to run again.

Dry-run by default; ``--apply`` writes. ``--check`` writes nothing and exits 1 while any legacy ref remains.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from urbanlens.dashboard.services.places import overture_refs

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    """Re-key legacy Overture refs to ``overture:<gers_id>``."""

    help = "Re-key building places, floorplans and swept buildings from legacy Overture refs to overture:<gers_id>. Dry-run unless --apply; --check gates P98's removal."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Register the command's flags.

        Args:
            parser: The argument parser.
        """
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true", help="Re-key; without it, only report what would move.")
        mode.add_argument("--check", action="store_true", help="Count the legacy refs that remain and exit 1 if there are any.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Report, re-key or check.

        Args:
            *args: Unused.
            **options: ``apply`` and ``check``.

        Raises:
            CommandError: ``--check`` found a legacy ref.
        """
        if options["check"]:
            remaining = overture_refs.legacy_refs_remaining()
            for store, count in remaining.items():
                self.stdout.write(f"{store}: {count} legacy Overture ref(s)")
            if any(remaining.values()):
                raise CommandError("Legacy Overture refs remain.")
            return

        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        apply: bool = options["apply"]
        report = overture_refs.migrate_legacy_refs(apply=apply, resolver=overture_refs.RedataResolver(), fetch_buildings=lambda uuid: RedataGateway().lookup_buildings(uuid))
        verb = "re-keyed" if apply else "would re-key"
        for row in report.places:
            moved = row.outcome in (overture_refs.RESOLVED, overture_refs.FOOTPRINT)
            line = f"  place {row.place} {row.old}: {verb + ' to ' + row.new if moved else 'left'} ({row.outcome}{', ' + row.detail if row.detail else ''})"
            self.stdout.write(line)
        for ref, stable in sorted(report.floorplans.items()):
            self.stdout.write(f"  floorplans {ref}: {verb} to {stable}")
        for ref, stable in sorted(report.pins.items()):
            self.stdout.write(f"  swept buildings {ref}: {verb} to {stable}")
        counts = ", ".join(f"{outcome} {count}" for outcome, count in sorted(report.outcomes().items())) or "none"
        self.stdout.write(f"{len(report.places)} legacy-keyed place(s): {counts}.")
        if not apply and (report.places or report.floorplans or report.pins):
            self.stdout.write("Nothing was written. Run with --apply to re-key.")
