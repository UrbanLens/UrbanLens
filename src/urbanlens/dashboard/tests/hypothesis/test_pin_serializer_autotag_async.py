"""Creating a pin through the serializer must not block on an AI call.

``AutoTagService.suggest_for_pin`` runs the keyword stage and then, for any label
the keywords missed, calls the LLM gateway - a network round-trip. The two other
pin-creation paths (``services.pins.pin_creation`` and the Google Maps import)
already enqueue ``tasks.suggest_pin_category`` for exactly that reason;
``PinSerializer.create`` still called the service inline, so every pin created
through the REST API or an import that goes through this serializer waited on an
LLM before the response came back.

The tagging itself is best-effort either way - the old code swallowed its
exceptions - so nothing about the response contract depends on it having run.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.serializer import PinSerializer

_SUGGEST = "urbanlens.dashboard.services.labels.auto_tag.AutoTagService.suggest_for_pin"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class PinSerializerAutoTagTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)

    def _create(self) -> Pin:
        serializer = PinSerializer(data={"name": "Powerhouse"})
        serializer.is_valid(raise_exception=True)
        return serializer.save(profile=self.profile, location=self.location)

    def test_no_ai_call_happens_inside_the_request(self) -> None:
        with mock.patch(_SUGGEST) as suggest, mock.patch(_ENQUEUE):
            self._create()
        suggest.assert_not_called()

    def test_tagging_is_enqueued_for_the_new_pin(self) -> None:
        from urbanlens.dashboard.tasks import suggest_pin_category

        with mock.patch(_SUGGEST), mock.patch(_ENQUEUE) as enqueue:
            pin = self._create()

        enqueue.assert_called_once_with(suggest_pin_category, pin.pk)

    def test_the_pin_is_still_created_when_the_broker_is_down(self) -> None:
        """safely_enqueue_task returns None on an unreachable broker; the pin must survive."""
        with mock.patch(_SUGGEST), mock.patch(_ENQUEUE, return_value=None):
            pin = self._create()

        self.assertIsNotNone(pin.pk)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())

    def test_a_create_is_still_not_treated_as_an_explicit_rename(self) -> None:
        """The behaviour create() already had, which the change must not disturb."""
        with mock.patch(_SUGGEST), mock.patch(_ENQUEUE):
            pin = self._create()

        self.assertFalse(pin.name_is_user_provided)
