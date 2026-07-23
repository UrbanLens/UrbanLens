"""Property owner and sale-history models.

Mirrors the ``PinAlias``/``WikiAlias`` split (``models.aliases.model``) - two
entirely separate models per concept, never a single model with a
visibility flag. ``PinOwner``/``PinPropertySale`` are private, FK'd straight
to one ``Pin``, definitionally invisible to anyone else and to the wiki - no
"private" flag exists because there is nothing else it could mean.
``WikiOwner``/``WikiPropertySale`` are shared, community-editable data about
the place (``Location``), visible to anyone with a pin there - the same
access rule as the rest of the wiki (``services.wiki_access.location_visible_to``).
A pin's own Ownership card must only ever query ``PinOwner``/``PinPropertySale``;
the wiki's Ownership card must only ever query ``WikiOwner``/``WikiPropertySale``
- never both in the same view, exactly like ``PinAlias``/``WikiAlias`` are
never queried together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, CharField, CheckConstraint, DateField, DecimalField, EmailField, ForeignKey, Index, ManyToManyField, Q, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.queryset import PinOwnerManager, PinPropertySaleManager, WikiOwnerManager, WikiPropertySaleManager


class _OwnerBase(abstract.DashboardModel):
    """Shared fields for PinOwner and WikiOwner."""

    name = CharField(max_length=200)
    company_name = CharField(max_length=200, blank=True, default="")
    address = TextField(blank=True, default="")
    phone = CharField(max_length=50, blank=True, default="")
    email = EmailField(blank=True, default="")
    notes = TextField(blank=True, default="")

    class Meta(abstract.DashboardModel.Meta):
        abstract = True
        ordering = ["name"]

    def __str__(self) -> str:
        """Return a human-readable label for this owner.

        Returns:
            The owner's name, with their company appended in parentheses when set.
        """
        return f"{self.name} ({self.company_name})" if self.company_name else self.name


class PinOwner(_OwnerBase):
    """An owner private to one Pin, visible only to that pin's own profile.

    Ownership is derived from pin.profile; no separate profile FK is needed
    (mirrors ``PinAlias``).
    """

    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="owners")

    if TYPE_CHECKING:
        pin_id: int

    objects = PinOwnerManager()

    class Meta(_OwnerBase.Meta):
        db_table = "dashboard_pin_owner"
        indexes = [Index(fields=["pin"], name="idxdb_pinowner_pin")]


class WikiOwner(_OwnerBase):
    """An owner shared with everyone who has this location pinned.

    ``locations`` (not a Wiki FK) so the same real-world owner can be linked
    to any number of properties, the way one landlord or company genuinely
    can own many distinct places - unlinking a location never deletes the
    record (see the controller's remove view): previous ownership is never
    lost, only no longer "current."
    """

    locations = ManyToManyField("dashboard.Location", related_name="owners", blank=True)
    source = CharField(max_length=10, choices=OwnerSource.choices, default=OwnerSource.USER)
    created_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="wiki_owners_created")

    if TYPE_CHECKING:
        created_by_id: int | None

    objects = WikiOwnerManager()

    class Meta(_OwnerBase.Meta):
        db_table = "dashboard_wiki_owner"


class _PropertySaleBase(abstract.DashboardModel):
    """Shared fields for PinPropertySale and WikiPropertySale."""

    sale_price = DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sale_date = DateField(null=True, blank=True)
    notes = TextField(blank=True, default="")

    class Meta(abstract.DashboardModel.Meta):
        abstract = True
        ordering = ["-sale_date", "-created"]


class PinPropertySale(_PropertySaleBase):
    """A sale record private to one Pin.

    ``previous_owners``/``new_owners`` are M2M to ``PinOwner`` (not single
    FKs) - a sale can record any number of co-owners on either side, and
    nothing caps how many sale records a pin can accumulate over time.
    """

    pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, related_name="property_sales")
    previous_owners = ManyToManyField(PinOwner, related_name="sales_as_previous_owner", blank=True)
    new_owners = ManyToManyField(PinOwner, related_name="sales_as_new_owner", blank=True)

    if TYPE_CHECKING:
        pin_id: int

    objects = PinPropertySaleManager()

    def __str__(self) -> str:
        """Return a human-readable label for this sale record.

        Returns:
            A string like "Sale of pin <id> on <date>", or "(undated)" if unset.
        """
        when = self.sale_date.isoformat() if self.sale_date else "(undated)"
        return f"Sale of pin {self.pin_id} on {when}"

    class Meta(_PropertySaleBase.Meta):
        db_table = "dashboard_pin_property_sale"
        indexes = [Index(fields=["pin", "-sale_date"], name="idxdb_pin_sale_pin_date")]
        constraints = [
            CheckConstraint(condition=Q(sale_price__isnull=True) | Q(sale_price__gte=0), name="db_pin_sale_price_gte_0"),
        ]


class WikiPropertySale(_PropertySaleBase):
    """A sale record shared with everyone who has this location pinned.

    ``previous_owners``/``new_owners`` are M2M to ``WikiOwner`` for the same
    reason as ``PinPropertySale``.
    """

    location = ForeignKey("dashboard.Location", on_delete=CASCADE, related_name="sales")
    source = CharField(max_length=10, choices=OwnerSource.choices, default=OwnerSource.USER)
    created_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="wiki_property_sales_created")
    previous_owners = ManyToManyField(WikiOwner, related_name="sales_as_previous_owner", blank=True)
    new_owners = ManyToManyField(WikiOwner, related_name="sales_as_new_owner", blank=True)

    if TYPE_CHECKING:
        location_id: int
        created_by_id: int | None

    objects = WikiPropertySaleManager()

    def __str__(self) -> str:
        """Return a human-readable label for this sale record.

        Returns:
            A string like "Sale of location <id> on <date>", or "(undated)" if unset.
        """
        when = self.sale_date.isoformat() if self.sale_date else "(undated)"
        return f"Sale of location {self.location_id} on {when}"

    class Meta(_PropertySaleBase.Meta):
        db_table = "dashboard_wiki_property_sale"
        indexes = [Index(fields=["location", "-sale_date"], name="idxdb_wiki_sale_location_date")]
        constraints = [
            CheckConstraint(condition=Q(sale_price__isnull=True) | Q(sale_price__gte=0), name="db_wiki_sale_price_gte_0"),
        ]
