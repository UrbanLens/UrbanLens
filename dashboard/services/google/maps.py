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
from dashboard.services.gateway import Gateway

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
    
    def get_satellite_view(self, latitude, longitude, zoom=15, size='600x300', maptype='satellite'):
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

    def get_street_view(self, latitude, longitude, fov=90, pitch=0, size='600x300'):
        """
        Get a Street View image for the given latitude and longitude.
        """
        street_view_url = 'https://maps.googleapis.com/maps/api/streetview'
        params = {
            'location': f'{latitude},{longitude}',
            'fov': fov,
            'pitch': pitch,  # Up or down angle of the camera relative to the Street View vehicle
            'size': size,  
            'key': self.api_key
        }
        response = self.session.get(street_view_url, params=params)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image
