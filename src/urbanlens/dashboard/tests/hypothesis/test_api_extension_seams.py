"""A plugin can add a field to any external API serializer, and narrow or annotate the querysets the API lists."""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import User
from model_bakery import baker
from rest_framework import serializers

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers import PinListSerializer
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins.base import PluginContext, UrbanLensPlugin
from urbanlens.dashboard.plugins.drf import SerializerField
from urbanlens.dashboard.plugins.testing import plugin_active
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_LISTS = "/dashboard/api/external/v1/lists/"


def _only_named(queryset, serializer_class, request):
    if serializer_class is PinListSerializer and (name := request.query_params.get("named")):
        return queryset.filter(name=name)
    return queryset


class _ShoutedName(serializers.Field):
    def __init__(self) -> None:
        super().__init__(source="*", read_only=True)

    def to_representation(self, value: PinList) -> str:
        return value.name.upper()


class _ListsPlugin(UrbanLensPlugin):
    name: ClassVar[str] = "lists_extension_probe"

    def setup(self, plugin: PluginContext) -> None:
        from urbanlens.dashboard.plugins import points

        plugin.add(
            points.SERIALIZER_FIELDS,
            SerializerField(PinListSerializer, "shouted_name", _ShoutedName),
        )
        plugin.add(
            points.SERIALIZER_FIELDS,
            SerializerField(PinListSerializer, "name", lambda: serializers.CharField(default="replaced")),
        )
        plugin.filter(points.API_LIST_QUERYSETS, _only_named)


class ApiExtensionSeamTests(TestCase):
    def setUp(self) -> None:
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, raw_key = generate_api_key(self.user, "Lists client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.LISTS_READ.value])
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}
        PinList.objects.create(profile=self.profile, name="Mills")
        PinList.objects.create(profile=self.profile, name="Asylums")

    def test_a_plugin_field_is_added_and_a_core_field_is_not_replaced(self) -> None:
        with plugin_active(_ListsPlugin):
            rows = self.client.get(_LISTS, **self.auth).json()["results"]

        self.assertEqual({row["shouted_name"] for row in rows}, {"MILLS", "ASYLUMS"})
        self.assertEqual({row["name"] for row in rows}, {"Mills", "Asylums"})

    def test_a_plugin_narrows_a_list_by_its_own_parameter(self) -> None:
        with plugin_active(_ListsPlugin):
            response = self.client.get(_LISTS, {"named": "Mills"}, **self.auth).json()

        self.assertEqual(response["count"], 1)
        self.assertEqual([row["name"] for row in response["results"]], ["Mills"])

    def test_without_the_plugin_neither_applies(self) -> None:
        rows = self.client.get(_LISTS, {"named": "Mills"}, **self.auth).json()["results"]

        self.assertEqual(len(rows), 2)
        self.assertNotIn("shouted_name", rows[0])
