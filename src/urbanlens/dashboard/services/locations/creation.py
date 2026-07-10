"""Explicit, user-initiated creation of community Wikis for a pin's Location.

Wikis are never created automatically any more: the user clicks "Create
community wiki" on the pin detail page and chooses which of their pin's fields
(if any) to seed the new wiki with. External enrichment (Google place linking,
name resolution, boundary generation) runs afterwards in a Celery task so pin
creation and bulk imports never touch external APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: Security fields shared between Pin and Wiki (both inherit SecurityModel).
SECURITY_FIELDS = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

#: Pin fields (or field groups) a user may copy into a newly created wiki.
#: Keys are the tokens posted by the create-wiki dialog.
SEEDABLE_FIELDS = ("name", "description", "date_abandoned", "date_last_active", "security", "badges")


@dataclass(slots=True)
class WikiCreationService:
    """Create the community Wiki for a pin's Location, seeded from chosen pin fields."""

    def create_for_pin(self, pin: Pin, *, include_fields: set[str] | None = None) -> tuple[Wiki, bool]:
        """Create (or fetch) the Wiki for a pin's Location and link the pin to it.

        Args:
            pin: The pin whose Location gets a community wiki.
            include_fields: Subset of :data:`SEEDABLE_FIELDS` the user chose to
                copy from their pin into the new wiki. Ignored when the wiki
                already exists (never overwrite community content with
                personal data).

        Returns:
            Tuple of (Wiki, created).

        Raises:
            ValueError: If the pin has no Location to attach a wiki to.
        """
        if pin.location_id is None:
            raise ValueError("Cannot create a wiki for a pin without a Location")

        include = {f for f in (include_fields or set()) if f in SEEDABLE_FIELDS}
        location: Location = pin.location
        defaults: dict = {}
        if "name" in include and pin.name and pin.name.strip():
            defaults["name"] = pin.name.strip()
        if "description" in include and pin.description:
            defaults["description"] = pin.description
        if "date_abandoned" in include and pin.date_abandoned:
            defaults["date_abandoned"] = pin.date_abandoned
        if "date_last_active" in include and pin.date_last_active:
            defaults["date_last_active"] = pin.date_last_active
        if "security" in include:
            for field in SECURITY_FIELDS:
                value = getattr(pin, field, None)
                if value and value != "unknown":
                    defaults[field] = value

        with transaction.atomic():
            wiki, created = Wiki.objects.get_or_create_for_location(location, defaults=defaults)
            if created and "badges" in include:
                wiki.badges.set(pin.badges.all())
            # Link the pin (and any of the user's other pins on this location
            # that aren't linked yet) to the community wiki.
            Pin.objects.filter(pk=pin.pk).update(wiki=wiki)

        def _enqueue() -> None:
            from urbanlens.dashboard.services.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import enrich_wiki_location

            safely_enqueue_task(enrich_wiki_location, wiki.pk)

        if created:
            transaction.on_commit(_enqueue)
        return wiki, created


def seedable_field_values(pin: Pin) -> list[dict]:
    """Describe which pin fields have values worth offering in the create-wiki dialog.

    Args:
        pin: The pin whose fields are candidates for seeding.

    Returns:
        List of dicts with ``field``, ``label`` and a short display ``value``,
        one per seedable field that actually has content on this pin.
    """
    candidates: list[dict] = []
    if pin.name and pin.name.strip():
        candidates.append({"field": "name", "label": "Name", "value": pin.name.strip()})
    if pin.description:
        candidates.append({"field": "description", "label": "Description", "value": pin.description})
    if pin.date_abandoned:
        candidates.append({"field": "date_abandoned", "label": "Date abandoned", "value": pin.date_abandoned.strftime("%b %d, %Y") if hasattr(pin.date_abandoned, "strftime") else str(pin.date_abandoned)})
    if pin.date_last_active:
        candidates.append({"field": "date_last_active", "label": "Last active", "value": pin.date_last_active.strftime("%b %d, %Y") if hasattr(pin.date_last_active, "strftime") else str(pin.date_last_active)})
    known_security = [field for field in SECURITY_FIELDS if getattr(pin, field, "unknown") not in (None, "", "unknown")]
    if known_security:
        candidates.append({"field": "security", "label": "Security details", "value": ", ".join(known_security)})
    badge_names = list(pin.badges.values_list("name", flat=True))
    if badge_names:
        candidates.append({"field": "badges", "label": "Tags & categories", "value": ", ".join(badge_names[:8])})
    return candidates
