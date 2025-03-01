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
*        Path:    /dashboard/models/pin/serializer.py                                                            *
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
from rest_framework import serializers
from urbanlens.dashboard.models.pin.model import Pin

class PinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Pin
        fields = ['name', 'icon', 'categories', 'last_visited', 'latitude', 'longitude', 'created', 'updated', 'profile', 'status', 'tags', 'rating']

    def create(self, validated_data):
        user = validated_data.pop('user')
        pin = Pin.objects.create(**validated_data)
        pin.user = user
        pin.save()
        return pin
