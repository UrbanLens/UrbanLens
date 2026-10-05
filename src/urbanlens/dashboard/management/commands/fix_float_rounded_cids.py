"""Repair or delete the import failures whose Google Maps CID lost its low digits to a float64 (REData ``docs/PROBLEMS.md`` P120).

The import wizard used to send each pin's CID through the browser as a JSON number, which a
JavaScript ``Number`` returns with its low digits zeroed. Such a CID names no place; REData asked
Google about 1,945 of them, nightly, for months.

A dry run by default: it reports what it would change, per table. ``--execute`` applies all of it in
one transaction.

- ``dashboard_google_places``: a float-shaped CID is counted and kept. Every row holds coordinates,
  so its CID resolved, and a real CID has the float-rounded shape about once in a few hundred; a
  rounded CID never resolved, so it cannot have reached this table through the import.
- ``dashboard_pin_import_failures``: a float-shaped CID is replaced by the CID its own Google Maps URL
  states when that URL's CID rounds to it; deleted when nothing can recover it, or when the owner
  already has a failure for the recovered CID. A float-shaped CID its URL agrees with is real, and kept.

TEMPORARY: delete once run against production.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand
from django.db import transaction

from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure
from urbanlens.dashboard.services.apis.locations.cid_validation import FLOAT64_EXACT_LIMIT, cid_stated_by, float_rounded, looks_float_rounded


@dataclass
class _Plan:
    places_kept: int = 0
    failures_to_repair: dict[int, int] = field(default_factory=dict)
    failures_to_delete: list[int] = field(default_factory=list)
    failures_corroborated: int = 0


def _plan() -> _Plan:
    plan = _Plan()
    for cid in GooglePlace.objects.filter(cid__gt=FLOAT64_EXACT_LIMIT).values_list("cid", flat=True).iterator():
        if cid is not None and looks_float_rounded(int(cid)):
            plan.places_kept += 1

    taken = {(profile_id, int(cid)) for profile_id, cid in PinImportFailure.objects.values_list("profile_id", "cid").iterator()}
    for pk, profile_id, cid, maps_url in PinImportFailure.objects.filter(cid__gt=FLOAT64_EXACT_LIMIT).values_list("pk", "profile_id", "cid", "maps_url").iterator():
        rounded = int(cid)
        if not looks_float_rounded(rounded):
            continue
        stated = cid_stated_by(maps_url)
        if stated == rounded:
            plan.failures_corroborated += 1
        elif stated is not None and float_rounded(stated) == rounded and (profile_id, stated) not in taken:
            plan.failures_to_repair[pk] = stated
            taken.add((profile_id, stated))
        else:
            plan.failures_to_delete.append(pk)
    return plan


class Command(BaseCommand):
    """Repair or delete every import failure whose CID has visibly been through a float64."""

    help = "Repair or delete pin import failures whose Google Maps CID was rounded through a float64 (REData P120). Dry run unless --execute."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="Apply the changes. Without it, only report them.")

    def handle(self, *args, **options):
        execute = options["execute"]
        with transaction.atomic():
            plan = _plan()
            verb = "" if execute else "would be "
            self.stdout.write(f"dashboard_google_places: {plan.places_kept} float-shaped cid(s) kept (each row holds coordinates, so its cid resolved)")
            self.stdout.write(f"dashboard_pin_import_failures: {len(plan.failures_to_repair)} {verb}repaired from maps_url, {len(plan.failures_to_delete)} {verb}deleted, {plan.failures_corroborated} kept (maps_url confirms the cid)")
            if not execute:
                self.stdout.write("Dry run - nothing changed. Re-run with --execute to apply.")
                return
            PinImportFailure.objects.filter(pk__in=plan.failures_to_delete).delete()
            for pk, cid in plan.failures_to_repair.items():
                PinImportFailure.objects.filter(pk=pk).update(cid=cid)
        self.stdout.write("Done.")
