"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    queryset.pyi                                                                                       *
*        - Path:    /dashboard/models/categories/queryset.pyi                                                          *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2023-12-24                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2023 - 2024 Urban Lens                                                                        *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from django.db import models

from urbanlens.dashboard.models.abstract import models

class CategoryQuerySet(models.QuerySet):
    ...

class CategoryManager(models.Manager, CategoryQuerySet):
    def all(self) -> CategoryQuerySet: ...
