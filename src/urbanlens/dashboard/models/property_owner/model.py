"""Property owner and sale-history models.

An Owner is a fact about a Location (the shared, canonical place record), not
about any one user's Pin - ownership doesn't change per-viewer the way a pin's
name or notes do. See ``models.location.model.Location`` and ``models.pin.model.Pin``
for the full Location/Pin split this mirrors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, CheckConstraint, DateField, DecimalField, EmailField, ForeignKey, Index, ManyToManyField, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.property_owner.queryset import OwnerManager, PropertySaleManager


class Owner(abstract.DashboardModel):
    """An individual or company that owns one or more Locations.

    Ownership is entered and maintained by any user who has a pin at one of
    the associated locations (see ``services.wiki_access.location_visible_to``,
    reused as the edit-permission gate) - it is shared, community-editable
    data about the place, not private to whoever entered it.
    """

    name = CharField(max_length=200)
    company_name = CharField(max_length=200, blank=True, default="")
    address = TextField(blank=True, default="")
    phone = CharField(max_length=50, blank=True, default="")
    email = EmailField(blank=True, default="")
    notes = TextField(blank=True, default="")

    locations = ManyToManyField("dashboard.Location", related_name="owners", blank=True)

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


class PropertySale(abstract.DashboardModel):
    """A recorded sale of a Location, linking its previous and new owner.

    ``previous_owner``/``new_owner`` are nullable - a sale can be entered with
    incomplete ownership history (e.g. the price and date are known, but the
    previous owner isn't).
    """

    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="sales")
    sale_price = DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sale_date = DateField(null=True, blank=True)
    previous_owner = ForeignKey(Owner, on_delete=SET_NULL, null=True, blank=True, related_name="sales_as_previous_owner")
    new_owner = ForeignKey(Owner, on_delete=SET_NULL, null=True, blank=True, related_name="sales_as_new_owner")
    notes = TextField(blank=True, default="")

    if TYPE_CHECKING:
        location_id: int
        previous_owner_id: int | None
        new_owner_id: int | None

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
        ]
