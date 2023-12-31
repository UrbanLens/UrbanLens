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