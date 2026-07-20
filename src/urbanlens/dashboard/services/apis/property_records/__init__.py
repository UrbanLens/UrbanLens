"""US county property ownership & tax record retrieval.

The retrieval pipeline itself (jurisdiction resolution, ArcGIS/Socrata, vendor
scraping, bespoke recipes) now lives in the standalone REData service - see
``docs/redata.md``. This package is just ``redata_gateway``, the REST client
that talks to it; see ``dashboard.plugins.builtin.property_records`` for how
it's wired into the app (pin-detail panel + background enrichment writing
``WikiOwner``/``WikiPropertySale`` rows).
"""

from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_BLOCKED,
    REASON_MANUAL_ONLY,
    REASON_SOURCE_ERROR,
    PropertyRecordsUnavailableError,
    RedataGateway,
)

__all__ = ["REASON_BLOCKED", "REASON_MANUAL_ONLY", "REASON_SOURCE_ERROR", "PropertyRecordsUnavailableError", "RedataGateway"]
