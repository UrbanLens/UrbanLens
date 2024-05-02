"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    places.py                                                                                            *
*        Path:    /dashboard/services/google/places.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
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

class GooglePlacesGateway(Gateway):
    """
    Gateway for the Google Places API.
    """

    def __init__(self, api_key):
        self.api_key = api_key
        self.session = requests.Session()

    def get_data(self, latitude, longitude, radius=1000, place_type=None):
        """
        Fetch details about locations near the given coordinates from Google Places API.
        """
        base_url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
        params = {
            'location': f'{latitude},{longitude}',
            'radius': radius,
            'key': self.api_key
        }

        if place_type:
            params['type'] = place_type

        response = self.session.get(base_url, params=params)
        response.raise_for_status()

        places_data = response.json().get('results', [])
        return places_data

    def get_place_details(self, place_id, fields=None):
        details_url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'key': self.api_key
        }
        if fields:
            params['fields'] = ','.join(fields)

        response = self.session.get(details_url, params=params)
        response.raise_for_status()
        return response.json().get('result', {})

    def get_place_photos(self, photoreference, max_width=None):
        photo_url = 'https://maps.googleapis.com/maps/api/place/photo'
        params = {
            'photoreference': photoreference,
            'key': self.api_key
        }
        if max_width:
            params['maxwidth'] = max_width

        response = self.session.get(photo_url, params=params, stream=True)
        response.raise_for_status()
        return response.content  # Returns the raw bytes of the image.

    def get_recent_search_results(self, location_name):
        search_url = 'https://maps.googleapis.com/maps/api/place/findplacefromtext/json'
        params = {
            'input': location_name,
            'inputtype': 'textquery',
            'fields': 'formatted_address,name,rating,opening_hours,geometry',
            'key': self.api_key
        }

        response = self.session.get(search_url, params=params)
        response.raise_for_status()
        return response.json().get('candidates', [])

    def autocomplete(self, input_text):
        autocomplete_url = 'https://maps.googleapis.com/maps/api/place/autocomplete/json'
        params = {
            'input': input_text,
            'key': self.api_key
        }

        response = self.session.get(autocomplete_url, params=params)
        response.raise_for_status()
        return response.json().get('predictions', [])


'''
import fastkml
from django.contrib.gis.geos import Point
from .models import Location  # Import your Location model

def process_saved_places(file):
    kml = fastkml.kml.KML()
    kml.from_string(file.read())

    for feature in kml.features():
        if isinstance(feature, fastkml.Placemark):
            # Assuming the name and description are stored in the Placemark
            name = feature.name
            description = feature.description or ''
            # Extract coordinates (longitude, latitude, altitude)
            coords = feature.geometry.coords[0]
            longitude, latitude, _ = coords

            # Create and save the location to your database
            location = Location(
                name=name,
                description=description,
                latitude=latitude,
                longitude=longitude
                # Set other fields as necessary
            )
            location.save()

'''