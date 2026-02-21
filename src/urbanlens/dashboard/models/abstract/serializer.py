"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    serializer.py                                                                                        *
*        Path:    /dashboard/models/abstract/serializer.py                                                             *
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

from rest_framework import serializers


class Serializer(serializers.Serializer):
    id = serializers.ReadOnlyField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Dynamically include or exclude fields based on context
        context = kwargs.get("context", {})
        exclude_fields = context.get("exclude_fields", None)
        include_fields = context.get("include_fields", None)

        fields = self.fields
        for field in list(fields.keys()):
            if (exclude_fields and field in exclude_fields) or (include_fields and field not in include_fields):
                fields.pop(field)

    @classmethod
    def get_fieldnames(cls) -> list:
        return cls.Meta.fields

    @classmethod
    def get_native_fields(cls) -> list:
        """
        Get fields that are native to this model, (i.e. normal fields), not generated or calculated properties.

        Returns:
                list: A truncated list of cls.get_fieldnames()

        """
        fields = cls.get_fieldnames()
        for field in cls.Meta.generated_fields:
            if field in fields:
                fields.remove(field)
        return fields

    class Meta:
        fields = [
            "id",
        ]
        generated_fields = []
