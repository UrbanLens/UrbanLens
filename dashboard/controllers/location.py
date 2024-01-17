"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    location.py                                                                                          *
*        Path:    /dashboard/controllers/location.py                                                                   *
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
from datetime import datetime
import logging

from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from requests.exceptions import HTTPError
from icecream import ic
import csv
from io import StringIO

from rest_framework.viewsets import GenericViewSet
from rest_framework.decorators import action
from django.http import JsonResponse
from rest_framework.exceptions import ValidationError

from dashboard.models.locations.model import Location
from dashboard.services.smithsonian import SmithsonianGateway
from dashboard.services.google.search import GoogleCustomSearchGateway
from dashboard.services.google.maps import GoogleMapsGateway
from dashboard.forms.upload_csv import CSVUploadForm

logger = logging.getLogger(__name__)

class LocationController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the location page
    """
    def view(self, request, *args, **kwargs):
        """
        View the location page
        """
        location = Location.objects.get(id=kwargs['location_id'])

        return render(request, 'dashboard/pages/location/index.html', { 'location': location, 'google_maps_api_key': settings.GOOGLE_MAPS_API_KEY })

    def init_map(self, request, *args, **kwargs):
        map_data = self.get_map_data()

        # Preprocess data into strings
        for pin in map_data:
            if 'description' in pin and pin['description'] is None:
                pin['description'] = ''

            # Turn arrays into csv
            if 'tags' in pin and pin['tags']:
                pin['tags'] = ', '.join(pin['tags'])
            else:
                pin['tags'] = ''
            if 'categories' in pin and pin['categories']:
                pin['categories'] = ', '.join(pin['categories'])
            else:
                pin['categories'] = ''

            # Last visited = None => Never
            if not pin['last_visited'] or pin['last_visited'] == 'never':
                pin['last_visited'] = 'Never'
            else:
                try:
                    # Dates look like this: 2023-01-02T00:00:00+00:00
                    pin['last_visited'] = datetime.strptime(pin['last_visited'], '%Y-%m-%dT%H:%M:%S%z').strftime('%Y-%m-%d')
                except ValueError:
                    logger.warning('Unable to parse date: %s', pin['last_visited'])

            if pin['status']:
                pin['status'] = pin['status'].replace('_', ' ').capitalize()

        return render(request, 'dashboard/pages/map/data.html', {'map_data': map_data})

    def get_map_data(self):
        map_data = Location.objects.all()
        if not map_data:
            # Default map data
            map_data = [{'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Location', 'description': 'No pins saved yet.'}]
        else:
            map_data = [pin.to_json() for pin in map_data]

        return map_data

    def get_smithsonian_images(self, request, location_id, *args, **kwargs):
        """
        Returns the Smithsonian images for a location.
        """
        # Get the location
        try:
            location : Location = Location.objects.get(id=location_id)
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(settings.SMITHSONIAN_API_KEY)

        # Get historic images from the Smithsonian's API
        smithsonian_images = smithsonian_gateway.get_data(location.name)

        return render(request, 'dashboard/pages/location/smithsonian.html', {
            'images': smithsonian_images,
        })

    def web_search(self, request, location_id, *args, **kwargs):
        """
        Returns the web search results for a location.
        """
        # Get the location
        try:
            location : Location = Location.objects.get(id=location_id)
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the GoogleCustomSearchGateway with the API key
        try:
            google_gateway = GoogleCustomSearchGateway()

            # Get web search results from the Google Custom Search API
            query = [
                location.address_extended,
                [
                    location.address_basic,
                    location.city
                ],
                [
                    location.address_basic,
                    location.county
                ],
                [
                    location.address_basic,
                    location.state
                ],
                f'{location.latitude}, {location.longitude}'
            ]

            if location.name and location.address_basic != location.name:
                query.append([
                    location.name,
                    location.city
                ])

            place_name = location.place_name
            if place_name and place_name != location.address_basic and place_name != location.name:
                query.append(place_name)

            search_results = google_gateway.search(query)
        except HTTPError as e:
            logger.error('Unable to contact Google Search API. Is the API Key valid? Exception ---> %s', e)
            return HttpResponse("Unable to search. This is unlikely to be resolved by multiple requests.", status=500)

        return render(request, 'dashboard/pages/location/web_search.html', { 'search_results': search_results })

    def satellite_view_google_image(self, request, *args, **kwargs):
        """
        Returns the satellite view image for a location.
        """
        try:
            location = Location.objects.get(id=kwargs['location_id'])
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(settings.GOOGLE_MAPS_API_KEY)

        # Get the satellite view image from the Google Maps API
        satellite_image = google_maps_gateway.get_satellite_view(location.latitude, location.longitude)

        return HttpResponse(satellite_image, content_type="image/jpeg")

    def street_view(self, request, *args, **kwargs):
        """
        Returns the street view image for a location.
        """
        try:
            location = Location.objects.get(id=kwargs['location_id'])
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(settings.GOOGLE_MAPS_API_KEY)

        # Get the street view image from the Google Maps API
        street_view_image = google_maps_gateway.get_street_view(location.latitude, location.longitude)

        return HttpResponse(street_view_image, content_type="image/jpeg")

    @action(detail=True, methods=['get'])
    def import_csv(self, request, *args, **kwargs):
        """
        View the import CSV page
        """
        return render(request, 'dashboard/pages/location/import/csv.html', { 'form': CSVUploadForm() })

    def upload_csv(self, request, *args, **kwargs):
        """
        Upload a CSV file
        """
        try:
            form = CSVUploadForm(request.POST, request.FILES)
            if form.is_valid():
                csv_file = form.cleaned_data['file']

                # Read the file
                csv_file_contents = csv_file.read().decode('utf-8')

                from dashboard.services.google.maps import GoogleMapsGateway

                # Instantiate the GoogleMapsGateway with the API key
                google_maps_gateway = GoogleMapsGateway(settings.GOOGLE_MAPS_API_KEY)

                # Get the locations from the CSV file
                locations = google_maps_gateway.import_locations_from_csv(csv_file_contents, request.user.profile)

                return JsonResponse({'locations': [location.to_json() for location in locations]})
            else:
                return JsonResponse({'error': 'Invalid form'}, status=400)

        except ValidationError as e:
            return JsonResponse({'error': str(e)}, status=400)


    @action(detail=True, methods=['get'])
    def import_kml(self, request, *args, **kwargs):
        """
        View the import KML page
        """
        return render(request, 'dashboard/pages/location/import/kml.html')

    @action(detail=True, methods=['post'])
    def upload_kml(self, request, *args, **kwargs):
        """
        Upload a KML file
        """
        # Get the file from the request
        kml_file = request.FILES['kml_file']

        # Read the file
        kml_file_contents = kml_file.read().decode('utf-8')

        from dashboard.services.google.maps import GoogleMapsGateway

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(settings.GOOGLE_MAPS_API_KEY)

        # Get the locations from the KML file
        locations = google_maps_gateway.import_locations_from_kml(kml_file_contents, request.user.profile)

        return JsonResponse({'locations': locations})
