"""PropertyJurisdiction - the county-level registry driving property-record retrieval.

Per ``docs/property-records-plan.md`` section 1: there is no unified national
API for county assessor/treasurer/recorder data, so retrieval is dispatched
per-county through a tiered fallback pipeline (see
``services.apis.property_records.orchestrator``). This table is *the* piece
of long-lived infrastructure that pipeline depends on - "a table, not code"
that grows over time as counties get researched, either by the
``discover_property_jurisdiction`` management command or by direct admin
edits. A fresh row (created the first time a coordinate resolves into a new
county - see ``services.apis.property_records.jurisdiction``) starts at
``AdapterType.UNKNOWN`` with every URL blank; the orchestrator surfaces that
as a distinct "unresearched" outcome (vs the operator-asserted
``MANUAL_ONLY``) rather than erroring.
"""

from __future__ import annotations

from django.db.models import SET_NULL, BooleanField, CharField, DateTimeField, ForeignKey, Index, JSONField, TextField, URLField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType
from urbanlens.dashboard.models.property_jurisdiction.queryset import PropertyJurisdictionManager


class PropertyJurisdiction(abstract.DashboardModel):
    """One US county's (or county-equivalent's) property-record retrieval configuration.

    Keyed by the 5-digit Census FIPS county code (state FIPS + county FIPS),
    which is exactly what both ``CensusTigerwebGateway.get_geography`` (point
    lookups) and ``CensusGeocoderGateway`` (address lookups) return - the two
    jurisdiction-resolution paths in
    ``services.apis.property_records.jurisdiction.resolve_jurisdiction``.

    ``field_map`` lets a Tier 1 (ArcGIS/Socrata) row override the generic
    heuristic attribute-name matching in
    ``services.apis.property_records.field_mapping`` for a county whose layer
    uses non-standard field names - keys are the standardized
    ``PropertyRecord`` field names, values are the raw attribute name in that
    county's service (e.g. ``{"apn": "PARCELID", "owner_name": "OWNNAME"}``).

    ``scrape_recipe`` holds the Tier 3 (custom scraper) configuration - a
    bounded ``html_scrape.ScrapeRecipe`` dict describing one search request
    (which URL, which form field the address/APN goes into), never arbitrary
    code or selectors-to-execute (see the plan's compliance section on why AI
    output must stay strictly non-executable). It's populated by the
    ``discover_property_jurisdiction --tier3`` command or by hand, and parsed
    back through ``html_scrape.recipe_from_dict``, which treats the stored
    JSON as untrusted.
    """

    fips = CharField(max_length=5, unique=True, db_index=True, help_text="5-digit Census FIPS county code (2-digit state + 3-digit county).")
    county_name = CharField(max_length=200, blank=True, default="")
    state = CharField(max_length=2, blank=True, default="", help_text="USPS state abbreviation.")

    adapter_type = CharField(max_length=20, choices=AdapterType.choices, default=AdapterType.UNKNOWN)

    #: Tier 1 endpoints. ``gis_rest_url`` is the full queryable endpoint (an
    #: ArcGIS ``.../MapServer/<layer>`` or ``.../FeatureServer/<layer>``
    #: service URL, or a Socrata resource's ``.json`` SODA endpoint) - not
    #: just a host, so the generic gateway needs no other per-county config
    #: to issue a query beyond ``field_map``.
    gis_rest_url = URLField(max_length=500, blank=True, default="")
    #: Raw attribute name of the parcel/APN identifier, for non-spatial
    #: ``where``-clause fallback queries once an APN is already known.
    gis_id_field = CharField(max_length=100, blank=True, default="")
    #: Socrata-only: the dataset's point/location column name, needed for
    #: ``within_circle`` spatial SoQL queries (ArcGIS spatial queries need no
    #: equivalent - the geometry type is fixed per service).
    gis_geo_field = CharField(max_length=100, blank=True, default="")
    field_map = JSONField(default=dict, blank=True)

    #: Reference/citation URLs - not queried programmatically, but surfaced to
    #: the record's ``source`` metadata and to users when a jurisdiction is
    #: ``MANUAL_ONLY``.
    assessor_url = URLField(max_length=500, blank=True, default="")
    treasurer_url = URLField(max_length=500, blank=True, default="")
    recorder_url = URLField(max_length=500, blank=True, default="")

    #: Tier 2: which vendor platform runs this county's assessor site. Only
    #: effective once a matching ``vendor_templates.VendorTemplate`` is
    #: registered in code for the slug.
    vendor = CharField(max_length=100, blank=True, default="", help_text="Known vendor platform slug (Tyler, BS&A, qPublic, ...) matching a registered vendor template.")
    #: Tier 3: bounded per-county search recipe (see the class docstring).
    scrape_recipe = JSONField(default=dict, blank=True)

    requires_captcha = BooleanField(default=False, help_text="County site puts a CAPTCHA in front of search - never attempted programmatically (see plan compliance section).")
    manual_instructions = TextField(blank=True, default="", help_text="Shown to users when this jurisdiction has no automated path (phone/mail/in-person only).")

    last_verified = DateTimeField(null=True, blank=True, help_text="When a human or the discovery command last confirmed these endpoints still work.")
    notes = TextField(blank=True, default="")

    #: Set once discovery (deterministic search or AI-assisted parse) proposed
    #: this row's endpoint but a human hasn't confirmed it yet - surfaced in
    #: the admin so unverified auto-discovered rows are easy to find and
    #: review before the orchestrator's confidence score treats them as
    #: trusted as a manually-verified row.
    discovered_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="+")

    objects: PropertyJurisdictionManager = PropertyJurisdictionManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_property_jurisdiction"
        ordering = ["state", "county_name"]
        indexes = [
            Index(fields=["adapter_type"], name="idxdb_propjuris_adapter"),
        ]

    def __str__(self) -> str:
        label = f"{self.county_name}, {self.state}" if self.county_name else self.fips
        return f"{label} ({self.get_adapter_type_display()})"

    @property
    def is_automatable(self) -> bool:
        """Whether the orchestrator has any configured tier it would actually attempt for this jurisdiction.

        Mirrors the orchestrator's own dispatch rules: Tier 1 needs a
        matching adapter type plus an endpoint URL; the scraping tiers (a
        registered Tier 2 vendor template, or a Tier 3 recipe) are vetoed
        outright by ``requires_captcha``; ``MANUAL_ONLY`` is an operator's
        explicit "nothing here is automatable".
        """
        from urbanlens.dashboard.services.apis.property_records import vendor_templates

        if self.adapter_type == AdapterType.MANUAL_ONLY:
            return False
        if self.adapter_type in (AdapterType.ARCGIS_REST, AdapterType.SOCRATA) and self.gis_rest_url:
            return True
        if self.requires_captcha:
            return False
        return vendor_templates.get_template(self.vendor) is not None or bool(self.scrape_recipe)
