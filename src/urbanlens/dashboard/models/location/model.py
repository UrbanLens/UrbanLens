"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/locations/model.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
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

# Generic imports
from __future__ import annotations
from typing import TYPE_CHECKING
import logging
# Django Imports
from django.db.models import Index
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models import PointField
# 3rd Party Imports
from django.db.models.fields import CharField, DecimalField
from django.db.models import ManyToManyField

# App Imports
from urbanlens.UrbanLens.settings.app import settings
from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.location.queryset import LocationManager
from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway

if TYPE_CHECKING:
    # Imports required for type checking, but not program execution.
    from urbanlens.dashboard.models.categories.model import Category

logger = logging.getLogger(__name__)

class Location(abstract.Model):
    """
    Records location data.
    """
    name = CharField(max_length=255)
    description = CharField(max_length=500, null=True, blank=True)
    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    point = PointField(geography=True, default=Point(0, 0))

    # Address
    street_number = CharField(max_length=50, null=True, blank=True)
    route = CharField(max_length=80, null=True, blank=True)
    locality = CharField(max_length=80, null=True, blank=True)
    administrative_area_level_1 = CharField(max_length=30, null=True, blank=True)
    administrative_area_level_2 = CharField(max_length=50, null=True, blank=True)
    administrative_area_level_3 = CharField(max_length=50, null=True, blank=True)
    country = CharField(max_length=20, default='United States')
    zipcode = CharField(max_length=10, null=True, blank=True)
    zipcode_suffix = CharField(max_length=10, null=True, blank=True)

    # Cached api data
    cached_place_name = CharField(max_length=255, null=True, blank=True)

    categories = ManyToManyField(
        'dashboard.Category',
        blank=True,
        default=list,
        related_name='locations'
    )
    tags = ManyToManyField(
        'dashboard.Tag',
        blank=True,
        default=list,
        related_name='locations'
    )

    objects = LocationManager()

    @property
    def place_name(self):
        """
        Returns the place name of the location.
        """
        if self.cached_place_name:
            return self.cached_place_name
        
        return self.get_place_name()

    @property
    def address(self) -> str | None:
        """
        Returns the address of the location.
        """
        # Do this, but skip over any attributes that are None
        #address = f"{self.street_number} {self.route}, {self.locality}, {self.administrative_area_level_1} {self.zipcode}"

        address = ''
        if self.street_number:
            address += f"{self.street_number} "
        if self.route:
            address += f"{self.route}, "
        if self.locality:
            address += f"{self.locality}, "
        if self.administrative_area_level_1:
            address += f"{self.administrative_area_level_1} "
        if self.zipcode:
            address += f"{self.zipcode}"

        return address or None
    
    @property
    def address_basic(self) -> str | None:
        """
        Returns the address of the location.
        """
        #address = f"{self.street_number} {self.route}"
        
        address = ''
        if self.street_number:
            address += f"{self.street_number} "
        if self.route:
            address += f"{self.route}"

        return address or None
    
    @property
    def address_extended(self) -> str | None:
        """
        Returns the address of the location.
        """
        #address = f"{self.street_number} {self.route}, {self.locality}"
        address = ''
        if self.street_number:
            address += f"{self.street_number} "
        if self.route:
            address += f"{self.route}, "
        if self.locality:
            address += f"{self.locality}"

        return address or None
    
    @property
    def state(self) -> str | None:
        """
        Returns the state of the location.
        """
        return self.administrative_area_level_1
    
    @state.setter
    def state(self, value : str):
        """
        Sets the state of the location.
        """
        self.administrative_area_level_1 = value
    
    @property
    def county(self) -> str | None:
        """
        Returns the county of the location.
        """
        return self.administrative_area_level_2
    
    @county.setter
    def county(self, value : str):
        """
        Sets the county of the location.
        """
        self.administrative_area_level_2 = value
    
    @property
    def city(self) -> str | None:
        """
        Returns the city of the location.
        """
        return self.locality
    
    @city.setter
    def city(self, value : str):
        """
        Sets the city of the location.
        """
        self.locality = value

    @property
    def rating(self) -> int:
        try:
            review = self.reviews.all().latest()
            if review:
                return review.rating
        except Exception:
            logger.debug('no rating found for location %s', self.id)

        return 0
    
    def get_place_name(self) -> str | None:
        result = GoogleGeocodingGateway(settings.google_maps_api_key).get_place_name(self.latitude, self.longitude)
        
        # We don't want to keep making requests to the API for results with no info, 
        # so cache a string instead of None
        if not result:
            result = 'No Information Available'

        if not self.cached_place_name:
            self.cached_place_name = result
            self.save()
        return result
    
    def has_place_name(self) -> bool:
        if not self.place_name:
            return False
        
        if self.place_name == 'No Information Available':
            return False
        
        return True

    def change_category(self, category_id : int) -> None:
        from urbanlens.dashboard.models.categories.model import Category
        category = Category.objects.get(id=category_id)
        self.categories.clear()
        self.categories.add(category)
        self.save()

    def suggest_category(self, append_suggestion : bool = False) -> str | None:
        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway
        instructions = "" +\
            "Look at the following information about a location and determine what category it belongs in. Example categories are:" +\
            "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, Firehouse, Fire Tower, " +\
            "Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, Library, Lighthouse, Mall, Mansion, Military Base, " +\
            "Monument, Police Station, Power Plant, Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel" +\
            "If the location does not fit into any of these categories, provide a new category that is broad enough to include a variety " +\
            "of similar urbex locations. Do not answer with the name of the location; always answer with a category, like this: <ANSWER>Factory</ANSWER>."

        prompt = ''
        if self.address:
            prompt += f"address: {self.address}\n"
        if self.has_place_name():
            prompt += f"google maps description: {self.place_name}\n"
            instructions += "\nThe google maps description may be helpful, but it also may be inaccurate. Use your best judgement.\n"
        if self.name:
            prompt += f"location title: {self.name}\n"
        if self.description:
            prompt += f"user notes: {self.description}\n"

        if not prompt:
            return None

        gateway = CloudflareGateway(instructions=instructions)

        category_name = gateway.send_prompt(prompt)
        if not category_name:
            return None
        
        if len(category_name) < 3:
            logger.debug('category too short: %s', category_name)
            return None
        
        if append_suggestion:
            self.add_category(category_name, save=False)
        
        return category_name
    
    def add_category(self, category_name : str, save : bool = True) -> 'Category' | None:
        from urbanlens.dashboard.models.categories.model import Category
        category_name = category_name.lower()
        try:
            category, _created = Category.objects.get_or_create(name=category_name)
            if category:
                self.categories.add(category)
                if save:
                    self.save()
                return category
            
        except Exception as e:
            logger.error('failed to add category %s to location -> %s', category_name, e)
        
        return None

    def __str__(self):
        return f"""
            Name: {self.name}
            Description: {self.description or ''}
            Google Place Name: {self.place_name}
        """

    def to_json(self):
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            'id': self.id,
            'name': self.name,
            'place_name': self.place_name,
            'description': self.description,
            'address': self.address,
            'city': self.city,
            'state': self.state,
            'country': self.country,
            'latitude': float(self.latitude),
            'longitude': float(self.longitude),
        }
    
    def save(self, *args, **kwargs):
        # update the location field accordingly for distance calculations in postgis
        if self.latitude is not None and self.longitude is not None:
            self.point = Point(float(self.longitude), float(self.latitude), srid=4326)

        super().save(*args, **kwargs)

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_locations'
        get_latest_by = 'updated'

        indexes = [
            Index(fields=['latitude', 'longitude']),
            Index(fields=['name']),
        ]

        unique_together = [
            ['latitude', 'longitude']
        ]