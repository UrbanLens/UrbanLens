"""
``bin/check_comment_hygiene.py`` flags each rule's cases and leaves the near misses alone.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import textwrap

from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_comment_hygiene.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_comment_hygiene", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _rules(path: str, source: str, open_ids: frozenset[str] = frozenset()) -> list[str]:
    return [v.rule for v in checker.check_file(path, textwrap.dedent(source), open_ids)]


def _history(text: str, path: str = "src/x.py", open_ids: frozenset[str] = frozenset()) -> str | None:
    found = checker.history_in(path, 1, text, open_ids)
    return None if found is None else found.message


class CommentBlockTests(SimpleTestCase):
    def test_a_block_at_the_limit_passes_and_one_past_it_fails(self) -> None:
        four = "x = 1\n" + "# line\n" * 4 + "y = 2\n"
        five = "x = 1\n" + "# line\n" * 5 + "y = 2\n"
        self.assertEqual(_rules("a.py", four), [])
        self.assertEqual(_rules("a.py", five), ["comment-block"])

    def test_a_pragma_line_splits_a_block(self) -> None:
        source = "x = 1\n" + "# line\n" * 3 + "# fmt: off\n" + "# line\n" * 3
        self.assertEqual(_rules("a.py", source), [])

    def test_a_non_python_file_header_is_exempt_but_a_later_block_is_not(self) -> None:
        header = "#!/usr/bin/env bash\n" + "# usage\n" * 8 + "set -e\n"
        self.assertEqual(_rules("bin/a.sh", header), [])
        self.assertEqual(_rules("bin/a.sh", header + "# more\n" * 5 + "echo\n"), ["comment-block"])

    def test_slash_comments_count_and_urls_do_not(self) -> None:
        source = "const a = 1;\n" + "// line\n" * 5 + 'const url = "https://example.org"; // ok\n'
        self.assertEqual(_rules("a.ts", source), ["comment-block"])
        self.assertEqual(_rules("a.ts", 'const u = "https://x.org";\n' * 6), [])


class CommentDensityTests(SimpleTestCase):
    def test_four_comments_to_nine_code_lines_is_too_dense(self) -> None:
        body = "".join(f"    a{i} = {i}  # note\n" for i in range(4)) + "".join(f"    b{i} = {i}\n" for i in range(4))
        self.assertEqual(_rules("a.py", "def f():\n" + body), ["comment-density"])

    def test_four_comments_to_twelve_code_lines_is_not(self) -> None:
        body = "".join(f"    a{i} = {i}  # note\n" for i in range(4)) + "".join(f"    b{i} = {i}\n" for i in range(7))
        self.assertEqual(_rules("a.py", "def f():\n" + body), [])

    def test_pragmas_docstrings_and_nested_functions_are_not_counted_against_the_outer(self) -> None:
        source = '''
        def outer():
            """
            Doc.

            More doc.
            More doc.
            """
            a = 1  # noqa: F841
            b = 2  # type: ignore[assignment]

            def inner():
                return 1

            return a + b
        '''
        self.assertEqual(_rules("a.py", source), [])


class HistoryReferenceTests(SimpleTestCase):
    def test_each_kind_of_history_is_flagged(self) -> None:
        maintainer = "J" + "ess"
        cases = {
            f"Per {maintainer}, keep it.": "names the maintainer",
            "C" + "laude wrote this.": "names a coding agent",
            "The previous agent left this half done.": "refers to agent work",
            "See the handoff note for why.": "refers to agent work",
            "Restored in PR #111.": "cites a pull request or commit",
            "It used to read the whole table.": "narrates a change",
            "Before the fix, this leaked.": "narrates a change",
            "We found it on staging.": "narrates a change",
            "Fixed the leak (P123).": "cites a retired tracker id",
            "An outage held it (G2-27).": "cites a retired tracker id",
            "Measured 2026-10-07: four workers.": "dates an event",
            "Since 2026-10-07.": "dates an event",
        }
        for text, label in cases.items():
            with self.subTest(text=text):
                message = _history(text)
                self.assertIsNotNone(message)
                self.assertTrue(message.startswith(label), message)

    def test_near_misses_are_not_flagged(self) -> None:
        for text in (
            "The key used to sign the token.",
            "Sent with a descriptive User-Agent; a bare urllib agent is refused.",
            "AI gateway backed by Anthropic's " + "C" + "laude models.",
            "An X-Accel hand-off, or a FileResponse.",
            'See UrbanLens#286 ("articles name provider images").',
            "Since API version 2025-03-31 it lives elsewhere.",
            "Accepts ``2026-01-01`` or `2026-01-01T00:00`.",
            "A 2019-05-01 survey.",
            "Signed with P256.",
            "A P0 for the mobile team.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(_history(text))

    def test_an_id_still_open_in_problems_is_a_pointer_not_history(self) -> None:
        self.assertIsNone(_history("Tracked as P7.", open_ids=frozenset({"P7"})))
        self.assertIsNotNone(_history("Tracked as P8.", open_ids=frozenset({"P7"})))

    def test_open_ids_are_read_from_the_problems_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "docs").mkdir()
            (pathlib.Path(tmp) / "docs" / "PROBLEMS.md").write_text(
                "| former id | issue |\n|---|---|\n| P7 | #266 |\n| T3 | #300 |\n"
            )
            self.assertEqual(checker.open_tracker_ids(pathlib.Path(tmp)), frozenset({"P7", "T3"}))

    def test_docstrings_and_slash_comments_are_read_too(self) -> None:
        self.assertEqual(_rules("a.py", 'def f():\n    """\n    It used to be slow.\n    """\n'), ["history-reference"])
        self.assertEqual(_rules("a.ts", "const a = 1; // it used to be slow\n"), ["history-reference"])
        self.assertEqual(_rules("a.ts", "/**\n * It used to be slow.\n */\nconst a = 1;\n"), ["history-reference"])


class RestatedSignatureTests(SimpleTestCase):
    def test_args_that_only_name_their_parameter_and_type_are_flagged(self) -> None:
        source = '''
        def get(request: HttpRequest, pin_ids: list[int]) -> None:
            """
            Show it.

            Args:
                request: The HTTP request.
                pin_ids: The pin IDs.
            """
        '''
        self.assertEqual(_rules("a.py", source), ["docstring-restates-signature"])

    def test_one_informative_entry_keeps_the_section(self) -> None:
        source = '''
        def get(request: HttpRequest, pin_ids: list[int]) -> None:
            """
            Show it.

            Args:
                request: The HTTP request.
                pin_ids: Pins to show, in the order the map lists them.
            """
        '''
        self.assertEqual(_rules("a.py", source), [])

    def test_returns_restating_the_annotation_is_flagged_and_without_one_is_not(self) -> None:
        annotated = 'def f() -> list[Pin]:\n    """\n    Find them.\n\n    Returns:\n        The pins.\n    """\n'
        unannotated = 'def f():\n    """\n    Find them.\n\n    Returns:\n        The pins.\n    """\n'
        detailed = (
            'def f() -> list[Pin]:\n    """\n    Find them.\n\n    Returns:\n        The pins nearest first.\n    """\n'
        )
        self.assertEqual(_rules("a.py", annotated), ["docstring-restates-signature"])
        self.assertEqual(_rules("a.py", unannotated), [])
        self.assertEqual(_rules("a.py", detailed), [])

    def test_star_parameters_described_only_as_extra_arguments_are_flagged(self) -> None:
        source = 'def f(*args, **kwargs):\n    """\n    Wrap.\n\n    Args:\n        *args: Positional arguments.\n        **kwargs: Additional keyword arguments.\n    """\n'
        self.assertEqual(_rules("a.py", source), ["docstring-restates-signature"])


class AllowMarkerTests(SimpleTestCase):
    def test_a_marker_on_or_above_the_line_allows_only_the_named_rule(self) -> None:
        dated = "x = 1  # the county renamed the field on 2025-11-01\n"
        self.assertEqual(_rules("a.py", dated), ["history-reference"])
        same_line = "x = 1  # the county renamed the field on 2025-11-01; hygiene: allow history - still sent\n"
        self.assertEqual(_rules("a.py", same_line), [])
        line_above = "# hygiene: allow history-reference - requests before then use the old field\n" + dated
        self.assertEqual(_rules("a.py", line_above), [])
        wrong_rule = "# hygiene: allow block\n" + dated
        self.assertEqual(_rules("a.py", wrong_rule), ["history-reference"])

    def test_a_marker_two_lines_up_does_not_reach(self) -> None:
        source = "# hygiene: allow history\nx = 0\nx = 1  # the county renamed the field on 2025-11-01\n"
        self.assertEqual(_rules("a.py", source), ["history-reference"])

    def test_a_marker_above_a_block_allows_it(self) -> None:
        five = "x = 1\n" + "# hygiene: allow block - a worked example\n" + "# line\n" * 5 + "y = 2\n"
        self.assertNotIn("comment-block", _rules("a.py", five))


class BaselineTests(SimpleTestCase):
    def test_a_file_that_gains_or_loses_violations_is_reported(self) -> None:
        empty = {rule: {} for rule in checker.RULES}
        baseline = {**empty, "comment-block": {"a.py": 2, "b.py": 1}}
        current = {**empty, "comment-block": {"a.py": 3}}
        gained, lost = checker.compare(current, baseline, None)
        self.assertEqual(gained, [("comment-block", "a.py", 3, 2)])
        self.assertEqual(lost, [("comment-block", "b.py", 0, 1)])

    def test_checking_named_files_ignores_the_rest_of_the_baseline(self) -> None:
        empty = {rule: {} for rule in checker.RULES}
        baseline = {**empty, "comment-block": {"a.py": 2, "b.py": 1}}
        current = {**empty, "comment-block": {"a.py": 2}}
        self.assertEqual(checker.compare(current, baseline, ["a.py"]), ([], []))

    def test_update_refuses_to_raise_a_total_unless_told_to(self) -> None:
        empty = {rule: {} for rule in checker.RULES}
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "baseline.json"
            path.write_text(json.dumps({**empty, "comment-block": {"a.py": 1}}))
            raised = {**empty, "comment-block": {"a.py": 1, "b.py": 1}}
            self.assertEqual(checker.update_baseline(raised, path, allow_increase=False), 1)
            self.assertEqual(json.loads(path.read_text())["comment-block"], {"a.py": 1})
            moved = {**empty, "comment-block": {"c.py": 1}}
            self.assertEqual(checker.update_baseline(moved, path, allow_increase=False), 0)
            self.assertEqual(json.loads(path.read_text())["comment-block"], {"c.py": 1})


class SelfTests(SimpleTestCase):
    def test_the_checker_and_these_tests_pass_their_own_rules(self) -> None:
        for path in (_CHECKER_PATH, pathlib.Path(__file__)):
            with self.subTest(path=path.name):
                self.assertEqual(checker.check_file(path.name, path.read_text(encoding="utf-8"), frozenset()), [])
