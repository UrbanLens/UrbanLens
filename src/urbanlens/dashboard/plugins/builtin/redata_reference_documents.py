"""REData reference-documents plugin: the rate budget the Media gallery's archive providers share."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory


class ReferenceDocumentsPlugin(UrbanLensPlugin):
    """Rate limits for REData's reference-documents search, which every REData-backed archive provider calls."""

    name: ClassVar[str] = "redata_reference_documents"
    verbose_name: ClassVar[str] = "REData Reference Documents"
    description: ClassVar[str] = (
        "The shared rate budget for REData's reference-documents search, through which the Media gallery's archive providers (Smithsonian, Library of Congress, Digital Commonwealth, Internet Archive, Chronicling America) look a place up by name."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's reference-documents search."""
        return {
            "redata_reference_documents": ServiceDefaults(
                display_name="REData Reference Documents",
                category=EgressCategory.REDATA,
                calls_per_minute=20,
                calls_per_day=None,
                notes=("Archival lookups by name via GET /reference-documents/search/, made by the Media gallery's archive providers. See services.apis.locations.redata_reference_documents_gateway."),
            ),
        }
