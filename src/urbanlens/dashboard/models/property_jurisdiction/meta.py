from urbanlens.dashboard.models import abstract


class AdapterType(abstract.TextChoices):
    """Which retrieval strategy a county's ``PropertyJurisdiction`` row uses.

    Mirrors the tiered fallback pipeline in ``docs/property-records-plan.md``:
    Tier 1 (``ARCGIS_REST``/``SOCRATA``) is free structured REST, no scraping;
    Tier 2 (``KNOWN_VENDOR``) is one adapter per assessor-site vendor platform
    shared by many counties; Tier 3 (``CUSTOM_SCRAPER``) is a bespoke,
    LLM-assisted recipe for a single county's unique site; Tier 4
    (``MANUAL_ONLY``) means no automated path exists at all - the pipeline
    must surface that plainly instead of failing silently. ``UNKNOWN`` is the
    default for a freshly-resolved jurisdiction nobody has researched yet;
    the registry is meant to grow from ``UNKNOWN`` toward a real tier over
    time (research, ``discover_property_jurisdiction`` command, or manual
    admin entry), not to be fully populated up front.
    """

    UNKNOWN = "unknown", "Not yet researched"
    ARCGIS_REST = "arcgis_rest", "ArcGIS REST (Tier 1)"
    SOCRATA = "socrata", "Socrata / CKAN (Tier 1)"
    KNOWN_VENDOR = "known_vendor", "Known vendor platform (Tier 2)"
    CUSTOM_SCRAPER = "custom_scraper", "Custom scraper (Tier 3)"
    MANUAL_ONLY = "manual_only", "Manual only (Tier 4)"
