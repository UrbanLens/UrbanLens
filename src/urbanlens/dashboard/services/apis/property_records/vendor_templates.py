"""Tier 2 vendor templates: one ``ScrapeRecipe`` factory per known assessor-site vendor platform.

Per ``docs/property-records-plan.md`` section 2 (Tier 2): a handful of
vendors run assessor/treasurer sites for hundreds of counties each with
near-identical structure, so one adapter per vendor buys broad coverage
cheaply. Mechanically, a "vendor adapter" is just a :class:`VendorTemplate`
that builds a :class:`~html_scrape.ScrapeRecipe` from a jurisdiction's own
``gis_rest_url`` (reused here as "this vendor's base URL for this specific
county" - the same per-jurisdiction-endpoint role it plays for Tier 1) and
``field_map`` override - execution then goes through the exact same
``html_scrape``/``compliance``/``normalize`` pipeline as a Tier 3 recipe.

:data:`VENDOR_TEMPLATES` starts **empty**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
    from urbanlens.dashboard.services.apis.property_records.html_scrape import ScrapeRecipe


@dataclass(frozen=True, slots=True)
class VendorTemplate:
    """One vendor platform's recipe factory.

    Attributes:
        display_name: Shown as the merged record's ``source.provider``.
        build_recipe: Builds a ``ScrapeRecipe`` for one jurisdiction row -
            typically substitutes ``jurisdiction.gis_rest_url`` (this
            vendor's per-county base URL) into an otherwise-fixed recipe
            shape shared by every county on the platform.
        field_map: Optional vendor-wide raw-label-name override, for a
            vendor whose page labels don't match ``field_mapping.py``'s
            generic heuristics. Takes precedence over the *jurisdiction's*
            own ``field_map`` for Tier 2 - a vendor's markup is the same
            across every county running it, so a per-county override
            wouldn't make sense the way it does for Tier 1's varying GIS
            layers.
    """

    display_name: str
    build_recipe: Callable[[PropertyJurisdiction], ScrapeRecipe]
    field_map: dict[str, str] | None = None


#: Vendor slug (``PropertyJurisdiction.vendor``) -> template. See the module
#: docstring for why this starts empty.
VENDOR_TEMPLATES: dict[str, VendorTemplate] = {}


def get_template(vendor: str) -> VendorTemplate | None:
    """Look up a vendor's template by slug.

    Args:
        vendor: A ``PropertyJurisdiction.vendor`` value.

    Returns:
        The registered template, or None when nothing is registered for
        that slug yet (including an empty ``vendor`` string).
    """
    if not vendor:
        return None
    return VENDOR_TEMPLATES.get(vendor)
