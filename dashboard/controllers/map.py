"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    MapController.py                                                                                     *
*        Path:    /dashboard/controllers/map.py                                                                        *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.1                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from datetime import datetime
import logging

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

from django.shortcuts import render
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from UrbanLens.settings.app import settings

from rest_framework.viewsets import GenericViewSet

from UrbanLens.dashboard.models.pin import Pin, PinQuerySet
from UrbanLens.dashboard.models.categories.model import Category
from UrbanLens.dashboard.models.images.model import Image
from UrbanLens.dashboard.models.tags.model import Tag
from UrbanLens.dashboard.forms.advanced_search import AdvancedSearchForm
from UrbanLens.dashboard.forms.search import SearchForm

from django.contrib.auth.mixins import LoginRequiredMixin

logger = logging.getLogger(__name__)

class MapController(LoginRequiredMixin, GenericViewSet):
    def view_map(self, request, *args, **kwargs):
        pins = Pin.objects.all()

        return render(request, 'dashboard/pages/map/index.html', {'pins': pins, 'openweathermap_api_key': settings.openweathermap_api_key})

    def edit_pin(self, request, pin_id, *args, **kwargs):
        pin : Pin = Pin.objects.get(id=pin_id)
        # Update the pin based on the form data
        pin.name = request.POST.get('name')
        pin.description = request.POST.get('description')
        pin.latitude = request.POST.get('latitude')
        pin.longitude = request.POST.get('longitude')
        tags = request.POST.get('tags').split(',')
        for tag_name in tags:
            tag, created = Tag.objects.get_or_create(name=tag_name)
            pin.tags.add(tag)
        icon = request.FILES.get('icon', None)
        if icon:
            pin.icon = icon
        pin.save()
        return HttpResponseRedirect(reverse('view_map'))

    def get_edit_pin(self, request, pin_id, *args, **kwargs):
        pin = Pin.objects.get(id=pin_id)
        # Render the edit form
        categories = Category.objects.all()
        return render(request, 'dashboard/edit_pin.html', {'pin': pin, 'categories': categories})

    def add_pin(self, request, *args, **kwargs):
        # Render the add form
        return render(request, 'dashboard/pages/map/add_pin.html')

    def post_add_pin(self, request, *args, **kwargs):
        logger.critical('Adding a new pin!')
        try:
            # Create a new pin based on the form data
            name = request.POST.get('name')
            latitude = request.POST.get('latitude')
            longitude = request.POST.get('longitude')
            address = request.POST.get('address', None)
            tags = request.POST.get('tags')
            if tags is not None:
                tags = tags.split(',')
            else:
                tags = []
            icon = request.POST.get('icon', None)
            logger.critical('ADDING PIN, icon is %s', icon)
            logger.critical('POST is %s', request.POST)

            if not latitude or not longitude:
                if not address:
                    return HttpResponse("Error: No address or lat/lon provided.", status=400)

                # Convert address into lat/lng
                (latitude, longitude) = get_pin_by_address(address)
                if not latitude or not longitude:
                    return HttpResponse("Error: Unable to convert address to lat/lng.", status=400)

            pin = Pin.objects.create(name=name, latitude=latitude, longitude=longitude, icon=icon, profile=request.user.profile)
            for tag_name in tags:
                tag, created = Tag.objects.get_or_create(name=tag_name)
                pin.tags.add(tag)
            pin.save()
            logger.critical('New pin created: %s', pin.name)
            logger.critical('Profile is %s', request.user.profile)
            return HttpResponse(status=200)
        except Exception as e:
            raise e from e
            return HttpResponse(f"Error: {str(e)}", status=400)

    def search_map(self, request, *args, **kwargs):
        search_form = SearchForm()
        return render(request, 'dashboard/pages/map/search.html', {'form': search_form})

    def search_map_post(self, request, *args, **kwargs):
        logger.info('Searching map...')
        search_form = SearchForm(request.POST)
        if search_form.is_valid():
            query = Pin.objects.filter(profile=request.user.profile).filter_by_criteria(search_form.cleaned_data)
            data = self.get_map_data(request, query)
            return render(request, 'dashboard/pages/map/data.html', {'pins': data})
        
        logger.error('Invalid search criteria: %s', search_form.errors)
        return HttpResponse(status=400, content='Invalid search criteria.')

    def upload_image(self, request, pin_id, *args, **kwargs):
        image = request.FILES.get('image')
        pin = Pin.objects.get(id=pin_id)
        Image.objects.create(image=image, pin=pin)
        return HttpResponse(status=200)

    def change_category(self, request, pin_id, *args, **kwargs):
        category_id = request.POST.get('category')
        pin = Pin.objects.get(id=pin_id)
        pin.change_category(category_id)
        return HttpResponseRedirect(reverse('view_map'))

    def post_advanced_search(self, request, *args, **kwargs):
        form = AdvancedSearchForm(request.POST)
        if form.is_valid():
            pins = Pin.objects.filter_by_criteria(form.cleaned_data)
            return render(request, 'dashboard/view_map.html', {'pins': pins})

    def get_advanced_search(self, request, *args, **kwargs):
        form = AdvancedSearchForm()
        return render(request, 'dashboard/advanced_search.html', {'form': form})

    def init_map(self, request, *args, **kwargs):
        map_data = self.get_map_data(request)

        return render(request, 'dashboard/pages/map/data.html', {'map_data': map_data})

    def get_map_data(self, request, query : PinQuerySet | None = None):
        if query is None:
            query = Pin.objects.all().filter(profile = request.user.profile)

        if not query:
            # Default map data
            map_data = [] #{'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Pin', 'description': 'No pins saved yet.'}]
        else:
            map_data = [pin.to_json() for pin in query]

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
            if 'last_visited' not in pin or not pin['last_visited'] or pin['last_visited'] == 'never':
                pin['last_visited'] = 'Never'
            else:
                try:
                    # Dates look like this: 2023-01-02T00:00:00+00:00
                    pin['last_visited'] = datetime.strptime(pin['last_visited'], '%Y-%m-%dT%H:%M:%S%z').strftime('%Y-%m-%d')
                except ValueError:
                    logger.warning('Unable to parse date: %s', pin['last_visited'])

            if 'status' in pin and pin['status']:
                pin['status'] = pin['status'].replace('_', ' ').capitalize()

        return map_data


@login_required
def get_pin_by_address(address):
    try:
        geolocator = Nominatim(user_agent="geoapiExercises")
        pin = geolocator.geocode(address)
        if pin:
            return (pin.latitude, pin.longitude)

    except GeocoderTimedOut:
        raise Exception("Geocoder service timed out.")
    except GeocoderUnavailable:
        raise Exception("Geocoder service unavailable.")
    return (None, None)
