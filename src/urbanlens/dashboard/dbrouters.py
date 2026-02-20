"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    dbrouters.py                                                                                         *
*        Path:    /dashboard/dbrouters.py                                                                              *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Generic imports
from __future__ import annotations

import logging

#
# Set up logging for this module. __name__ includes the namespace (e.g. dashboard.models.cases).
#
# We can adjust logging settings from the namespace down to the module level in UrbanLens/settings
#
logger = logging.getLogger(__name__)


class DBRouter:
    route_app_labels = ["dashboard"]

    def db_for_read(self, model, **hints):
        """Reading Model from default"""
        default = None
        if model._meta.app_label in self.route_app_labels:
            default = "default"
        return getattr(model, "_database", default)

    def db_for_write(self, model, **hints):
        """Writing Model to default"""
        default = None
        if model._meta.app_label in self.route_app_labels:
            default = "default"
        return getattr(model, "_database", default)

    def allow_relation(self, obj1, obj2, **hints):
        return True
