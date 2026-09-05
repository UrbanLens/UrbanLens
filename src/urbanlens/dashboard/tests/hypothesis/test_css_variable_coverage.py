"""A `var()` against a token nothing defines renders its fallback, forever.

That is the whole reason this needs a check rather than a review: it is not a
syntax error, and it is not a visible one either. `var(--border-color, #cbd5e1)`
renders `#cbd5e1` on every theme, so the rule reads as themed, reviews as
themed, and is a hard-coded colour - dark mode never reaches it.

`bin/check_css_variables.py` decides which reads resolve, so what needs pinning
is the three cases where "undefined" is the wrong answer: an interpolated name
Sass assembles at build time, a name set at runtime by TypeScript or a
template's inline style, and a name inside a comment - including a comment that
records a broken reference having been removed, which is exactly the shape this
check's own fixes leave behind.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_css_variables.py"


def _load_checker():
    """Import ``bin/check_css_variables.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_css_variables", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class UndefinedPropertyTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def _undefined(self, scss: str, runtime: dict[str, str] | None = None) -> dict:
        return self.checker.undefined_properties({"a.scss": scss}, runtime or {})

    def test_a_read_with_no_definition_is_reported(self) -> None:
        self.assertEqual(self._undefined(".x { color: var(--nope); }"), {"--nope": ["a.scss"]})

    def test_a_fallback_does_not_excuse_it(self) -> None:
        """The case that hides: it renders, so nothing looks wrong."""
        self.assertEqual(self._undefined(".x { color: var(--nope, #cbd5e1); }"), {"--nope": ["a.scss"]})

    def test_a_defined_property_resolves(self) -> None:
        self.assertEqual(self._undefined(":root { --ok: #fff; }\n.x { color: var(--ok); }"), {})

    def test_a_definition_in_another_stylesheet_counts(self) -> None:
        undefined = self.checker.undefined_properties(
            {"tokens.scss": ":root { --ok: #fff; }", "use.scss": ".x { color: var(--ok); }"},
            {},
        )
        self.assertEqual(undefined, {})

    def test_an_interpolated_name_is_not_a_read(self) -> None:
        """`var(--ul-#{$kind})` is a family; Sass resolves it at build time."""
        self.assertEqual(self._undefined(".x { color: var(--ul-#{$kind}); }"), {})

    def test_a_name_inside_a_line_comment_is_not_a_read(self) -> None:
        self.assertEqual(self._undefined("// not the broken var(--gone) reference\n.x { color: red; }"), {})

    def test_a_name_inside_a_block_comment_is_not_a_read(self) -> None:
        self.assertEqual(self._undefined("/* var(--gone) */\n.x { color: red; }"), {})

    def test_a_property_set_by_typescript_counts_as_defined(self) -> None:
        undefined = self._undefined(
            ".x { color: var(--tag-color); }", {"a.ts": 'el.style.setProperty("--tag-color", c);'}
        )
        self.assertEqual(undefined, {})

    def test_a_name_passed_to_a_helper_counts_too(self) -> None:
        """The name is as often an argument as it is a call-site literal."""
        undefined = self._undefined(
            ".x { top: var(--ul-undo-offset-y); }", {"a.ts": 'positionAboveColliders(root, "--ul-undo-offset-y", C);'}
        )
        self.assertEqual(undefined, {})

    def test_a_property_set_inline_by_a_template_counts_as_defined(self) -> None:
        undefined = self._undefined(
            ".x { color: var(--label-color); }", {"a.html": '<span style="--label-color: {{ c }}">'}
        )
        self.assertEqual(undefined, {})

    def test_every_stylesheet_reading_a_property_is_named(self) -> None:
        undefined = self.checker.undefined_properties(
            {"one.scss": ".a { color: var(--nope); }", "two.scss": ".b { color: var(--nope); }"},
            {},
        )
        self.assertEqual(undefined, {"--nope": ["one.scss", "two.scss"]})


class RepositoryStateTests(SimpleTestCase):
    """The tree itself, so a new undefined token fails here and not in a theme."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_no_stylesheet_reads_an_undefined_property(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[5]
        sass = root / "src/urbanlens/dashboard/frontend/sass"
        sources = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sass.rglob("*.scss")}
        self.assertTrue(sources, "no stylesheets found; this test would pass vacuously")

        runtime = {}
        for directory in ("src/urbanlens/dashboard/frontend/ts", "src/urbanlens/dashboard/templates"):
            for pattern in ("*.ts", "*.tsx", "*.html"):
                for path in (root / directory).rglob(pattern):
                    runtime[str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")

        self.assertEqual(self.checker.undefined_properties(sources, runtime), {})
