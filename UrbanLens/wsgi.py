"""*****************************************************************************
 *                                                                             *
WSGI config for urbanlens project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
 * Metadata:                                                                   *
 *                                                                             *
 * 	File: wsgi.py                                                              *
 * 	Project: personal                                                          *
 * 	Created: 28 Oct 2023                                                       *
 * 	Author: Jess Mann                                                          *
 * 	Email: jess@manlyphotos.com                                                *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 * 	Last Modified: Sat Oct 28 2023                                             *
 * 	Modified By: Jess Mann                                                     *
 *                                                                             *
 * 	-----                                                                      *
 *                                                                             *
 * 	Copyright (c) 2023 Urban Lens                                              *
 ****************************************************************************"""


import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "UrbanLens.settings")

application = get_wsgi_application()
