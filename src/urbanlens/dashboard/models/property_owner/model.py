"""Property owner and sale-history models.

Every record is either PRIVATE (attached to one Pin, visible only there) or
SHARED (attached to a Location, visible to everyone with a pin there) - see
``meta.OwnerVisibility``. This mirrors the codebase's existing private/shared
split: private records are the ``PinNote`` pattern (``models.pin.note``, a
private per-pin FK); shared records are the ``Location``/``Wiki`` pattern
(shared, community-editable, gated by ``services.wiki_access.location_visible_to``).

``source`` (``meta.OwnerSource``) distinguishes user-contributed data from
data a future automated source would populate (``OFFICIAL``) - see the
``OwnerSource`` docstring for why OFFICIAL records are never directly
user-editable and why they're SHARED-only. Both ``Owner`` and ``PropertySale``
use the same M2M-per-scope shape so a future plugin only ever needs to touch
the SHARED/OFFICIAL surface (``Owner.locations``, ``PropertySale.location``) -
exactly the single, de-duplicated, Location-keyed record every pin and the
wiki already read from, with no per-viewer duplication and no separate cache
layer needed (mirrors how ``EpaFacility`` is a persisted relational model,
not a time-limited cache blob).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, CheckConstraint, DateField, DecimalField, EmailField, ForeignKey, Index, ManyToManyField, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.property_owner.meta import OwnerSource, OwnerVisibility
from urbanlens.dashboard.models.property_owner.queryset import OwnerManager, PropertySaleManager


class Owner(abstract.DashboardModel):
    """An individual or company that owns one or more properties.

    ``pins`` (when ``visibility=PRIVATE``) or ``locations`` (when
    ``visibility=SHARED``) is the M2M actually populated - either way an
    owner can be linked to any number of properties, and unlinking a
    property never deletes the Owner record (see the controller's remove
    views): previous ownership is never lost, only no longer "current."
    """

    name = CharField(max_length=200)
    company_name = CharField(max_length=200, blank=True, default="")
    address = TextField(blank=True, default="")
    phone = CharField(max_length=50, blank=True, default="")
    email = EmailField(blank=True, default="")
    notes = TextField(blank=True, default="")

    visibility = CharField(max_length=10, choices=OwnerVisibility.choices, default=OwnerVisibility.SHARED)
    source = CharField(max_length=10, choices=OwnerSource.choices, default=OwnerSource.USER)
    created_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="owners_created")

    # Populated only when visibility=PRIVATE - the one pin this owner is
    # private to. May list several of that pin's own profile's pins.
    pins = ManyToManyField("dashboard.Pin", related_name="private_owners", blank=True)
    # Populated only when visibility=SHARED - every location this owner
    # currently owns, community-wide.
    locations = ManyToManyField("dashboard.Location", related_name="owners", blank=True)

    if TYPE_CHECKING:
        created_by_id: int | None

    objects = OwnerManager()

    def __str__(self) -> str:
        """Return a human-readable label for this owner.

        Returns:
            The owner's name, with their company appended in parentheses when set.
        """
        return f"{self.name} ({self.company_name})" if self.company_name else self.name

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_property_owner"
        ordering = ["name"]
        constraints = [
            CheckConstraint(
                condition=Q(source=OwnerSource.USER) | Q(visibility=OwnerVisibility.SHARED),
                name="db_property_owner_official_is_shared",
            ),
        ]


class PropertySale(abstract.DashboardModel):
    """A recorded sale of a Location, linking any number of previous and new owners.

    ``previous_owners``/``new_owners`` are M2M (not single FKs) - a sale can
    record any number of co-owners on either side, and nothing caps how many
    sale records a location can accumulate over time, so the full ownership
    chain is never lost to a fixed-size field.
    """

    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="sales")
    # Populated only when visibility=PRIVATE - the one pin this sale record
    # is private to.
    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="private_sales")

    visibility = CharField(max_length=10, choices=OwnerVisibility.choices, default=OwnerVisibility.SHARED)
    source = CharField(max_length=10, choices=OwnerSource.choices, default=OwnerSource.USER)
    created_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="property_sales_created")

    sale_price = DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sale_date = DateField(null=True, blank=True)
    previous_owners = ManyToManyField(Owner, related_name="sales_as_previous_owner", blank=True)
    new_owners = ManyToManyField(Owner, related_name="sales_as_new_owner", blank=True)
    notes = TextField(blank=True, default="")

    if TYPE_CHECKING:
        location_id: int
        pin_id: int | None
        created_by_id: int | None

    objects = PropertySaleManager()

    def __str__(self) -> str:
        """Return a human-readable label for this sale record.

        Returns:
            A string like "Sale of location <id> on <date>", or "(undated)" if unset.
        """
        when = self.sale_date.isoformat() if self.sale_date else "(undated)"
        return f"Sale of location {self.location_id} on {when}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_property_sale"
        ordering = ["-sale_date", "-created"]
        indexes = [
            Index(fields=["location", "-sale_date"], name="idxdb_property_sale_location_date"),
        ]
        constraints = [
            CheckConstraint(
                condition=Q(sale_price__isnull=True) | Q(sale_price__gte=0),
                name="db_property_sale_price_gte_0",
            ),
            CheckConstraint(
                condition=Q(source=OwnerSource.USER) | Q(visibility=OwnerVisibility.SHARED),
                name="db_property_sale_official_is_shared",
            ),
            CheckConstraint(
                condition=Q(visibility=OwnerVisibility.SHARED) | Q(pin__isnull=False),
                name="db_property_sale_private_requires_pin",
            ),
        ]
