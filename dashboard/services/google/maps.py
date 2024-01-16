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
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
import requests
import logging
from dashboard.services.gateway import Gateway

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

    def find_place(self, input_text, input_type='textquery'):
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

    @staticmethod
    def calculate_heading(lat1, lng1, lat2, lng2):
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