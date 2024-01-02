"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    model.py                                                                                             *
*        Path:    /dashboard/models/categories/model.py                                                                *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2023 - 2024 Urban Lens                                                                          *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
from __future__ import annotations
from django.db.models import CharField
from dashboard.models import abstract
from dashboard.models.categories.queryset import Manager

class Category(abstract.Model):
    """
    Records category data.
    """
    name = CharField(max_length=255)
    icon = CharField(max_length=255, choices=[
        ('church', 'church'),
        ('factory', 'factory'),
        ('home', 'home'),
        ('hospital', 'hospital'),
        ('school', 'school'),
        ('warehouse', 'warehouse'),
        ('office_building', 'office_building'),
        ('shopping_mall', 'shopping_mall'),
        ('hotel', 'hotel'),
        ('stadium', 'stadium'),
    ])

    objects = Manager()

    class Meta(abstract.Model.Meta):
        db_table = 'dashboard_categories'
        get_latest_by = 'updated'
