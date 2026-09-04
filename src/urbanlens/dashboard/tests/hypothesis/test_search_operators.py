"""Operator tokenizing for the global search box.

The operator layer runs ahead of the English heuristics, so its job is to be
exact about what it claims and to hand everything else back untouched. Most of
these tests are about what it must *not* swallow.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.global_search.operators import OPERATORS, lookup, scan, suggestions


class OperatorLookupTests(SimpleTestCase):
    def test_canonical_keys_resolve(self) -> None:
        self.assertEqual(lookup("label").key, "label")

    def test_aliases_resolve_to_the_canonical_operator(self) -> None:
        self.assertEqual(lookup("tag").key, "label")
        self.assertEqual(lookup("edited").key, "updated")

    def test_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(lookup("LaBeL").key, "label")

    def test_an_unknown_spelling_is_not_an_operator(self) -> None:
        self.assertIsNone(lookup("banana"))

    def test_no_spelling_is_claimed_by_two_operators(self) -> None:
        """An alias colliding with another operator's key would silently shadow it."""
        spellings: list[str] = []
        for operator in OPERATORS:
            spellings.append(operator.key)
            spellings.extend(operator.aliases)
        self.assertEqual(len(spellings), len(set(spellings)))

    def test_every_operator_documents_itself(self) -> None:
        """The vocabulary doubles as the help text, so gaps are user-visible."""
        for operator in OPERATORS:
            self.assertTrue(operator.summary, operator.key)
            self.assertTrue(operator.example, operator.key)


class ScanTests(SimpleTestCase):
    def test_an_empty_query_scans_to_nothing(self) -> None:
        result = scan("")
        self.assertEqual(result.clauses, [])
        self.assertEqual(result.text, "")

    def test_a_plain_query_is_all_free_text(self) -> None:
        result = scan("abandoned hospital")
        self.assertEqual(result.clauses, [])
        self.assertEqual(result.text, "abandoned hospital")

    def test_a_single_operator_is_extracted(self) -> None:
        result = scan("label:rooftop")
        self.assertEqual(len(result.clauses), 1)
        self.assertEqual(result.clauses[0].key, "label")
        self.assertEqual(result.clauses[0].values, ("rooftop",))
        self.assertEqual(result.text, "")

    def test_free_text_around_an_operator_survives(self) -> None:
        result = scan("boiler label:rooftop house")
        self.assertEqual(result.clauses[0].values, ("rooftop",))
        self.assertEqual(result.text, "boiler house")

    def test_an_alias_is_normalized_to_its_canonical_key(self) -> None:
        self.assertEqual(scan("tag:tunnel").clauses[0].key, "label")

    def test_commas_mean_any_of_these(self) -> None:
        self.assertEqual(scan("label:rooftop,tunnel,vent").clauses[0].values, ("rooftop", "tunnel", "vent"))

    def test_a_quoted_value_keeps_its_spaces(self) -> None:
        result = scan('place:"Poughkeepsie, NY"')
        self.assertEqual(result.clauses[0].values, ("Poughkeepsie, NY",))
        self.assertEqual(result.text, "")

    def test_a_quoted_value_is_one_value_even_with_commas(self) -> None:
        """Quoting is how a user says the comma is part of the name."""
        self.assertEqual(len(scan('place:"Poughkeepsie, NY"').clauses[0].values), 1)

    def test_a_leading_dash_negates(self) -> None:
        clause = scan("-label:demolished").clauses[0]
        self.assertTrue(clause.negated)
        self.assertEqual(clause.values, ("demolished",))

    def test_several_operators_are_all_extracted(self) -> None:
        result = scan('type:photo label:rooftop place:"Poughkeepsie, NY"')
        self.assertEqual([clause.key for clause in result.clauses], ["type", "label", "place"])
        self.assertEqual(result.text, "")

    def test_an_unknown_key_stays_as_text_and_is_reported(self) -> None:
        """Rejecting input outright is how a search box loses its user."""
        result = scan("banana:yellow hospital")
        self.assertEqual(result.clauses, [])
        self.assertIn("banana", result.unknown_keys)
        self.assertIn("banana:yellow", result.text)

    def test_a_clock_time_is_not_mistaken_for_an_operator(self) -> None:
        result = scan("met at 12:30 today")
        self.assertEqual(result.clauses, [])
        self.assertIn("12:30", result.text)

    def test_a_url_is_not_mistaken_for_an_operator(self) -> None:
        result = scan("https://example.com/x")
        self.assertEqual(result.clauses, [])
        self.assertIn("https://example.com/x", result.text)

    def test_an_operator_with_no_value_is_dropped(self) -> None:
        """`label:` on its own filters nothing; matching everything or nothing
        would both be guesses about what a half-typed query meant."""
        result = scan("label: hospital")
        self.assertEqual(result.clauses, [])

    def test_first_returns_only_non_negated_clauses(self) -> None:
        result = scan("-type:photo type:pin")
        self.assertEqual(result.first("type").values, ("pin",))

    def test_all_for_returns_negated_clauses_too(self) -> None:
        self.assertEqual(len(scan("-label:a label:b").all_for("label")), 2)

    def test_values_are_stripped(self) -> None:
        self.assertEqual(scan("label:rooftop, tunnel").clauses[0].values, ("rooftop",))

    def test_operators_that_cannot_be_answered_still_parse(self) -> None:
        """They parse so the UI can explain the gap instead of showing an
        empty result that reads as "you have none of those"."""
        clause = scan("color:red").clauses[0]
        self.assertEqual(clause.key, "color")
        self.assertTrue(clause.operator.unsupported_reason)


class SuggestionTests(SimpleTestCase):
    def test_an_empty_prefix_offers_everything(self) -> None:
        self.assertEqual(len(suggestions("")), len(OPERATORS))

    def test_a_prefix_narrows_to_matching_keys(self) -> None:
        self.assertEqual([operator.key for operator in suggestions("vis")], ["visited", "visits"])

    def test_a_trailing_colon_is_tolerated(self) -> None:
        self.assertEqual([operator.key for operator in suggestions("label:")], ["label"])

    def test_an_alias_prefix_finds_its_operator(self) -> None:
        self.assertIn("label", [operator.key for operator in suggestions("tag")])


class ScanPropertyTests(SimpleTestCase):
    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters=':"'), max_size=80))
    def test_text_without_colons_is_returned_whole(self, raw: str) -> None:
        """Whitespace is normalized, so compare on tokens rather than exactly."""
        result = scan(raw)
        self.assertEqual(result.clauses, [])
        self.assertEqual(result.text.split(), raw.split())

    @given(
        st.sampled_from([operator.key for operator in OPERATORS]),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12),
    )
    def test_any_operator_with_a_simple_value_round_trips(self, key: str, value: str) -> None:
        result = scan(f"{key}:{value}")
        self.assertEqual(len(result.clauses), 1)
        self.assertEqual(result.clauses[0].key, lookup(key).key)
        self.assertEqual(result.clauses[0].values, (value,))
        self.assertEqual(result.text, "")

    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=120))
    def test_scanning_never_raises(self, raw: str) -> None:
        """The search box must survive whatever is pasted into it."""
        scan(raw)


class ParseQueryOperatorTests(SimpleTestCase):
    """Operators reaching `parse_query`, which is what the engine consumes.

    The two registers must land in the same fields: a downstream provider
    should never be able to tell whether the user typed English or operators.
    """

    def test_type_operator_sets_the_type_filter(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertEqual(parse_query("type:photo waterfall").types, {"photos"})

    def test_type_operator_and_english_agree(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertEqual(parse_query("type:photo waterfall").types, parse_query("photos waterfall").types)

    def test_a_quoted_place_survives_the_place_heuristic(self) -> None:
        """Bare "in Poughkeepsie, NY" is guessable; the quoted operator has to
        be exact, comma and all."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertEqual(parse_query('type:markup place:"Poughkeepsie, NY"').place, "Poughkeepsie, NY")

    def test_labels_are_collected(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertEqual(parse_query("label:rooftop,tunnel").labels, ("rooftop", "tunnel"))

    def test_negated_labels_are_kept_separate(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("label:rooftop -label:demolished")
        self.assertEqual(parsed.labels, ("rooftop",))
        self.assertEqual(parsed.exclude_labels, ("demolished",))

    def test_a_date_operator_resolves_a_range_and_names_its_field(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("visited:2019")
        self.assertEqual(parsed.date_field, "visited")
        self.assertIsNotNone(parsed.date_start)
        self.assertEqual(parsed.date_start.year, 2019)
        self.assertEqual(parsed.date_end.year, 2019)

    def test_date_operators_reuse_the_english_phrase_vocabulary(self) -> None:
        """`visited:"last march"` and "visited last march" must not be two
        date parsers that drift apart."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        viaoperator = parse_query('visited:"last march"')
        viaenglish = parse_query("pins last march")
        self.assertEqual(viaoperator.date_start, viaenglish.date_start)
        self.assertEqual(viaoperator.date_end, viaenglish.date_end)

    def test_different_date_operators_are_distinguished(self) -> None:
        """ "Visited in March" and "added in March" are different questions."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertEqual(parse_query("visited:2019").date_field, "visited")
        self.assertEqual(parse_query("created:2019").date_field, "created")

    def test_free_text_still_reaches_terms(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertIn("hospital", parse_query("type:pin hospital label:rooftop").terms)

    def test_operators_count_as_structure_for_an_otherwise_empty_query(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("label:rooftop")
        self.assertTrue(parsed.has_structure)
        self.assertFalse(parsed.is_empty)

    def test_by_and_from_are_different_people(self) -> None:
        """`by:` is who made it; `from:` is who shared it with you."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("by:dana from:sam")
        self.assertEqual(parsed.author, "dana")
        self.assertEqual(parsed.person, "sam")

    def test_near_me_operator_matches_the_english_form(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        self.assertTrue(parse_query("near:me").near_me)
        self.assertTrue(parse_query("pins near me").near_me)

    def test_chips_describe_operator_filters(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        chips = parse_query("type:pin label:rooftop -label:demolished has:photos").describe_filters()
        self.assertIn("labelled rooftop", chips)
        self.assertIn("not labelled demolished", chips)
        self.assertIn("has photos", chips)

    def test_an_unknown_key_is_surfaced_as_a_problem(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        problems = parse_query("banana:yellow").describe_problems()
        self.assertTrue(any("banana" in problem for problem in problems))

    def test_an_unanswerable_operator_explains_itself(self) -> None:
        """Better than an empty result the user reads as "I have no red photos"."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        problems = parse_query("color:red").describe_problems()
        self.assertTrue(any("colour" in problem or "color" in problem for problem in problems))

    def test_an_unbacked_is_value_explains_itself(self) -> None:
        """`is:` mixes answerable and unanswerable values on one operator - unlike
        `color:`, which is unanswerable outright."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("is:starred")
        self.assertIn(("is:starred", parsed.unsupported[0][1]), parsed.unsupported)
        problems = parsed.describe_problems()
        self.assertTrue(any("starred" in problem for problem in problems))

    def test_an_unbacked_has_value_explains_itself(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        problems = parse_query("has:coords").describe_problems()
        self.assertTrue(any("coords" in problem or "coordinates" in problem for problem in problems))

    def test_a_backed_is_value_is_not_reported_as_unsupported(self) -> None:
        """`archived` is unbacked for most types but real for safety check-ins -
        it must not be blanket-flagged as unanswerable."""
        from urbanlens.dashboard.services.global_search.parser import parse_query

        parsed = parse_query("is:archived")
        self.assertFalse(any(key.endswith("archived") for key, _reason in parsed.unsupported))

    def test_answerable_is_values_are_not_reported_as_unsupported(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query

        for value in ("visited", "unvisited", "upcoming", "past"):
            parsed = parse_query(f"is:{value}")
            self.assertEqual(parsed.unsupported, (), f"is:{value} should not be flagged unsupported")
