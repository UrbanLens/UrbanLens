from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway


@dataclass(slots=True, kw_only=True)
class RealEstateSalesGateway(Gateway):
    """
    Gateway for accessing real estate sales data from data.gov
    """

    service_key: ClassVar[str] = "datagov"
    paid_service: ClassVar[bool] = False

    base_url: str = "https://data.ct.gov/api/views/5mzw-sjtu/rows.json"
    data_dictionary_url: str = "https://data.ct.gov/api/views/5mzw-sjtu/columns.json"

    def get_sales_data(self, access_type="DOWNLOAD") -> dict:
        """
        Fetch the real estate sales data.
        """
        params = {"accessType": access_type}
        response = self.session.get(self.base_url, params=params)
        response.raise_for_status()
        return response.json()

    def get_data_dictionary(self) -> dict:
        """
        Fetch the data dictionary to understand the structure of the real estate data.
        """
        response = self.session.get(self.data_dictionary_url)
        response.raise_for_status()
        return response.json()
