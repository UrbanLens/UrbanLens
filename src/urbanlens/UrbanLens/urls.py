"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
URL configuration for urbanlens project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    urls.py                                                                                              *
*        Path:    /UrbanLens/urls.py                                                                                   *
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
*        2023-12-31     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
import logging

from urbanlens.dashboard.urls import urlpatterns as dashboard_urls
from urbanlens.dashboard.controllers.account import SignupView
from urbanlens.dashboard.controllers.index import IndexController
from urbanlens.dashboard.controllers.health import HealthController

logger = logging.getLogger(__name__)

admin.autodiscover()

urlpatterns = [
    path("admin/", admin.site.urls, name="admin"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("signup/", SignupView.as_view(), name="signup"),
    path("dashboard/", include(dashboard_urls), name="dashboard"),
    path("health/", HealthController.as_view({"get": "check"}), name="health"),
    path("", IndexController.as_view(), name="index"),

    # 404
	re_path('.*', TemplateView.as_view(template_name="dashboard/pages/errors/404.html"), name='404')
]