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
import requests
import logging
from urbanlens.UrbanLens.settings.app import settings
from urbanlens.dashboard.services.gateway import Gateway
import json

logger = logging.getLogger(__name__)

class WeatherForecastGateway(Gateway):
    def __init__(self, api_key : str | None = None):
        if not api_key:
            api_key = settings.openweathermap_api_key
        self.api_key = api_key
        self.base_url = "http://api.openweathermap.org/data/2.5/forecast"

    def get_weather_forecast(self, latitude: float, longitude: float) -> dict | None:
        """
        Retrieve a weather forecast for the given coordinates.
        """
        if not latitude or not longitude:
            raise ValueError('Latitude and longitude must be provided to get weather forecast.')

        params = {
            "lat": latitude,
            "lon": longitude,
            #"cnt": 7, 
            "appid": self.api_key,
            "units": "imperial"
        }

        result = self.get(params)

        # OpenWeatherMap returns a 4-hour forecast. We only want morning and evening for each day.
        filtered = self.filter_forecast(result.get('list', []))

        return filtered

    def get(self, params: dict) -> dict | None:
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        return self.handle_response(response)

    def handle_response(self, response: requests.Response) -> dict | None:
        """
        Handle a response from the Weather API.
        """
        if response.status_code != 200:
            logger.error('Error getting weather forecast -> Status Code: %s', response.status_code)
            return None

        try:
            return response.json()
        except json.JSONDecodeError as e:
            logger.error('Error decoding JSON response -> Message: "%s"', e)
            return None

    def filter_forecast(self, forecast: list[dict]) -> list[dict]:
        """
        Filter the forecast to only include morning and evening for each day.
        """
        filtered_forecast = []
        for forecast_item in forecast:
            date = forecast_item.get('dt_txt', '')
            # Parse the date into a date object
            forecast_item['date'] = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')

            if date.endswith('12:00:00'):
                filtered_forecast.append(forecast_item)
            elif date.endswith('21:00:00'):
                filtered_forecast.append(forecast_item)
        return filtered_forecast