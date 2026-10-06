"""JSON nested past the interpreter's recursion limit is a 400 wherever a client can send it, and changes nothing (P195).

``json.loads`` raises ``RecursionError`` on it, which is not a ``ValueError``, so an ``except ValueError`` around the
decode let it through as a 500. Each route here is reached as its owner, so the request gets as far as the decode.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import json
from pathlib import Path
import pkgutil
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from rest_framework import serializers
from webauthn.helpers import bytes_to_base64url

from urbanlens.core.tests.features import grant_alpha_features
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
from urbanlens.dashboard.controllers.csp_report import MAX_REPORT_BYTES
from urbanlens.dashboard.external_api.fields import JSONField
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.forms.settings_form import HotkeySettingsForm
from urbanlens.dashboard.models.account import WebAuthnCredential
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.custom_fields.model import CustomField
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoMetadataConflict, PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.relevance import MediaRelevance
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinMessage
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.spotguessr.model import GameSession
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.ai.dismissals import parse_dismissals_json
from urbanlens.dashboard.services.auth import webauthn as webauthn_service
from urbanlens.dashboard.services.auth.two_factor import SESSION_WEBAUTHN_PENDING_USER
from urbanlens.dashboard.services.core import request_body
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import DEEPLY_NESTED_JSON, ExternalApiRouteCase

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse

_DASHBOARD = Path(__file__).resolve().parents[2]

_PASSWORD = "correct horse battery staple"  # noqa: S105

#: Deep enough to exceed the recursion limit, small enough for the CSP endpoint's and the chat socket's size caps.
_NESTED_UNDER_32K = "[" * 16_000 + "]" * 16_000


class _OwnerCase(TestCase):
    """A signed-in owner whose routes the deep body is sent to."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def post_nested(self, url: str) -> TestResponse:
        """POST the deep body to *url* as ``application/json``."""
        return self.client.post(url, data=DEEPLY_NESTED_JSON, content_type="application/json")

    def assert_bad_request(self, response: TestResponse) -> None:
        self.assertEqual(response.status_code, 400, response.content[:200])


class TheBodyIsDeepEnoughTests(SimpleTestCase):
    """Every other test here is vacuous unless the bodies really do overflow the decoder."""

    def test_the_bodies_raise_recursion_error(self) -> None:
        for body in (DEEPLY_NESTED_JSON, _NESTED_UNDER_32K):
            with self.subTest(length=len(body)), self.assertRaises(RecursionError):
                json.loads(body)

    def test_the_small_body_fits_the_caps_it_is_sized_for(self) -> None:
        self.assertLessEqual(len(_NESTED_UNDER_32K), MAX_REPORT_BYTES)
        self.assertLessEqual(len(_NESTED_UNDER_32K), SafetyCheckinChatConsumer.max_frame_chars or 0)


class DecodeJsonTests(SimpleTestCase):
    def test_a_deep_value_is_a_malformed_body(self) -> None:
        with self.assertRaises(request_body.MalformedBodyError):
            request_body.decode_json(DEEPLY_NESTED_JSON)

    def test_any_shape_decodes(self) -> None:
        for raw, expected in (("[1]", [1]), ("null", None), ('"a"', "a"), (b'{"a": 1}', {"a": 1})):
            with self.subTest(raw=raw):
                self.assertEqual(request_body.decode_json(raw), expected)


class LabelBulkRouteTests(_OwnerCase):
    def setUp(self) -> None:
        super().setUp()
        self.labels = [baker.make(Label, profile=self.profile, kind="tag", name=f"Zz {n}", order=n) for n in (1, 2)]

    def _rows(self) -> list[tuple[Any, ...]]:
        return list(
            Label.objects.filter(profile=self.profile).order_by("pk").values_list("pk", "name", "kind", "order")
        )

    def test_every_bulk_label_route_refuses_it(self) -> None:
        before = self._rows()
        for name in (
            "label.reorder",
            "label.bulk_delete",
            "label.bulk_edit",
            "label.bulk_convert",
            "label.multi_merge",
        ):
            with self.subTest(route=name):
                self.assert_bad_request(self.post_nested(reverse(name, kwargs={"label_kind": "tag"})))
        self.assertEqual(self._rows(), before)


class VaultPhotoRouteTests(_OwnerCase):
    def test_recording_an_upload_failure_refuses_it(self) -> None:
        self.assert_bad_request(self.post_nested(reverse("vault.photos.failures")))
        self.assertFalse(PhotoUploadFailure.objects.exists())

    def test_resolving_a_metadata_conflict_refuses_it(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        existing, new = (baker.make(Image, profile=self.profile, pin=pin, caption=c) for c in ("first", "second"))
        conflict = baker.make(
            PhotoMetadataConflict,
            profile=self.profile,
            existing_image=existing,
            new_image=new,
            fields={"caption": ["first", "second"]},
        )

        self.assert_bad_request(self.post_nested(reverse("vault.photos.conflicts.resolve", args=[conflict.pk])))
        conflict.refresh_from_db()
        self.assertEqual(conflict.status, PhotoIssueStatus.PENDING)
        existing.refresh_from_db()
        self.assertEqual(existing.caption, "first")


class AccountRouteTests(_OwnerCase):
    def test_the_password_policy_check_refuses_it(self) -> None:
        self.client.logout()
        self.assert_bad_request(self.post_nested(reverse("validate_password_policy")))

    def test_changing_the_password_refuses_it(self) -> None:
        self.user.set_password(_PASSWORD)
        self.user.save(update_fields=["password"])
        self.client.force_login(self.user)

        self.assert_bad_request(self.post_nested(reverse("e2ee.change_password")))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_PASSWORD))

    def test_registering_a_passkey_refuses_it(self) -> None:
        session = self.client.session
        session[webauthn_service.SESSION_REGISTRATION_CHALLENGE] = "Y2hhbGxlbmdl"
        session.save()

        response = self.client.post(reverse("settings.security.passkeys.register"), {"credential": DEEPLY_NESTED_JSON})

        self.assert_bad_request(response)
        self.assertFalse(WebAuthnCredential.objects.exists())

    def test_completing_a_passkey_sign_in_refuses_it(self) -> None:
        self.client.logout()
        baker.make(WebAuthnCredential, user=self.user)
        session = self.client.session
        session[SESSION_WEBAUTHN_PENDING_USER] = self.user.pk
        session[webauthn_service.SESSION_AUTHENTICATION_CHALLENGE] = "Y2hhbGxlbmdl"
        session.save()

        self.assert_bad_request(self.post_nested(reverse("login.2fa.verify")))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_passkey_sign_in_missing_its_assertion_is_refused(self) -> None:
        """A registered ``rawId`` gets past the lookup, so the library's own field checks are what refuse it."""
        self.client.logout()
        credential = baker.make(WebAuthnCredential, user=self.user, credential_id=b"\x01\x02\x03\x04")
        raw_id = bytes_to_base64url(bytes(credential.credential_id))
        session = self.client.session
        session[SESSION_WEBAUTHN_PENDING_USER] = self.user.pk
        session[webauthn_service.SESSION_AUTHENTICATION_CHALLENGE] = "Y2hhbGxlbmdl"
        session.save()

        response = self.client.post(
            reverse("login.2fa.verify"),
            data=json.dumps({"id": raw_id, "rawId": raw_id, "response": {}, "type": "public-key"}),
            content_type="application/json",
        )

        self.assert_bad_request(response)
        self.assertNotIn("_auth_user_id", self.client.session)


class CspReportRouteTests(SimpleTestCase):
    def test_a_report_refuses_it(self) -> None:
        response = self.client.post(
            reverse("csp.report"), data=_NESTED_UNDER_32K, content_type="application/reports+json"
        )

        self.assertEqual(response.status_code, 400)


class WikiRouteTests(_OwnerCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="40.000000", longitude="-74.000000")
        self.wiki = baker.make(Wiki, location=self.location, name="Mill")
        baker.make(Pin, profile=self.profile, location=self.location)

    def test_drawing_the_boundary_refuses_it(self) -> None:
        self.assert_bad_request(self.post_nested(reverse("location.wiki.boundary", args=[self.location.slug])))
        self.assertFalse(WikiEdit.objects.filter(wiki=self.wiki).exists())
        self.assertFalse(Boundary.objects.filter(wiki=self.wiki).exists())

    def test_voting_on_media_refuses_it(self) -> None:
        self.assert_bad_request(self.post_nested(reverse("location.wiki.media.vote", args=[self.location.slug])))
        self.assertFalse(MediaRelevance.objects.exists())


class PinScopedRouteTests(_OwnerCase):
    def setUp(self) -> None:
        super().setUp()
        place = baker.make(Place, kind=PlaceKind.BUILDING, parent=baker.make(Place, kind=PlaceKind.PARCEL))
        location = baker.make(Location, latitude=41.733, longitude=-73.928, place=place)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

    def test_positioning_a_custom_field_refuses_it(self) -> None:
        field = CustomField.objects.create(profile=self.profile, entity_type="pin", name="Fixed", config={"a": 1})

        self.assert_bad_request(self.post_nested(reverse("custom_fields.position", args=[field.id])))
        field.refresh_from_db()
        self.assertEqual(field.config, {"a": 1})

    def test_saving_a_floorplan_refuses_it(self) -> None:
        self.assert_bad_request(self.post_nested(reverse("pin.floorplan.save", args=[self.pin.slug])))
        self.assertFalse(Floorplan.objects.exists())

    def test_moving_an_overlay_refuses_it(self) -> None:
        corners = [[40.01, -74.01], [40.01, -74.0], [40.0, -74.0], [40.0, -74.01]]
        overlay = MapImageOverlay(parent_pin=self.pin, profile=self.profile, tile_url_template="/t/{z}/{x}/{y}.png")
        overlay.set_corners(corners)
        overlay.save()

        response = self.client.post(
            reverse("pin.overlays.corners", args=[self.pin.slug, overlay.uuid]), {"corners": DEEPLY_NESTED_JSON}
        )

        self.assert_bad_request(response)
        overlay.refresh_from_db()
        self.assertEqual(overlay.corners(), corners)

    def test_a_visits_map_is_dropped_and_the_visit_kept(self) -> None:
        """A malformed ``map_data`` field is ignored rather than refused, as an undecodable one already was."""
        response = self.client.post(
            reverse("pin.visits", args=[self.pin.slug]), {"visited_date": "2024-05-01", "map_data": DEEPLY_NESTED_JSON}
        )

        self.assertLess(response.status_code, 500)
        self.assertFalse(MarkupMap.objects.filter(profile=self.profile).exists())


class MemoriesRouteTests(_OwnerCase):
    def test_bulk_logging_visits_refuses_it(self) -> None:
        location = baker.make(Location, latitude=42.1, longitude=-73.1)
        baker.make(Pin, profile=self.profile, location=location, last_visited=timezone.now())

        self.assert_bad_request(self.post_nested(reverse("memories.visits.bulk", args=["log"])))
        self.assertFalse(PinVisit.objects.exists())

    def test_bulk_accepting_suggestions_refuses_it(self) -> None:
        suggestion = PinSuggestion.objects.create(
            profile=self.profile,
            pin=None,
            latitude=42.2,
            longitude=-73.2,
            origin=PinSuggestionOrigin.LOCAL_SCAN,
            visit_dates=["2024-01-01"],
        )

        self.assert_bad_request(self.post_nested(reverse("memories.locations.bulk", args=["accept"])))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, PinSuggestionStatus.PENDING)

    def test_uploading_a_photo_scan_refuses_it(self) -> None:
        self.assert_bad_request(self.post_nested(reverse("tools.photo_scan.upload")))
        self.assertFalse(PinSuggestion.objects.exists())


class SafetyRouteTests(_OwnerCase):
    def test_repositioning_a_check_in_photo_refuses_it(self) -> None:
        checkin = baker.make(
            SafetyCheckin,
            profile=self.profile,
            title="Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        image = baker.make(Image, profile=self.profile, safety_checkin=checkin, latitude=1, longitude=1)

        self.assert_bad_request(
            self.post_nested(reverse("safety.checkin.gallery.image", args=[checkin.slug, image.pk]))
        )
        image.refresh_from_db()
        self.assertEqual((image.latitude, image.longitude), (1, 1))


class SpotGuessrRouteTests(_OwnerCase):
    def setUp(self) -> None:
        super().setUp()
        grant_alpha_features(self.user)

    def test_starting_a_game_refuses_its_bounds(self) -> None:
        response = self.client.post(
            reverse("spotguessr.start"), {"total_rounds": "1", "geo_bounds": DEEPLY_NESTED_JSON}
        )

        self.assert_bad_request(response)
        self.assertFalse(GameSession.objects.exists())

    def test_counting_pins_in_an_area_refuses_its_bounds(self) -> None:
        self.assert_bad_request(
            self.client.get(reverse("spotguessr.area_pin_count"), {"geo_bounds": DEEPLY_NESTED_JSON})
        )


class _ExternalFormCase(ExternalApiRouteCase):
    def bearer(self) -> str:
        return self.auth["HTTP_AUTHORIZATION"]


class ExternalGamesRouteTests(_ExternalFormCase):
    scopes = (ApiKeyScope.GAMES_READ, ApiKeyScope.GAMES_WRITE)
    read_scopes = (ApiKeyScope.GAMES_READ,)

    def test_counting_eligible_pins_refuses_its_bounds(self) -> None:
        response = self.client.get(
            reverse("external_api:games.spotguessr.eligible-count"),
            {"geo_bounds": DEEPLY_NESTED_JSON},
            HTTP_AUTHORIZATION=self.bearer(),
        )

        self.assertEqual(response.status_code, 400)

    def test_a_form_posted_session_refuses_its_bounds(self) -> None:
        response = self.client.post(
            reverse("external_api:games.spotguessr.sessions"),
            {"geo_bounds": DEEPLY_NESTED_JSON},
            HTTP_AUTHORIZATION=self.bearer(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GameSession.objects.exists())


class ExternalListsAndFiltersRouteTests(_ExternalFormCase):
    """A form post hands DRF's ``JSONField`` the raw text to decode itself."""

    scopes = (ApiKeyScope.LISTS_READ, ApiKeyScope.LISTS_WRITE)
    read_scopes = (ApiKeyScope.LISTS_READ,)

    def test_a_form_posted_saved_filter_refuses_its_criteria(self) -> None:
        before = list(SavedFilter.objects.filter(profile=self.owner).values_list("pk", flat=True))

        response = self.client.post(
            reverse("external_api:saved_filters"),
            {"name": "Mills", "criteria": DEEPLY_NESTED_JSON},
            HTTP_AUTHORIZATION=self.bearer(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(SavedFilter.objects.filter(profile=self.owner).values_list("pk", flat=True)), before)
        self.assertFalse(SavedFilter.objects.filter(name="Mills").exists())

    def test_a_form_posted_list_refuses_its_smart_fields(self) -> None:
        for field in ("smart_filter", "smart_boundary"):
            with self.subTest(field=field):
                response = self.client.post(
                    reverse("external_api:lists"),
                    {"name": "Mills", "is_smart": "true", field: DEEPLY_NESTED_JSON},
                    HTTP_AUTHORIZATION=self.bearer(),
                )

                self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.filter(profile=self.owner, name="Mills").exists())

    def test_every_writable_json_field_is_the_guarded_one(self) -> None:
        """A writable stock ``serializers.JSONField`` decodes form-posted text with nothing catching ``RecursionError``."""
        import urbanlens.dashboard

        for module in pkgutil.walk_packages(urbanlens.dashboard.__path__, "urbanlens.dashboard."):
            if "serializer" in module.name and ".tests." not in module.name:
                importlib.import_module(module.name)
        unguarded = []
        seen = 0
        pending: list[type[serializers.BaseSerializer]] = [serializers.BaseSerializer]
        while pending:
            cls = pending.pop()
            pending.extend(cls.__subclasses__())
            if not cls.__module__.startswith("urbanlens.") or not issubclass(cls, serializers.Serializer):
                continue
            seen += 1
            for field_name, field in cls._declared_fields.items():
                if (
                    isinstance(field, serializers.JSONField)
                    and not field.read_only
                    and not isinstance(field, JSONField)
                ):
                    unguarded.append(f"{cls.__module__}.{cls.__name__}.{field_name}")

        self.assertGreater(seen, 50, "found suspiciously few serializers - the scan is not reaching them")
        self.assertEqual(unguarded, [])


class FormFieldParsingTests(TestCase):
    """Helpers that read JSON out of one form field, and promise never to raise on a malformed one."""

    def test_assistant_dismissals_are_dropped(self) -> None:
        self.assertEqual(parse_dismissals_json(DEEPLY_NESTED_JSON), ())

    def test_keyboard_shortcuts_are_dropped(self) -> None:
        profile = baker.make(User).profile
        form = HotkeySettingsForm(data={"keyboard_shortcuts": DEEPLY_NESTED_JSON}, instance=profile)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["keyboard_shortcuts"], {})

    def test_search_regions_and_label_groups_are_dropped(self) -> None:
        form = SearchForm(
            data={"label_groups": DEEPLY_NESTED_JSON, "include_regions": DEEPLY_NESTED_JSON, "exclude_regions": "[["}
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.parse_label_groups())
        self.assertIsNone(form.parse_region_geojson("include_regions"))


@override_settings(UL_WEBSOCKET_FRAMES_PER_MINUTE=0, UL_MESSAGES_PER_MINUTE=0)
class ChatSocketFrameTests(TransactionTestCase):
    """A WebSocket frame is decoded the same way, and an overflowing one must not take the socket down."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.checkin = baker.make(SafetyCheckin, profile=self.user.profile)

    def test_the_socket_survives_it_and_keeps_working(self) -> None:
        async_to_sync(self._survives)()

    async def _survives(self) -> None:
        comm = WebsocketCommunicator(
            SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/checkin/{self.checkin.uuid}/chat/"
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = self.user
        connected, _ = await comm.connect()
        self.assertTrue(connected)

        await comm.send_to(text_data=_NESTED_UNDER_32K)
        await comm.send_to(text_data=json.dumps({"body": "still here"}))
        while not await comm.receive_nothing(timeout=0.3):
            await comm.receive_from()

        count = database_sync_to_async(
            SafetyCheckinMessage.objects.filter(checkin=self.checkin, body="still here").count
        )
        self.assertEqual(await count(), 1)
        await comm.disconnect()


#: ``json.loads`` calls in request-handling code that never see client input: the server's own HX-Trigger headers
#: and response bodies. Keyed by file and enclosing function.
_TRUSTED_JSON_LOADS = {
    ("controllers/albums.py", "AlbumUploadView.post"),
    ("controllers/aliases.py", "_show_toast"),
    ("controllers/custom_fields.py", "_show_toast"),
    ("controllers/custom_layers.py", "CustomLayerShareToWikiView.post"),
    ("controllers/notifications.py", "_merge_triggers"),
    ("controllers/pin_lists.py", "_show_toast"),
    ("controllers/property_owner.py", "_show_toast"),
    ("controllers/undo.py", "_with_toast"),
}


class _JsonLoadsCalls(ast.NodeVisitor):
    """Every ``json.loads(...)`` call in a module, with the qualified name of the function enclosing it."""

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[str | None, int]] = []

    def _enter(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "loads"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            self.found.append((".".join(self.stack) or None, node.lineno))
        self.generic_visit(node)


class NoBareDecodeOfClientJsonTests(SimpleTestCase):
    """Client JSON in views, forms and sockets goes through ``request_body.decode_json`` or its callers."""

    def test_only_trusted_decodes_remain(self) -> None:
        paths = [*(_DASHBOARD / "controllers").rglob("*.py"), *(_DASHBOARD / "external_api").rglob("*.py")]
        paths += [*(_DASHBOARD / "forms").rglob("*.py"), _DASHBOARD / "consumers.py"]
        stray = []
        for path in paths:
            calls = _JsonLoadsCalls()
            calls.visit(ast.parse(path.read_text(encoding="utf-8")))
            rel = path.relative_to(_DASHBOARD).as_posix()
            stray += [f"{rel}:{line} ({name})" for name, line in calls.found if (rel, name) not in _TRUSTED_JSON_LOADS]

        self.assertEqual(stray, [], "decode client JSON with request_body.decode_json/posted_json_object instead")

    def test_the_scan_finds_a_call(self) -> None:
        calls = _JsonLoadsCalls()
        calls.visit(ast.parse("def f(raw):\n    return json.loads(raw)\n"))

        self.assertEqual(calls.found, [("f", 2)])

    def test_the_scan_names_the_class_too(self) -> None:
        calls = _JsonLoadsCalls()
        calls.visit(
            ast.parse(
                "class A:\n    def post(self, raw):\n        return json.loads(raw)\nclass B:\n    def post(self):\n        pass\n"
            )
        )

        self.assertEqual(calls.found, [("A.post", 3)])
