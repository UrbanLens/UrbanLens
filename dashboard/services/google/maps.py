"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    maps.py                                                                                              *
*        Path:    /dashboard/services/google/maps.py                                                                   *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-17     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations
import json
from typing import Any
import requests
import logging

import csv
from fastkml import kml
from tqdm import tqdm

from dashboard.services.gateway import Gateway
from dashboard.models.locations import Location
from dashboard.models.profile import Profile
from dashboard.services.google.geocoding import GoogleGeocodingGateway

logger = logging.getLogger(__name__)

class GoogleMapsGateway(Gateway):
    """
    Gateway for the Google Maps API.
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()

    def get_directions(self, origin, destination, mode='driving'):
        """
        Get directions from origin to destination.
        """
        directions_url = 'https://maps.googleapis.com/maps/api/directions/json'
        params = {
            'origin': origin,
            'destination': destination,
            'mode': mode,
            'key': self.api_key
        }
        response = self.session.get(directions_url, params=params)
        response.raise_for_status()
        return response.json()

    def find_place(self, input_text : str, input_type : str = 'textquery'):
        """
        Find a place using the Google Maps Places API.
        """
        place_url = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'
        params = {
            'input': input_text,
            'inputtype': input_type,
            'key': self.api_key
        }
        response = self.session.get(place_url, params=params)
        response.raise_for_status()
        return response.json()

    def get_satellite_view(self, latitude, longitude, zoom=17, size='600x300', maptype='satellite'):
        """
        Get a satellite view image for the given latitude and longitude.
        """
        static_map_url = 'https://maps.googleapis.com/maps/api/staticmap'
        params = {
            'center': f'{latitude},{longitude}',
            'zoom': zoom,
            'size': size,
            'maptype': maptype,
            'key': self.api_key
        }
        response = self.session.get(static_map_url, params=params)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image

    def get_street_view(self, latitude, longitude, fov=90, pitch=0, size='600x300', radius=50, max_radius=1000, radius_increment=50):
        """
        Get the closest Street View image to the given latitude and longitude.
        """
        street_view_url = 'https://maps.googleapis.com/maps/api/streetview/metadata'
        logger.critical('Getting street view')

        while radius <= max_radius:
            params = {
                'location': f'{latitude},{longitude}',
                'fov': fov,
                'pitch': pitch,
                'size': size,
                'radius': radius,
                'key': self.api_key
            }

            # Checking for metadata first to avoid unnecessary data usage
            metadata_response = self.session.get(street_view_url, params=params)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()

            if metadata['status'] == 'OK':
                logger.critical('Found street view')
                # If image is available, get the image with the heading towards the original coordinates
                image_params = params.copy()
                image_params.pop('radius')
                image_params['heading'] = self.calculate_heading(metadata['location']['lat'], metadata['location']['lng'], latitude, longitude)
                street_view_url = 'https://maps.googleapis.com/maps/api/streetview'
                image_response = self.session.get(street_view_url, params=image_params)
                image_response.raise_for_status()
                return image_response.content  # Returns the raw bytes of the image

            radius += radius_increment
            logger.critical('Increasing radius to %s', radius)

        logger.critical('No street view found')
        raise ValueError("No Street View imagery found within the maximum search radius.")

    def calculate_heading(self, lat1, lng1, lat2, lng2):
        """
        Calculate the heading from the first coordinate (lat1, lng1) to the second coordinate (lat2, lng2).
        """
        import math
        lat1 = math.radians(lat1)
        lng1 = math.radians(lng1)
        lat2 = math.radians(lat2)
        lng2 = math.radians(lng2)
        dLng = lng2 - lng1
        x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dLng)
        y = math.sin(dLng) * math.cos(lat2)
        heading = math.degrees(math.atan2(y, x))
        return (heading + 360) % 360
    
    def import_locations_from_file(self, file, user_profile : 'Profile') -> list[Location]:
        """
        Imports locations from a file and bulk creates Location objects.
        """
        parts = file.name.split('.')
        if not parts or len(parts) < 2:
            raise ValueError("No file extension provided.")
        
        extension : str = parts[-1]
        file_contents = file.read().decode('utf-8')

        match extension.lower():
            case 'kml':
                data = self.takeout_kml_to_dict(file_contents, user_profile)
            case 'json':
                data = self.takeout_json_to_dict(file_contents, user_profile)
            case 'csv':
                data = self.takeout_csv_to_dict(file_contents, user_profile)
            case _:
                raise ValueError("Unsupported file format. Supported formats are KML, JSON, and CSV.")
            
        # Parse every location in data
        locations = []
        total = len(data)
        created = 0
        skipped = 0
        with tqdm(total=total, desc="Importing locations") as pbar:
            for location_data in data:
                try:
                    location = self._create_location(**location_data)
                    if location:
                        locations.append(location)
                        created += 1
                    else:
                        skipped += 1
                finally:
                    pbar.update(1)
                    pbar.set_description(f"Importing locations: {created} created, {skipped} skipped")

        return locations

    def takeout_kml_to_dict(self, file_contents : str, user_profile : 'Profile') -> dict[str, Any]:
        try:
            k = kml.KML()
            k.from_string(file_contents)
                
            locations = []
            for feature in k.features():
                for placemark in feature.features():
                    coords = placemark.geometry.coords[0]

                    locations.append({
                        'latitude': coords[1],
                        'longitude': coords[0],
                        'profile': user_profile,
                        'name': placemark.name,
                        'description': placemark.description
                    })

            logger.debug(f"Converted {len(locations)} locations from KML file to dicts.")
        except Exception as e:
            logger.error("Failed to import locations from KML: %s", str(e))
            raise

        return locations

    def takeout_json_to_dict(self, file_contents: str, user_profile: 'Profile') -> dict[str, Any]:
        try:
            json_data = json.loads(file_contents)
            features = json_data.get('features', [])
            locations = []

            for feature in features:
                geometry = feature.get('geometry', {})
                properties = feature.get('properties', {})

                # Ensure the geometry type is Point for extracting coordinates
                if geometry.get('type') == 'Point':
                    coordinates = geometry.get('coordinates', [])

                    # Coordinates are expected to be in [longitude, latitude] format
                    longitude, latitude = coordinates if len(coordinates) == 2 else (None, None)

                    locations.append({
                        'latitude': latitude,
                        'longitude': longitude,
                        'profile': user_profile,
                        'name': properties.get('name', 'Unknown Location'),
                        'description': f"{properties.get('description','')} {properties.get('address','')}"
                    })

            logger.info(f"Converted {len(locations)} locations from JSON file to dicts.")

        except Exception as e:
            logger.error("Failed to import locations from JSON: %s", str(e))
            raise

        return locations

    def takeout_csv_to_dict(self, file_contents: str, user_profile: 'Profile') -> dict[str, Any]:
        locations = []
        gateway = GoogleGeocodingGateway()
        try:
            reader = csv.DictReader(file_contents.splitlines())

            for row in reader:
                # Extract coordinates from URL if available
                url = row.get('URL', '')
                if not url:
                    logger.error('No url to extract coordinates from: row -> %s', row)
                    continue

                latitude, longitude = gateway.extract_coordinates_from_url(url)

                locations.append({
                    'latitude': latitude,
                    'longitude': longitude,
                    'profile': user_profile,
                    'name': row.get('Title', ''),
                    'description': row.get('Note', '') + " " + row.get('Comment', '').strip()
                })

            logger.info(f"Converted {len(locations)} locations from CSV file to dicts.")
        except Exception as e:
            logger.error("Failed to import locations from CSV: %s", str(e))
            raise

        return locations
    
    def _create_location(self, latitude, longitude, profile : 'Profile', **kwargs) -> Location | None:
        if not latitude or not longitude:
            logger.error('No coordinates provided for new location')
            return None

        location, created = Location.objects.get_nearby_or_create(
            latitude=latitude,
            longitude=longitude,
            profile=profile,
            defaults=kwargs
        )

        if not created:
            location.suggest_category(append_suggestion=True)
            location.save()

        return location