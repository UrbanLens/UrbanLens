"""Tiered US property ownership & tax record retrieval.

See ``docs/property-records-plan.md`` for the design this package
implements, and ``dashboard.plugins.builtin.property_records`` for how it's
wired into the app (pin-detail panel + background enrichment writing
``WikiOwner``/``WikiPropertySale`` rows).

Module map:

* ``schema`` - the standardized ``PropertyRecord`` shape every tier normalizes into.
* ``jurisdiction`` - resolves coordinates/an address to a ``PropertyJurisdiction`` registry row.
* ``field_mapping`` - raw source attribute dict -> standardized field names.
* ``arcgis_socrata`` - the Tier 1 (free structured REST) gateway.
* ``orchestrator`` - tries tiers in order for one jurisdiction and returns a ``PropertyRecord``.
"""
from urbanlens.dashboard.services.apis.property_records.meta import SCRAPE_USER_AGENT