"""The identities importers gave a pin, so a re-import finds the pin again wherever the user has since moved it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import CASCADE, CharField, ForeignKey, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.model import PinImportSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class PinImportKey(abstract.DashboardModel):
    """
    One importer's content-derived name for a pin (see services.pins.import_keys).
    A pin can hold any number: two Takeout rows at one point, an archive uuid, and every identity of a pin merged into it.
    Each (profile, source, key) names one pin, so a re-import never has two candidates.
    """

    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="import_keys")
    #: The pin's profile, copied so the uniqueness is per account; a pin never changes profile.
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="+")
    source = CharField(max_length=20, choices=PinImportSource.choices)
    key = CharField(max_length=255)

    if TYPE_CHECKING:
        pin_id: int
        profile_id: int

    def __str__(self) -> str:
        return f"[{self.pin_id}] {self.source}:{self.key}"

    @classmethod
    def attach(cls, pin: Pin, source: str, key: str) -> bool:
        """
        Give ``pin`` the identity (source, key) alongside any it already holds.
        Returns False, writing nothing, when another of the profile's pins already holds it: that pin is then the one a re-import finds.
        Runs in a savepoint, so a collision leaves an enclosing transaction usable."""
        try:
            with transaction.atomic():
                row, created = cls.objects.get_or_create(profile_id=pin.profile_id, source=source, key=key, defaults={"pin": pin})
        except IntegrityError:
            # A concurrent writer took the identity between the lookup and the insert.
            return False
        return created or row.pin_id == pin.pk

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_pin_import_keys"
        constraints = [
            UniqueConstraint(fields=["profile", "source", "key"], name="db_pin_import_key_unique_per_profile"),
        ]
