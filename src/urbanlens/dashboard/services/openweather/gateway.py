"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    gateway.py                                                                                           *
*        Path:    /dashboard/services/openweather/gateway.py                                                           *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-17                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-17     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from datetime import datetime
import json
import logging

import requests
from dataclasses import dataclass, field
from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class WeatherForecastGateway(Gateway):
    api_key: str = settings.openweathermap_api_key
    base_url: str = "http://api.openweathermap.org/data/2.5/forecast"

    def __post_init__(self):
        if not self.api_key:
            raise ValueError("OpenWeatherMap API key must be provided.")

    def get_weather_forecast(self, latitude: float, longitude: float) -> list[dict] | None:
        """
        Retrieve a weather forecast for the given coordinates.
        """
        if not latitude or not longitude:
            raise ValueError("Latitude and longitude must be provided to get weather forecast.")

        params = {
            "lat": latitude,
            "lon": longitude,
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
        response = requests.get(self.base_url, params=params, timeout=60)
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

    def filter_forecast(self, forecast: list[dict]) -> list[dict]:
        """
        Filter the forecast to only include morning and evening for each day.
        """
        filtered_forecast = []
        for forecast_item in forecast:
            date = forecast_item.get("dt_txt", "")
            # Parse the date into a date object
            forecast_item["date"] = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")

            if date.endswith(("12:00:00", "21:00:00")):
                filtered_forecast.append(forecast_item)
        return filtered_forecast
