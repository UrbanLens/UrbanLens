"""*****************************************************************************
 *                                                                             *
ASGI config for urbanlens project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
 * Metadata:                                                                   *
 *                                                                             *
 * 	File: asgi.py                                                              *
 * 	Project: personal                                                          *
 * 	Created: 28 Oct 2023                                                       *
 * 	Author: Jess Mann                                                          *
 * 	Email: jess@urbanlens.org                                                *
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

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "UrbanLens.settings")

application = get_asgi_application()
