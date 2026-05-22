"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    IndexController.py                                                                                   *
*        Path:    /dashboard/controllers/index.py                                                                      *
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

from djangofoundry.controllers import ListController

from django.shortcuts import redirect

from urbanlens.dashboard.models.profile import Profile


class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"
    model = Profile

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("map.view")
        return super().get(request, *args, **kwargs)
