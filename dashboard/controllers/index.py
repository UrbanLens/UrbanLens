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
*        Path:    /index.py                                                                                            *
*        Project: controllers                                                                                          *
*        Version: <<projectversion>>                                                                                   *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
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

class IndexController(ListController):
    template_name = "dashboard/pages/home/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['project_description'] = "This is a stylish and modern homepage for our project."
        context['hero_image_url'] = "/static/images/hero.jpg"
        return context
