"""A failed task's ERROR line names its arguments without leaking a coordinate (P212)."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.redact import redact_call_arguments
from urbanlens.dashboard.tasks import fetch_recorded_weather_at
from urbanlens.UrbanLens.celery import log_task_failure

_LATITUDE = 41.7234567
_LONGITUDE = -73.9312345


def _refresh_pin(pin_id: int, pin_ids: list[int], *, force: bool = False) -> None:
    """A stand-in task body taking record ids."""


class TaskFailureLogTests(SimpleTestCase):
    def _failure_line(self, sender: object, args: tuple, kwargs: dict) -> str:
        with self.assertLogs("urbanlens.UrbanLens.celery", level="ERROR") as logs:
            log_task_failure(sender=sender, task_id="task-1", exception=OSError("timed out"), args=args, kwargs=kwargs)
        return logs.output[0]

    def test_the_weather_tasks_coordinates_never_reach_the_line(self) -> None:
        line = self._failure_line(fetch_recorded_weather_at, (_LATITUDE, _LONGITUDE), {"iso_days": ["2026-10-01"]})

        self.assertNotIn("41.72", line)
        self.assertNotIn("73.93", line)
        self.assertIn("fetch_recorded_weather_at", line)
        self.assertIn("task-1", line)
        self.assertIn("latitude", line)

    def test_a_task_that_fails_with_no_sender_still_logs_without_its_arguments(self) -> None:
        line = self._failure_line(None, (_LATITUDE, _LONGITUDE), {})

        self.assertNotIn("41.72", line)


class RedactCallArgumentsTests(SimpleTestCase):
    def test_arguments_are_named_by_the_signature(self) -> None:
        self.assertEqual(
            list(redact_call_arguments(fetch_recorded_weather_at.run, (_LATITUDE, _LONGITUDE, []), {})),
            ["latitude", "longitude", "iso_days"],
        )

    def test_record_ids_pass_through_so_a_failure_can_be_traced(self) -> None:
        redacted = redact_call_arguments(_refresh_pin, (12,), {"pin_ids": [3, 4], "force": True})

        self.assertEqual(redacted["pin_id"], 12)
        self.assertEqual(redacted["pin_ids"], [3, 4])

    def test_a_value_under_an_id_name_that_is_not_a_record_id_is_redacted(self) -> None:
        redacted = redact_call_arguments(_refresh_pin, ("41.7234567,-73.9312345", [1, "x"]), {})

        self.assertNotIn("41.72", str(redacted))
        self.assertNotEqual(redacted["pin_ids"], [1, "x"])

    def test_arguments_the_signature_rejects_are_still_redacted(self) -> None:
        redacted = redact_call_arguments(_refresh_pin, (1, 2, 3, _LATITUDE), {"nonsense": _LONGITUDE})

        self.assertNotIn("41.72", str(redacted))
        self.assertNotIn("73.93", str(redacted))
