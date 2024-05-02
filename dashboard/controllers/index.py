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
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2023 Urban Lens                                                                                 *
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
from dashboard.models.profile import Profile

class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"
    model = Profile

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['project_description'] = "This is a stylish and modern homepage for our project."
        context['hero_image_url'] = "/static/images/hero.jpg"
        context['profile_picture_url'] = "/static/images/profile.jpg"  # TODO: Replace
        return context
