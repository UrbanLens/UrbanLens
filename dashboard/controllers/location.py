"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    location.py                                                                                        *
*        - Path:    /dashboard/controllers/location.py                                                                 *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-01-01                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@manlyphotos.com                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from datetime import datetime
import logging

from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from requests.exceptions import HTTPError

from rest_framework.viewsets import GenericViewSet
from rest_framework.decorators import action
from django.http import JsonResponse
from rest_framework.exceptions import ValidationError

from UrbanLens.settings.app import settings
from dashboard.models.locations.model import Location
from dashboard.services.smithsonian import SmithsonianGateway
from dashboard.services.google.search import GoogleCustomSearchGateway
from dashboard.services.google.maps import GoogleMapsGateway
from dashboard.forms.upload_datafile import UploadDataFile

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

        return render(request, 'dashboard/pages/location/index.html', { 'location': location, 'google_maps_api_key': settings.google_maps_api_key })
    
    def test_ai(self, request, *args, **kwargs):
        """
        Test the AI. TODO Temporary function that can be deleted at any time with no side effects.
        """
        from dashboard.services.ai.cloudflare import CloudflareGateway
        gateway = CloudflareGateway(instructions="""
            Look at the following information about a location and determine what category it belongs in. Available categories are:
            Church, School, Park, Police Station, Firehouse, Library, Hospital, Castle, House, Mansion, Factory, Mall, Power Plant, 
            Asylum, Prison, Stadium, Military Base, Airport, Train Station, Bank, Hotel, Resort, Amusement Park, Tunnel, Cave, Silo,
            Graveyard, Lighthouse, Bridge, Dam, Water Tower, Theater, Observatory, Laboratory, Ruins, Cars, Boats, Planes, Trains,
            Casino, Strip Club, Office, Fire Tower, Warehouse, Campground, Skyscraper, Funeral Home, Monument, Bunker, Store
            If the location does not fit into any of these categories, provide a new category that is broad enough to include a variety 
            of similar urbex locations. Do not answer with the name of the location; always answer with a category.
        """)
        response = gateway.send_prompt('address: 312 Western Ave, Guilderland, NY 12084, USA, name: Master Cleaners')

        return JsonResponse({'response': response})

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
        smithsonian_gateway = SmithsonianGateway(settings.smithsonian_api_key)

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
        google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

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
        google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

        # Get the street view image from the Google Maps API
        street_view_image = google_maps_gateway.get_street_view(location.latitude, location.longitude)

        return HttpResponse(street_view_image, content_type="image/jpeg")

    @action(detail=True, methods=['get'])
    def import_form(self, request, *args, **kwargs):
        """
        View the import CSV page
        """
        return render(request, 'dashboard/pages/location/import/csv.html', { 'form': UploadDataFile() })

    def upload_takeout(self, request, *args, **kwargs):
        """
        Upload a Google Takeout file
        """
        try:
            form = UploadDataFile(request.POST, request.FILES)
            if form.is_valid():
                datafile = form.cleaned_data['file']

                # Instantiate the GoogleMapsGateway with the API key
                google_maps_gateway = GoogleMapsGateway(settings.google_maps_api_key)

                # Get the file extension
                locations = google_maps_gateway.import_locations_from_file(datafile, request.user.profile)

                return JsonResponse({'locations': [location.to_json() for location in locations]})
            else:
                return JsonResponse({'error': 'Invalid form'}, status=400)

        except ValidationError as e:
            return JsonResponse({'error': str(e)}, status=400)

    def weather_forecast(self, request, location_id, *args, **kwargs):
        """
        Returns the weather forecast for a location.
        """
        # Get the location
        try:
            location : Location = Location.objects.get(id=location_id)
        except Location.DoesNotExist:
            return HttpResponse("Location does not exist", status=404)

        from dashboard.services.openweather.gateway import WeatherForecastGateway

        # Instantiate the WeatherForecastGateway with the API key
        weather_forecast_gateway = WeatherForecastGateway()

        # Get the weather forecast from the OpenWeather API
        weather_forecast = weather_forecast_gateway.get_weather_forecast(location.latitude, location.longitude)

        logger.debug('forecast_data: %s', weather_forecast)

        return render(request, 'dashboard/pages/location/weather.html', { 'forecast': weather_forecast })