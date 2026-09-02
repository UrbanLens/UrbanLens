"""Tests for services.ai.dismissals (plan §10, batch 4)."""

from __future__ import annotations

import json

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.dismissals import (
    BODY_MAX_CHARS,
    HEADING_MAX_CHARS,
    MAX_DISMISSALS,
    DismissalEntry,
    dismissals_from_list,
    dismissals_to_list,
    parse_dismissals_json,
)


class ParseDismissalsJsonTests(SimpleTestCase):
    def test_a_well_formed_explainer_entry_round_trips(self) -> None:
        raw = json.dumps([{"id": "organize-labels-intro", "kind": "explainer", "heading": "Labels", "body": "Tag your pins.", "page": "/organize/"}])
        self.assertEqual(
            parse_dismissals_json(raw),
            (DismissalEntry(id="organize-labels-intro", kind="explainer", heading="Labels", body="Tag your pins.", page="/organize/"),),
        )

    def test_a_tour_entry_keeps_its_prefix(self) -> None:
        raw = json.dumps([{"id": "step-one", "kind": "tour", "heading": "Reorder", "body": "Drag to prioritize.", "page": "/organize/", "prefix": "ul_onboarding_v1_organize"}])
        entries = parse_dismissals_json(raw)
        self.assertEqual(entries[0].prefix, "ul_onboarding_v1_organize")

    def test_empty_string_is_no_entries(self) -> None:
        self.assertEqual(parse_dismissals_json(""), ())

    def test_not_json_is_no_entries_not_a_raise(self) -> None:
        self.assertEqual(parse_dismissals_json("not json"), ())

    def test_a_json_object_instead_of_a_list_is_no_entries(self) -> None:
        self.assertEqual(parse_dismissals_json(json.dumps({"id": "x"})), ())

    def test_an_item_missing_a_required_field_is_dropped(self) -> None:
        raw = json.dumps([{"id": "x", "kind": "explainer", "heading": "H"}])  # no body/page
        self.assertEqual(parse_dismissals_json(raw), ())

    def test_an_unknown_kind_is_dropped(self) -> None:
        raw = json.dumps([{"id": "x", "kind": "sorcery", "heading": "H", "body": "B", "page": "/"}])
        self.assertEqual(parse_dismissals_json(raw), ())

    def test_a_non_dict_item_is_dropped(self) -> None:
        raw = json.dumps(["not a dict"])
        self.assertEqual(parse_dismissals_json(raw), ())

    def test_valid_and_invalid_items_are_mixed_gracefully(self) -> None:
        raw = json.dumps([{"id": "x"}, {"id": "y", "kind": "explainer", "heading": "H", "body": "B", "page": "/"}])
        entries = parse_dismissals_json(raw)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "y")

    def test_heading_and_body_are_capped_server_side_too(self) -> None:
        raw = json.dumps([{"id": "x", "kind": "explainer", "heading": "H" * 500, "body": "B" * 2000, "page": "/"}])
        entries = parse_dismissals_json(raw)
        self.assertEqual(len(entries[0].heading), HEADING_MAX_CHARS)
        self.assertEqual(len(entries[0].body), BODY_MAX_CHARS)

    def test_more_than_the_cap_is_truncated_not_rejected(self) -> None:
        raw = json.dumps([{"id": f"x{i}", "kind": "explainer", "heading": "H", "body": "B", "page": "/"} for i in range(MAX_DISMISSALS + 3)])
        self.assertEqual(len(parse_dismissals_json(raw)), MAX_DISMISSALS)


class DismissalsListRoundTripTests(SimpleTestCase):
    def test_round_trips_through_a_celery_safe_list(self) -> None:
        entries = (DismissalEntry(id="x", kind="explainer", heading="H", body="B", page="/"),)
        self.assertEqual(dismissals_from_list(dismissals_to_list(entries)), entries)

    def test_none_is_no_entries(self) -> None:
        self.assertEqual(dismissals_from_list(None), ())

    def test_a_non_list_is_no_entries(self) -> None:
        self.assertEqual(dismissals_from_list({"id": "x"}), ())  # type: ignore[arg-type]
