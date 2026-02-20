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
*        Path:    /dashboard/services/nps/map.py                                                                       *
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
import logging

import geopandas as gpd
import requests
from shapely.geometry import Point

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)


class NPSMapGateway(Gateway):
    def __init__(self):
        self.base_url = "https://mapservices.nps.gov/arcgis/rest/services/ParkBoundaries/FeatureServer/0/query"

    def check_coordinates_within_park(self, latitude: float, longitude: float) -> str | None:
        """
        Determines if a set of coordinates is within a national park and returns the park code.
        """
        # Construct the point from the given coordinates
        point = Point(longitude, latitude)

        # Query parameters for retrieving the boundary data in GeoJSON format
        params = {
            "where": "1=1",  # This condition effectively selects all features
            "outFields": "*",  # Select all fields
            "outSR": "4326",  # Spatial reference for WGS84
            "f": "geojson",  # Output format as GeoJSON
        }

        # Make the request to the GIS data service
        response = requests.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()

        # Load the GeoJSON into a GeoDataFrame
        park_boundaries = gpd.read_file(response.content)

        # Check if the point is within any of the park boundaries
        for _, park in park_boundaries.iterrows():
            if point.within(park["geometry"]):
                return park["parkCode"]

        # If no park contains the coordinates, return an empty string or None
        return None
