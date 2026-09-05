"""``{# #}`` comments that span lines render as text; the check must catch them.

Django's single-line comment form is ``{# ... #}``. An opener that does not
meet ``#}`` before the newline is not a comment - the tokens go out as text and
the visitor sees them. ``{% comment %}`` is the supported multi-line form, and
a ``{#`` inside one is not rendered, so the check must leave those alone.

The regex this used to be (``{#[^}]*$``) both misses (``{{ var }}`` inside an
unclosed comment contains ``}``) and over-matches. The property is about
``{#`` / ``#}`` pairing per line, not about ``}``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase

_CHECKER_PATH = pathlib.Path(__file__).resolve().parents[5] / "bin" / "check_template_comments.py"

_payload = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="{#}%\n\r"),
    min_size=0,
    max_size=24,
)


def _load_checker():
    """Import ``bin/check_template_comments.py`` as a module.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("urbanlens_bin_check_template_comments", _CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@st.composite
def _templates_with_expected_lines(draw) -> tuple[str, list[int]]:
    """Build a template and the line numbers that must be flagged."""
    kinds = draw(st.lists(st.sampled_from(("plain", "closed", "open")), min_size=1, max_size=10))
    lines: list[str] = []
    expected: list[int] = []
    for index, kind in enumerate(kinds, start=1):
        payload = draw(_payload)
        if kind == "plain":
            lines.append(payload)
        elif kind == "closed":
            lines.append(f"{{# {payload} #}}")
        else:
            lines.append(f"{{# {payload}")
            expected.append(index)
    return "\n".join(lines), expected


class HashCommentLineTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checker = _load_checker()

    def test_a_closed_comment_is_clean(self) -> None:
        self.assertEqual(self.checker.unclosed_hash_comment_lines("{# hide me #}\n<p>ok</p>\n"), [])

    def test_an_opener_without_a_closer_on_the_same_line_is_flagged(self) -> None:
        source = "{# this comment\nspills into the page #}\n"
        self.assertEqual(self.checker.unclosed_hash_comment_lines(source), [1])

    def test_a_never_closed_opener_is_flagged(self) -> None:
        self.assertEqual(self.checker.unclosed_hash_comment_lines("<p>{# leftover</p>\n"), [1])

    def test_a_second_opener_on_a_line_that_already_closed_one_is_flagged(self) -> None:
        self.assertEqual(self.checker.unclosed_hash_comment_lines("{# a #} visible {# b\n"), [1])

    def test_braces_inside_a_closed_comment_do_not_count_as_a_closer(self) -> None:
        """The old regex stopped at ``}``, so ``{{ var }}`` hid an unclosed comment."""
        self.assertEqual(self.checker.unclosed_hash_comment_lines("{# see {{ name }} #}\n"), [])
        self.assertEqual(self.checker.unclosed_hash_comment_lines("{# see {{ name }}\n"), [1])

    def test_a_comment_block_hides_a_multiline_hash_comment(self) -> None:
        source = "{% comment %}\n{# this\nstill has a closer later #}\n{% endcomment %}\n"
        self.assertEqual(self.checker.unclosed_hash_comment_lines(source), [])

    def test_a_verbatim_block_hides_a_multiline_hash_comment(self) -> None:
        source = "{% verbatim %}\n{# this\nis sample text #}\n{% endverbatim %}\n"
        self.assertEqual(self.checker.unclosed_hash_comment_lines(source), [])

    def test_whitespace_control_comment_blocks_are_recognised(self) -> None:
        source = "{%- comment -%}\n{# x\n#}\n{%- endcomment -%}\n"
        self.assertEqual(self.checker.unclosed_hash_comment_lines(source), [])

    def test_unicode_next_line_inside_a_closed_comment_is_not_a_line_break(self) -> None:
        """``str.splitlines()`` would split on U+0085 and flag a closed comment."""
        self.assertEqual(self.checker.unclosed_hash_comment_lines("{# \x85 #}\n"), [])

    def test_an_empty_file_is_clean(self) -> None:
        self.assertEqual(self.checker.unclosed_hash_comment_lines(""), [])

    @given(_templates_with_expected_lines())
    @settings(max_examples=60, deadline=None)
    def test_flagged_lines_are_exactly_the_unclosed_openers(self, built: tuple[str, list[int]]) -> None:
        source, expected = built
        self.assertEqual(self.checker.unclosed_hash_comment_lines(source), expected)
