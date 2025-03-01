"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    routexl.py                                                                                           *
*        Path:    /dashboard/services/routexl.py                                                                       *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import requests
from requests.auth import HTTPBasicAuth
from urbanlens.dashboard.services.gateway import Gateway

class RouteXLGateway(Gateway):
    """
    Gateway for the RouteXL API to optimize trip routes.
    """

    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.base_url = "https://api.routexl.com"

    def optimize_route(self, pins):
        """
        Optimize a route given a list of pins.

        Args:
            pins: A list of dicts with 'name', 'lat', and 'lng' keys.

        Returns:
            Optimized route.
        """
        url = f"{self.base_url}/tour"
        data = {"locations": pins}
        response = requests.post(url, json=data, auth=HTTPBasicAuth(self.username, self.password))
        response.raise_for_status()
        return response.json()

# Example usage
# gateway = RouteXLGateway('your_username', 'your_password')
# locations = [
#     {"name": "Location 1", "lat": 52.05429, "lng": 4.248618},
#     {"name": "Location 2", "lat": 52.076892, "lng": 4.26975}
# ]
# optimized_route = gateway.optimize_route(locations)
# print(optimized_route)
