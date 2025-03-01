"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    __index__.py                                                                                         *
*        Path:    /dashboard/models/trips/__index__.py                                                                 *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2024-01-07                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-07     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from urbanlens.dashboard.models.trips.queryset import QuerySet, Manager
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.trips.viewset import TripViewSet
from urbanlens.dashboard.models.trips.serializer import TripSerializer