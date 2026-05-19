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
from __future__ import annotations

from django.db.models import CharField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.categories.queryset import CategoryManager


class Category(abstract.Model):
    """
    Records category data.
    """
    name = CharField(max_length=255, unique=True)
    icon = CharField(max_length=255, choices=[
        ("church", "church"),
        ("factory", "factory"),
        ("home", "home"),
        ("hospital", "hospital"),
        ("school", "school"),
        ("warehouse", "warehouse"),
        ("office_building", "office_building"),
        ("shopping_mall", "shopping_mall"),
        ("hotel", "hotel"),
        ("stadium", "stadium"),
    ], null=True, blank=True)

    objects = CategoryManager()

    def __str__(self):
        return f"{self.name}"

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_categories"
        get_latest_by = "updated"
