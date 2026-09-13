"""The import preview must not parse an upload in the web process.

The preview accepts archives, KML, GPX, shapefiles and Word documents, and each of those parsers is
decorated with ``untrusted_parse``. Under ``UL_UNTRUSTED_PARSE_POLICY=deny`` a web process that reaches
one raises, which is why the preview is what keeps that policy at ``warn`` (P2).

Each test here runs the request as the web process and the parse as the sandbox, both under
``deny``, so a parser reached from the wrong side raises rather than passing quietly.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.core import single_flight
from urbanlens.dashboard.services.core.rate_limiter import ServiceDisabledError
from urbanlens.dashboard.services.pins import import_preview
from urbanlens.dashboard.tasks import finish_import_preview_task

ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
NOTIFY = "urbanlens.dashboard.services.apis.locations.google.maps._notify_pin_import_parse_failure"
DOCUMENTS = "urbanlens.dashboard.services.ai.document_import"

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <Placemark>
    <name>Test Spot</name>
    <Point><coordinates>-73.9251,41.7003,0</coordinates></Point>
  </Placemark>
</Document>
</kml>
"""

#: One Takeout row only a lookup can place - a place URL carrying no coordinates - and one that needs none.
TAKEOUT_CSV = (
    "Title,Note,URL,Comment\n"
    "Somewhere,,https://www.google.com/maps/place/Somewhere+Nice,\n"
    'Elsewhere,,"https://www.google.com/maps/search/40.7128,-74.0060",\n'
)


class PreviewParsesOutsideTheWebProcessTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.addCleanup(single_flight.release, import_preview.guard_key(self.profile.pk))

    def _upload(self, name: str, data: bytes) -> dict:
        """Post as the web process under deny, with the parse queued rather than run."""
        upload = SimpleUploadedFile(name, data, content_type="application/octet-stream")
        with (
            override_settings(UL_PROCESS_ROLE="web", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()),
        ):
            response = self.client.post(reverse("pin.import.preview"), {"upload_files": [upload]})
        self.assertEqual(response.status_code, 202, response.content)
        return response.json()

    def _parse_in_sandbox(self, job: dict) -> mock.Mock:
        with (
            override_settings(UL_PROCESS_ROLE="sandbox", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue,
        ):
            import_preview.parse_import_preview(self.profile.pk, job["job_id"])
        return enqueue

    def _state(self, job: dict) -> dict:
        return self.client.get(job["status_url"]).json()

    def test_a_preview_request_in_the_web_process_parses_nothing(self) -> None:
        self._upload("Urbex Sites.kml", KML.encode())

    def test_the_sandbox_worker_reads_what_the_web_process_stored(self) -> None:
        job = self._upload("Urbex Sites.kml", KML.encode())

        enqueue = self._parse_in_sandbox(job)

        enqueue.assert_not_called()
        state = self._state(job)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual([entry["stem"] for entry in state["result"]["lists"]], ["Urbex Sites"])
        self.assertEqual(len(state["result"]["lists"][0]["pins"]), 1)
        self.assertIsNone(single_flight.holder(import_preview.guard_key(self.profile.pk)))

    def test_a_row_only_a_lookup_can_place_is_left_for_the_networked_half(self) -> None:
        job = self._upload("saved.csv", TAKEOUT_CSV.encode())

        with mock.patch.object(GoogleGeocodingGateway, "get_coordinates", side_effect=AssertionError("sandbox lookup")):
            enqueue = self._parse_in_sandbox(job)

        enqueue.assert_called_once_with(finish_import_preview_task, self.profile.pk, job["job_id"])
        self.assertEqual(self._state(job)["status"], "running")

        with (
            override_settings(UL_PROCESS_ROLE="worker", UL_UNTRUSTED_PARSE_POLICY="deny"),
            mock.patch.object(GoogleGeocodingGateway, "get_coordinates", return_value=(41.5, -73.5)) as lookup,
        ):
            import_preview.finish_import_preview(self.profile.pk, job["job_id"])

        lookup.assert_called_once()
        state = self._state(job)
        self.assertEqual(state["status"], "done", state)
        names = sorted(pin["name"] for entry in state["result"]["lists"] for pin in entry["pins"])
        self.assertEqual(names, ["Elsewhere", "Somewhere"])
        self.assertIsNone(single_flight.holder(import_preview.guard_key(self.profile.pk)))

    def test_a_lookup_service_that_is_unavailable_keeps_the_rest_of_the_preview(self) -> None:
        """A disabled or rate-limited geocoder is the ordinary case on a small deployment."""
        job = self._upload("saved.csv", TAKEOUT_CSV.encode())
        self._parse_in_sandbox(job)

        unavailable = ServiceDisabledError("google_geocoding")
        with mock.patch.object(GoogleGeocodingGateway, "get_coordinates", side_effect=unavailable):
            import_preview.finish_import_preview(self.profile.pk, job["job_id"])

        state = self._state(job)
        self.assertEqual(state["status"], "done", state)
        names = [pin["name"] for entry in state["result"]["lists"] for pin in entry["pins"]]
        self.assertEqual(names, ["Elsewhere"])
        self.assertTrue(state["result"]["warnings"], "the rows it could not place were dropped without a word")
        self.assertIsNone(single_flight.holder(import_preview.guard_key(self.profile.pk)))

    def test_a_file_that_fails_to_parse_is_reported_by_the_networked_half(self) -> None:
        """The admin notice sends mail, and the sandbox has no route out."""
        job = self._upload("broken.kml", KML.encode())

        with (
            mock.patch.object(GoogleMapsGateway, "takeout_kml_to_dict", side_effect=ValueError("unparseable")),
            mock.patch(NOTIFY) as sandbox_notify,
        ):
            enqueue = self._parse_in_sandbox(job)

        sandbox_notify.assert_not_called()
        enqueue.assert_called_once()
        with mock.patch(NOTIFY) as notify:
            import_preview.finish_import_preview(self.profile.pk, job["job_id"])

        notify.assert_called_once_with("kml")
        self.assertEqual(self._state(job)["status"], "error")

    def test_a_document_is_read_in_the_sandbox_and_extracted_by_the_networked_half(self) -> None:
        job = self._upload("notes.txt", b"The old mill on Route 9.")

        self._parse_in_sandbox(job)
        found = {
            "stem": "notes",
            "pins": [{"name": "Old mill", "lat": 41.0, "lng": -73.0, "description": "", "cid": None}],
        }
        with (
            mock.patch(f"{DOCUMENTS}.ai_document_import_available", return_value=True),
            mock.patch(f"{DOCUMENTS}.extract_pins_from_text", return_value=(found, None)) as extract,
        ):
            import_preview.finish_import_preview(self.profile.pk, job["job_id"])

        extract.assert_called_once_with("notes.txt", "The old mill on Route 9.", self.profile)
        state = self._state(job)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(state["result"]["lists"], [found])

    def test_a_second_upload_waits_for_the_first(self) -> None:
        self._upload("Urbex Sites.kml", KML.encode())

        upload = SimpleUploadedFile("Other.kml", KML.encode(), content_type="application/octet-stream")
        with mock.patch(ENQUEUE, return_value=mock.Mock()) as enqueue:
            response = self.client.post(reverse("pin.import.preview"), {"upload_files": [upload]})

        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()

    def test_someone_else_cannot_read_the_preview(self) -> None:
        job = self._upload("Urbex Sites.kml", KML.encode())
        self._parse_in_sandbox(job)

        self.client.force_login(baker.make(User))

        self.assertEqual(self.client.get(job["status_url"]).status_code, 404)
