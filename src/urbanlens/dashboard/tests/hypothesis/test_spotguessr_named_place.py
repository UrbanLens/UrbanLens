"""Tests for services.spotguessr.named_place - Named Place mode's name/alias selection."""

from __future__ import annotations

from itertools import count
from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.spotguessr.named_place import candidate_name_for_location

_coordinate_counter = count()


def _make_location() -> Location:
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


class CandidateNameForLocationTests(TestCase):
    def setUp(self) -> None:
        baker.make("auth.User")

    def test_no_wiki_is_not_eligible(self) -> None:
        location = _make_location()
        self.assertIsNone(candidate_name_for_location(location))

    def test_meaningless_wiki_name_is_not_eligible(self) -> None:
        location = _make_location()
        baker.make(Wiki, location=location, name="Untitled")
        self.assertIsNone(candidate_name_for_location(location))

    def test_meaningful_wiki_name_is_used_when_no_aliases_exist(self) -> None:
        location = _make_location()
        baker.make(Wiki, location=location, name="Old Mill House")
        self.assertEqual(candidate_name_for_location(location), "Old Mill House")

    def test_use_aliases_false_always_uses_the_official_name(self) -> None:
        location = _make_location()
        wiki = baker.make(Wiki, location=location, name="Old Mill House")
        baker.make(WikiAlias, wiki=wiki, name="The Mill", kind=AliasType.NICKNAME)
        self.assertEqual(candidate_name_for_location(location, use_aliases=False), "Old Mill House")

    def test_use_aliases_true_prefers_a_meaningful_alias(self) -> None:
        """Wiki.save() auto-ensures an alias matching its own (meaningful)
        name (see models/wiki/model.py), so this wiki genuinely has two
        meaningful aliases once "The Mill" is added - "Old Mill House" (auto)
        and "The Mill" (this test's). random.choice's own selection between
        two valid candidates isn't what this test is verifying; it's
        verifying that use_aliases=True reaches the alias-selection branch
        at all (returning whatever random.choice picked) rather than falling
        straight through to the bare wiki name - so random.choice is pinned
        here rather than left to flip a real coin every run."""
        location = _make_location()
        wiki = baker.make(Wiki, location=location, name="Old Mill House")
        baker.make(WikiAlias, wiki=wiki, name="The Mill", kind=AliasType.NICKNAME)
        with mock.patch("urbanlens.dashboard.services.spotguessr.named_place.random.choice", return_value="The Mill") as mock_choice:
            result = candidate_name_for_location(location, use_aliases=True)
        mock_choice.assert_called_once()
        self.assertCountEqual(mock_choice.call_args.args[0], ["Old Mill House", "The Mill"])
        self.assertEqual(result, "The Mill")

    def test_meaningless_aliases_are_skipped_in_favor_of_the_wiki_name(self) -> None:
        location = _make_location()
        wiki = baker.make(Wiki, location=location, name="Old Mill House")
        baker.make(WikiAlias, wiki=wiki, name="12", kind=AliasType.NICKNAME)
        self.assertEqual(candidate_name_for_location(location, use_aliases=True), "Old Mill House")

    def test_meaningless_name_with_a_meaningful_alias_is_still_eligible(self) -> None:
        location = _make_location()
        wiki = baker.make(Wiki, location=location, name="Untitled")
        baker.make(WikiAlias, wiki=wiki, name="The Mill", kind=AliasType.NICKNAME)
        self.assertEqual(candidate_name_for_location(location, use_aliases=True), "The Mill")
