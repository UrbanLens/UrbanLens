from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import json
import logging
from typing import ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True, kw_only=True)
class WeatherForecastGateway(Gateway):
    service_key: ClassVar[str] = "openweathermap"

    api_key: str | None = settings.openweathermap_api_key
    base_url: str = "http://api.openweathermap.org/data/2.5/forecast"

    def __post_init__(self):
        Gateway.__post_init__(self)
        if not self.api_key:
            raise ValueError("OpenWeatherMap API key must be provided.")

    def get_weather_forecast(self, latitude: float | Decimal, longitude: float | Decimal) -> list[dict] | None:
        """
        Retrieve a weather forecast for the given coordinates.
        """
        params = {
            "lat": float(latitude),
            "lon": float(longitude),
            # "cnt": 7,
            "appid": self.api_key,
            "units": "imperial",
        }

        result = self.get(params)
        if result is None:
            logger.error("Failed to retrieve weather forecast for coordinates (%s, %s)", latitude, longitude)
            return None

        # OpenWeatherMap returns a 4-hour forecast. We only want morning and evening for each day.
        return self.filter_forecast(result.get("list", []))

    def get(self, params: dict) -> dict | None:
        response = self.session.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()
        return self.handle_response(response)

    def handle_response(self, response: requests.Response) -> dict | None:
        """
        Handle a response from the Weather API.
        """
        if response.status_code != 200:
            logger.error("Error getting weather forecast -> Status Code: %s", response.status_code)
            return None

        try:
            return response.json()
        except json.JSONDecodeError as e:
            logger.exception('Error decoding JSON response -> Message: "%s"', e)
            return None

    def get_raw_forecast(self, latitude: float | Decimal, longitude: float | Decimal) -> list[dict] | None:
        """Return all 3-hourly forecast slots with a parsed 'date' field, unfiltered."""
        params = {
            "lat": float(latitude),
            "lon": float(longitude),
            "appid": self.api_key,
            "units": "imperial",
        }
        result = self.get(params)
        if result is None:
            return None
        return self._parse_dates(result.get("list", []))

    def _parse_dates(self, forecast: list[dict]) -> list[dict]:
        """Add a parsed 'date' datetime field to each forecast item in-place."""
        for item in forecast:
            dt_txt = item.get("dt_txt", "")
            if dt_txt:
                item["date"] = datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S")
        return forecast

    def filter_forecast(self, forecast: list[dict]) -> list[dict]:
        """Filter the forecast to only include morning and evening for each day."""
        self._parse_dates(forecast)
        return [item for item in forecast if item.get("dt_txt", "").endswith(("12:00:00", "21:00:00"))]
