"""Tests for services.ai.tools.registry - the generic rules every tool obeys.

Per-tool correctness (pins.py, trips.py) lives in their own test files; this
one only exercises what :func:`execute` enforces regardless of which tool
ran: unknown-tool/bad-args handling, URL rejection, the
:attr:`~services.sandbox.guard.ProcessRole.AI` write refusal,
``user_content_fields`` wrapping, and the result byte cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.test import override_settings
from model_bakery import baker
from pydantic import BaseModel
import pytest

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.services.ai.tools.registry import (
    MAX_TOOL_RESULT_CHARS,
    REGISTRY,
    DataScope,
    ToolContext,
    ToolResult,
    ToolSpec,
    execute,
    register,
)

#: Every tool whose :class:`DataScope` is not ``NONE``, mapped to the tests
#: proving another profile's data never reaches it - as
#: ``"<test module>::<class>::<test>"``.
#:
#: This is the registry-driven guard the design asked for, and it is here for
#: one reason: a tool that reads user data and ships without a negative-access
#: test should fail CI, not depend on a reviewer noticing. Adding a tool with
#: ``scope != NONE`` breaks :meth:`RegistrationTests.test_negative_access_coverage_is_complete`
#: until its negative case is written and named here, and renaming or deleting
#: one of those tests breaks
#: :meth:`RegistrationTests.test_every_named_negative_access_test_exists`.
#:
#: The per-tool tests stay hand-written rather than generated: each tool takes
#: different arguments and reaches different models, so "what would a leak
#: even look like" is a per-tool question. What is mechanized is that the
#: question got asked.
NEGATIVE_CASES: dict[str, tuple[str, ...]] = {
    "search_pins": ("test_ai_tools_pins::SearchPinsTests::test_another_profiles_pin_never_leaks_through",),
    "find_unvisited_pins": ("test_ai_tools_pins::FindUnvisitedPinsTests::test_only_sees_own_pins",),
    "list_trips": ("test_ai_tools_trips::ListTripsTests::test_only_sees_own_trips",),
    # A write, so its negative case is that the row it creates binds to the
    # requesting profile rather than anything the model named.
    "create_trip": ("test_ai_tools_trips::CreateTripTests::test_creates_trip_and_membership",),
    "add_trip_activity": (
        "test_ai_tools_trips::AddTripActivityTests::test_foreign_trip_is_rejected",
        "test_ai_tools_trips::AddTripActivityTests::test_foreign_pin_is_rejected",
    ),
    "undo_peek": ("test_ai_tools_undo::UndoPeekToolTests::test_another_profiles_undo_history_is_invisible",),
    "undo_last_action": (
        "test_ai_tools_undo::UndoLastActionToolTests::test_another_profiles_real_uuid_cannot_be_used_against_this_profiles_stack",
    ),
    "has_tunnels": (
        "test_ai_tools_places::HasTunnelsTests::test_another_profiles_pin_slug_never_resolves",
        "test_ai_tools_places::HasTunnelsTests::test_another_profiles_personal_floorplan_is_never_evidence",
        "test_ai_tools_places::HasTunnelsTests::test_another_profiles_unshared_photo_is_never_evidence",
    ),
    "have_i_been_here": (
        "test_ai_tools_visits::HaveIBeenHereTests::test_another_profiles_pin_slug_never_resolves",
        "test_ai_tools_visits::HaveIBeenHereTests::test_another_profiles_route_is_never_evidence",
    ),
    "distance_and_drive_time": (
        "test_ai_tools_routing::DistanceAndDriveTimeTests::test_another_profiles_pin_slug_never_resolves",
    ),
    "get_weather": ("test_ai_tools_weather::GetWeatherTests::test_another_profiles_pin_slug_never_resolves",),
}


def _plain_profile():
    """A profile with SiteFeature.AI granted - the gate these tests exercise past, not around.

    The first user in a fresh DB is auto-promoted to site admin, so a
    throwaway user absorbs that first. Every tool below declares
    ``features=frozenset({SiteFeature.AI})``, so a profile with no grant at
    all would be refused before ever reaching the tool logic these tests are
    actually about - see test_ai_access.py's own ``_grant_ai_to_everyone``
    for the same pattern.
    """
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions import SiteFeature

    baker.make("auth.User")
    settings_obj = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=settings_obj.pk).update(default_features=SiteFeature.AI)
    return _make_profile()


def _context(profile) -> ToolContext:
    return ToolContext(profile=profile, now=datetime.now(tz=UTC))


class _NoArgs(BaseModel):
    """Args model for a throwaway test-only tool that takes nothing."""


class UnknownToolAndArgsTests(TestCase):
    """Bad input becomes an error ToolResult, never an exception."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_unknown_tool_name_is_an_error_result(self) -> None:
        result = execute("not_a_real_tool", {}, _context(self.profile))
        self.assertIn("error", result.data)
        self.assertIsNone(result.summary)

    def test_missing_required_arg_is_an_error_result(self) -> None:
        # search_pins requires "query"
        result = execute("search_pins", {}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_wrong_arg_type_is_an_error_result(self) -> None:
        result = execute("search_pins", {"query": "ok", "limit": "not a number"}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_none_args_is_treated_as_empty(self) -> None:
        # list_trips takes no args - None must not raise trying to unpack it.
        result = execute("list_trips", None, _context(self.profile))
        self.assertIn("trips", result.data)


class UrlRejectionTests(TestCase):
    """No tool takes a URL - one appearing in a string arg is refused before the handler runs."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_http_url_in_a_string_arg_is_rejected(self) -> None:
        result = execute("search_pins", {"query": "http://evil.example/steal"}, _context(self.profile))
        self.assertIn("error", result.data)
        self.assertIn("URL", result.data["error"])

    def test_https_url_in_a_string_arg_is_rejected(self) -> None:
        result = execute("create_trip", {"name": "trip https://evil.example"}, _context(self.profile))
        self.assertIn("error", result.data)

    def test_plain_text_is_not_rejected(self) -> None:
        result = execute("search_pins", {"query": "steel mill"}, _context(self.profile))
        self.assertNotIn("error", result.data)


class WriteRefusalUnderAiRoleTests(TestCase):
    """Write tools never execute inside the loop - registry.execute() is the backstop."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    @override_settings(UL_PROCESS_ROLE="ai")
    def test_create_trip_is_refused_under_the_ai_role(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip

        result = execute("create_trip", {"name": "Should Not Exist"}, _context(self.profile))
        self.assertIn("error", result.data)
        self.assertFalse(Trip.objects.filter(name="Should Not Exist").exists())

    @override_settings(UL_PROCESS_ROLE="worker")
    def test_create_trip_is_not_refused_under_a_non_ai_role(self) -> None:
        result = execute("create_trip", {"name": "Fine Here"}, _context(self.profile))
        self.assertNotIn("error", result.data)

    @override_settings(UL_PROCESS_ROLE="ai")
    def test_undo_last_action_is_refused_under_the_ai_role(self) -> None:
        result = execute("undo_last_action", {"undo_uuid": "not-a-real-uuid"}, _context(self.profile))
        self.assertIn("error", result.data)

    @override_settings(UL_PROCESS_ROLE="ai")
    def test_read_only_tools_are_unaffected_by_the_ai_role(self) -> None:
        result = execute("list_trips", {}, _context(self.profile))
        self.assertNotIn("error", result.data)


class UserContentWrappingTests(TestCase):
    """Fields named in a tool's user_content_fields are wrapped before the model sees them."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_pin_name_in_a_result_is_wrapped(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude="42.5", longitude="-73.5", locality="Troy")
        baker.make(Pin, profile=self.profile, location=location, name="Steel Mill", name_is_user_provided=True)

        result = execute("search_pins", {"query": "steel"}, _context(self.profile))
        name = result.data["pins"][0]["name"]
        self.assertTrue(name.startswith("<USER_DATA>"))
        self.assertTrue(name.endswith("</USER_DATA>"))
        self.assertIn("Steel Mill", name)

    def test_non_content_fields_are_left_alone(self) -> None:
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = baker.make(Location, latitude="42.5", longitude="-73.5")
        pin = baker.make(Pin, profile=self.profile, location=location, name="Steel Mill", name_is_user_provided=True)

        result = execute("search_pins", {"query": "steel"}, _context(self.profile))
        self.assertEqual(result.data["pins"][0]["slug"], pin.slug)


def _huge_handler(context: ToolContext, args: _NoArgs) -> dict[str, Any]:
    return {"data": "x" * (MAX_TOOL_RESULT_CHARS + 1)}


def _broken_handler(context: ToolContext, args: _NoArgs) -> dict[str, Any]:
    raise RuntimeError("boom")


class ResultCapTests(TestCase):
    """An oversized result is discarded in favor of an error, not silently truncated into invalid JSON."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_oversized_result_becomes_an_error(self) -> None:
        name = "test_only_huge_result_tool"
        register(
            ToolSpec(
                name=name, description="test only", args_model=_NoArgs, handler=_huge_handler, scope=DataScope.NONE
            )
        )
        try:
            result = execute(name, {}, _context(self.profile))
        finally:
            del REGISTRY[name]
        self.assertIn("error", result.data)
        self.assertIn("too large", result.data["error"])

    def test_ordinary_result_is_not_capped(self) -> None:
        result = execute("list_trips", {}, _context(self.profile))
        self.assertNotIn("error", result.data)


class HandlerExceptionTests(TestCase):
    """A handler's own exception becomes an error result, never propagates."""

    def setUp(self) -> None:
        self.profile = _plain_profile()

    def test_handler_exception_is_caught(self) -> None:
        name = "test_only_broken_tool"
        register(
            ToolSpec(
                name=name, description="test only", args_model=_NoArgs, handler=_broken_handler, scope=DataScope.NONE
            )
        )
        try:
            result = execute(name, {}, _context(self.profile))
        finally:
            del REGISTRY[name]
        self.assertIn("error", result.data)


class RegistrationTests(TestCase):
    def test_duplicate_name_raises(self) -> None:
        def _noop(context: ToolContext, args: _NoArgs) -> dict[str, Any]:
            return {}

        with pytest.raises(ValueError, match="already registered"):
            register(ToolSpec(name="search_pins", description="dup", args_model=_NoArgs, handler=_noop))

    def test_a_write_tool_must_declare_that_it_needs_confirmation(self) -> None:
        # execute() gates writes on read_only alone, so a spec that set
        # read_only=False without requires_confirmation would describe itself
        # as running unconfirmed while actually being proposal-gated. Reject
        # it at registration rather than let the two fields disagree.
        def _noop(context: ToolContext, args: _NoArgs) -> dict[str, Any]:
            return {}

        with pytest.raises(ValueError, match="requires_confirmation"):
            register(
                ToolSpec(
                    name="unconfirmed_write",
                    description="test only",
                    args_model=_NoArgs,
                    handler=_noop,
                    read_only=False,
                )
            )

    def test_every_registered_write_tool_requires_confirmation(self) -> None:
        for spec in REGISTRY.values():
            if not spec.read_only:
                self.assertTrue(spec.requires_confirmation, f"{spec.name} is a write but does not require confirmation")

    def test_negative_access_coverage_is_complete(self) -> None:
        scoped = {name for name, spec in REGISTRY.items() if spec.scope is not DataScope.NONE}
        missing = scoped - set(NEGATIVE_CASES)
        self.assertEqual(
            missing,
            set(),
            f"Tools {sorted(missing)} declare a DataScope other than NONE but have no entry in NEGATIVE_CASES. "
            "Write a test proving a second profile gets nothing from this tool, then name it there.",
        )

    def test_negative_access_coverage_has_no_stale_entries(self) -> None:
        stale = set(NEGATIVE_CASES) - set(REGISTRY)
        self.assertEqual(stale, set(), f"NEGATIVE_CASES names tools that are no longer registered: {sorted(stale)}")

    def test_every_named_negative_access_test_exists(self) -> None:
        # Without this, NEGATIVE_CASES decays into a list of names that used
        # to mean something - a renamed or deleted test would leave the
        # completeness check above passing while the coverage it claims is gone.
        import importlib

        for tool, cases in sorted(NEGATIVE_CASES.items()):
            self.assertTrue(cases, f"{tool} has an empty NEGATIVE_CASES entry")
            for case in cases:
                module_name, class_name, test_name = case.split("::")
                module = importlib.import_module(f"urbanlens.dashboard.tests.hypothesis.{module_name}")
                test_class = getattr(module, class_name, None)
                self.assertIsNotNone(test_class, f"{tool}: {module_name} has no class {class_name}")
                self.assertTrue(hasattr(test_class, test_name), f"{tool}: {class_name} has no test {test_name}")

    def test_real_tools_are_registered(self) -> None:
        for name in (
            "search_pins",
            "find_unvisited_pins",
            "list_trips",
            "create_trip",
            "add_trip_activity",
            "get_page_help",
            "recent_dismissals",
            "reopen_explainer",
            "undo_peek",
            "undo_last_action",
            "distance_and_drive_time",
            "get_weather",
            "has_tunnels",
            "have_i_been_here",
        ):
            self.assertIn(name, REGISTRY)


class FuzzTests(TestCase):
    """Arbitrary (tool_name, raw_args) never raises out of execute() - always a ToolResult."""

    def setUp(self) -> None:
        # Created once - reused across every hypothesis example this test
        # method runs, per this repo's documented @given + TestCase pattern
        # (ORM access is fine; only @given + self.client is not).
        self.profile = _plain_profile()

    @given(
        name=st.text(min_size=0, max_size=30),
        raw_args=st.dictionaries(
            st.text(min_size=0, max_size=20),
            st.one_of(
                st.text(max_size=50),
                st.integers(),
                st.booleans(),
                st.none(),
                st.lists(st.text(max_size=10), max_size=3),
            ),
            max_size=5,
        ),
    )
    def test_never_raises(self, name: str, raw_args: dict) -> None:
        result = execute(name, raw_args, _context(self.profile))
        self.assertIsInstance(result, ToolResult)
        self.assertIsInstance(result.data, dict)
