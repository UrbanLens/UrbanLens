"""A tsconfig glob has to mean what TypeScript means by it.

`bin/check_typescript_coverage.py` decides whether a file is checked by
matching it against each project's `include`/`exclude` globs itself, rather than
starting a compiler to ask. That is only worth doing if the translation is
right: a glob read as broader than TypeScript reads it silently reports a file
as covered when no project compiles it, which is the exact failure the check
exists to catch, restored one level up.

The three wildcards under test are the ones TypeScript documents - `**` for any
number of path segments, `*` within one segment, `?` for one character.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_typescript_coverage.py"

_SEGMENT = st.text(alphabet="abcdefg", min_size=1, max_size=4)
_PATH = st.lists(_SEGMENT, min_size=1, max_size=4).map(lambda parts: "/".join(parts) + ".ts")


def _load_checker():
    """Import ``bin/check_typescript_coverage.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_typescript_coverage", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GlobTranslationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def _matches(self, pattern: str, path: str) -> bool:
        return bool(self.checker.glob_to_regex(pattern).match(path))

    def test_a_star_stays_inside_one_segment(self) -> None:
        self.assertTrue(self._matches("src/*.ts", "src/app.ts"))
        self.assertFalse(self._matches("src/*.ts", "src/nested/app.ts"))

    def test_a_double_star_spans_segments_and_none_at_all(self) -> None:
        self.assertTrue(self._matches("src/**/*.ts", "src/app.ts"))
        self.assertTrue(self._matches("src/**/*.ts", "src/a/b/c/app.ts"))
        self.assertFalse(self._matches("src/**/*.ts", "other/app.ts"))

    def test_a_question_mark_is_exactly_one_character(self) -> None:
        self.assertTrue(self._matches("src/a?.ts", "src/ab.ts"))
        self.assertFalse(self._matches("src/a?.ts", "src/abc.ts"))
        self.assertFalse(self._matches("src/a?.ts", "src/a/.ts"))

    def test_a_dot_is_literal(self) -> None:
        """`.` is a regex wildcard; unescaped, `*.ts` would match `appXts`."""
        self.assertFalse(self._matches("src/*.ts", "src/appXts"))

    def test_the_match_is_anchored_at_both_ends(self) -> None:
        self.assertFalse(self._matches("src/*.ts", "vendor/src/app.ts"))
        self.assertFalse(self._matches("src/*.ts", "src/app.ts.bak"))

    @given(_PATH)
    @settings(max_examples=100, deadline=None)
    def test_a_bare_double_star_matches_every_path(self, path: str) -> None:
        self.assertTrue(self._matches("**/*.ts", path))

    @given(_PATH)
    @settings(max_examples=100, deadline=None)
    def test_a_path_is_matched_by_the_glob_naming_its_own_directory(self, path: str) -> None:
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        pattern = "*.ts" if directory == "." else f"{directory}/*.ts"
        self.assertTrue(self._matches(pattern, path))


class ProjectCoverageTests(SimpleTestCase):
    """Reading one project's `include`/`exclude` the way `tsc -p` would."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_globs_resolve_against_the_config_own_directory(self) -> None:
        """`include: ["**/*.ts"]` in a subdirectory covers only that subtree."""
        covered = self.checker.covered_paths(
            "tests/integration/tsconfig.json",
            {"include": ["**/*.ts"]},
            ["tests/integration/specs/api/pins.ts", "src/urbanlens/dashboard/frontend/ts/map.ts"],
        )
        self.assertEqual(covered, {"tests/integration/specs/api/pins.ts"})

    def test_node_modules_is_excluded_without_being_named(self) -> None:
        covered = self.checker.covered_paths(
            "tsconfig.json",
            {"include": ["**/*.ts"]},
            ["src/app.ts", "node_modules/pkg/index.ts"],
        )
        self.assertEqual(covered, {"src/app.ts"})

    def test_an_exclude_naming_a_directory_removes_its_contents(self) -> None:
        covered = self.checker.covered_paths(
            "tests/integration/tsconfig.json",
            {"include": ["**/*.ts"], "exclude": ["reports"]},
            ["tests/integration/lib/api.ts", "tests/integration/reports/trace.ts"],
        )
        self.assertEqual(covered, {"tests/integration/lib/api.ts"})

    def test_an_include_naming_a_bare_directory_covers_the_subtree(self) -> None:
        """TypeScript infers the extensions when the entry has no glob."""
        covered = self.checker.covered_paths(
            "tsconfig.json",
            {"include": ["src"]},
            ["src/deep/app.ts", "src/widget.tsx", "other/app.ts"],
        )
        self.assertEqual(covered, {"src/deep/app.ts", "src/widget.tsx"})

    def test_a_config_with_no_include_covers_everything_beside_it(self) -> None:
        covered = self.checker.covered_paths("tsconfig.json", {}, ["src/app.ts", "bin/build.ts"])
        self.assertEqual(covered, {"src/app.ts", "bin/build.ts"})
