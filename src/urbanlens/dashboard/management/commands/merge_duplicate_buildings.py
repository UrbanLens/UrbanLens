"""
Merge duplicate building places - one real building recorded twice - into one (REData P98).

A duplicate is found by REData's own identity record: a legacy-keyed place whose content hash REData resolves to an
``overture:<gers_id>`` that another place holds (or that another legacy place resolves to as well). Each group's
survivor is the place holding that key, else the oldest with a wiki. Its losers' locations, wiki and place rows move
to it; a loser is kept, SUPERSEDED and unkeyed, with a ``PlaceMerge`` record of what moved and what was dropped.

Anything resting on geometry alone - a footprint match, two sources' outlines holding each other's centroids, a
placeless wiki inside a building - or a group spanning parcels is reported and never merged.

Dry-run by default; ``--apply`` writes. Safe to run again: a merged loser is never merged twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand

from urbanlens.dashboard.services.places import building_merge, overture_refs

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from urbanlens.dashboard.models.place.model import Place


def _describe(place: Place, wikis: dict[int, tuple[int, str]]) -> str:
    wiki = wikis.get(place.pk)
    return f"place {place.pk} {place.provider_key or '(unkeyed)'}" + (f' [wiki {wiki[0]} "{wiki[1]}"]' if wiki else "")


class Command(BaseCommand):
    """Merge duplicate building places sharing one Overture building."""

    help = "Merge building places REData says are one Overture building; report geometry-only and cross-parcel duplicates. Dry-run unless --apply."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """
        Register the command's flags.

        Args:
            parser: The argument parser.
        """
        parser.add_argument("--apply", action="store_true", help="Merge; without it, only report what would move.")

    def handle(self, *args: Any, **options: Any) -> None:
        """
        Detect, then report or merge.

        Args:
            *args: Unused.
            **options: ``apply``.
        """
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        apply: bool = options["apply"]
        resolver = overture_refs.RedataResolver()
        detection = building_merge.detect_duplicate_buildings(
            resolver=resolver,
            fetch_buildings=lambda uuid: RedataGateway().lookup_buildings(uuid),
            locate_parcel=lambda latitude, longitude: RedataGateway().lookup_parcel_uuid(latitude, longitude),
        )
        member_ids = [member.pk for group in detection.groups for member in group.members]
        wikis = {place_id: (pk, name) for pk, place_id, name in Wiki.objects.filter(place_id__in=member_ids).values_list("pk", "place_id", "name")}

        losers = 0
        for group in detection.groups:
            self.stdout.write(f"{group.stable_ref} ({len(group.members)} places):")
            self.stdout.write(f"  survivor {_describe(group.survivor, wikis)}")
            plans = building_merge.plan_group(group)
            for loser in group.losers:
                moves = ", ".join(f"{name.replace('_', ' ')}: {value}" for name, value in plans[loser.pk].items() if value not in (0, "none"))
                self.stdout.write(f"  loser    {_describe(loser, wikis)}: {moves or 'nothing attached'}")
            if apply:
                for record in building_merge.merge_group(group):
                    self.stdout.write(f"  merged place {record.loser_id} into {record.survivor_id} (PlaceMerge {record.pk})")
            losers += len(group.losers)

        if detection.findings:
            self.stdout.write("Reported, not merged:")
            for finding in detection.findings:
                wikis_note = f" wikis {', '.join(map(str, finding.wikis))}" if finding.wikis else ""
                self.stdout.write(f"  {finding.kind}: places {', '.join(map(str, finding.places))}{wikis_note} - {finding.detail}")
        if detection.unavailable:
            self.stderr.write(f"REData could not answer for {len(detection.unavailable)} legacy place(s) ({', '.join(map(str, detection.unavailable))}); run again.")
        self.stdout.write(f"{len(detection.groups)} duplicate group(s), {losers} place(s) {'merged' if apply else 'to merge'}; {len(detection.findings)} finding(s) reported.")
        if not apply and detection.groups:
            self.stdout.write("Nothing was written. Run with --apply to merge.")
