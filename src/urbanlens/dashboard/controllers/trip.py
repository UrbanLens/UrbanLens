"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    trip.py                                                                                            *
*        - Path:    /dashboard/controllers/trip.py                                                                     *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-01-07                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.models.trips import Trip

logger = logging.getLogger(__name__)


class TripController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the trip planning page
    """

    def view(self, request: HttpRequest, trip_id: int) -> HttpResponse:
        """
        View the trip page
        """
        trip: Trip = Trip.objects.get(id=trip_id)
        return render(request, "dashboard/pages/trip/index.html", {"trip": trip})

    def get_trip_data(self, trip_id: int) -> dict[str, Any] | Any:
        """
        Fetch trip data.
        """
        trip: Trip = Trip.objects.get(id=trip_id)
        return trip.to_json()

    def get_trip_users(self, request: HttpRequest, trip_id: int) -> HttpResponse:
        """
        Fetch users associated with a trip.
        """
        trip: Trip = Trip.objects.get(id=trip_id)
        users = [user.to_json() for user in trip.users.all()]
        return HttpResponse(users, status=200)

    def get_trip_pins(self, request: HttpRequest, trip_id: int) -> HttpResponse:
        """
        Fetch pins associated with a trip.
        """
        trip: Trip = Trip.objects.get(id=trip_id)
        pins = [pin.to_json() for pin in trip.pins.all()]
        return HttpResponse(pins, status=200)

    def plan_trip(self, request: HttpRequest) -> HttpResponse:
        """
        Plan a trip itinerary using multiple pins and multiple users.
        """
        # TODO: Implement the logic for planning a trip itinerary.
        # This may involve fetching pins and users from the database,
        # performing some calculations or operations, and then returning
        # the result in the appropriate format (e.g., as an HttpResponse
        # or a render() call with a template and context).

        return HttpResponse("Trip planning not yet implemented", status=501)
