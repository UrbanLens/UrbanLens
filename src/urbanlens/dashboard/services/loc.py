from __future__ import annotations

from dataclasses import dataclass

from urbanlens.dashboard.services.gateway import Gateway


@dataclass(frozen=True, slots=True, kw_only=True)
class LOCJsonGateway(Gateway):
    """
    Gateway for accessing JSON data from the Library of Congress.
    """

    base_url: str = "https://loc.gov/api"

    def search_collections(self, query) -> dict:
        """
        Search the Library of Congress collections.
        """
        url = f"{self.base_url}/search.json"
        params = {"q": query}
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()
