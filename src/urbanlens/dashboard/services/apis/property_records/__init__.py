"""Tiered US property ownership & tax record retrieval.

See ``docs/property-records-plan.md`` for the design this package
implements, and ``dashboard.plugins.builtin.property_records`` for how it's
wired into the app (pin-detail panel + background enrichment writing
``WikiOwner``/``WikiPropertySale`` rows).

Module map:

* ``schema`` - the standardized ``PropertyRecord`` shape every tier normalizes into.
* ``jurisdiction`` - resolves coordinates/an address to a ``PropertyJurisdiction`` registry row.
* ``field_mapping`` - raw source attribute dict -> standardized field names.
* ``pacing`` - per-host politeness pacing + 429/503 backoff shared by every fetch path.
* ``arcgis_socrata`` - the Tier 1 (free structured REST) gateway.
* ``vendor_templates`` - Tier 2: one ``ScrapeRecipe`` factory per known vendor platform.
* ``html_scrape`` - the shared Tier 2/3 bounded-recipe scrape engine.
* ``discovery`` - finds/validates Tier 1 endpoints and Tier 3 recipes for the registry.
* ``merge`` - per-field merging when more than one tier answered.
* ``orchestrator`` - tries tiers in order for one jurisdiction and returns a ``PropertyRecord``.
* ``meta`` - shared constants (``SCRAPE_USER_AGENT``) and the transient ``SourceUnreachableError``.
"""
from urbanlens.dashboard.services.apis.property_records.meta import SCRAPE_USER_AGENT, SourceUnreachableError

__all__ = ["SCRAPE_USER_AGENT", "SourceUnreachableError"]
