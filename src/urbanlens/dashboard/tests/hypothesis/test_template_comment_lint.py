"""Lint: no ``{# ... #}`` comment may span lines.

Django's template lexer matches comments with a non-DOTALL ``{#.*?#}``, so a
``{#`` whose ``#}`` is on a later line is never tokenised as a comment at all -
it falls through as ordinary text and the "comment" is rendered to the user,
verbatim, in the middle of the page.

Nothing about that fails loudly: the page still returns 200 and the surrounding
markup is fine, so it survives until someone reads the rendered page carefully.
A wiki page shipped one of these. ``{% comment %}``/``{% endcomment %}`` is the
multi-line form.
"""

from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates"


def _unterminated_comment_lines() -> list[str]:
    offenders = []
    for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            opened = line.count("{#")
            if opened and opened > line.count("#}"):
                offenders.append(f"{path.relative_to(TEMPLATE_ROOT)}:{number}: {line.strip()}")
    return offenders


class TemplateCommentLintTests(SimpleTestCase):
    def test_no_template_opens_a_comment_it_does_not_close_on_the_same_line(self) -> None:
        offenders = _unterminated_comment_lines()
        self.assertEqual(
            offenders,
            [],
            "These {# #} comments span lines, so Django renders them to the user as text. "
            "Use {% comment %}...{% endcomment %} instead:\n" + "\n".join(offenders),
        )

    def test_the_lint_actually_scans_templates(self) -> None:
        """Guards against the check silently passing because it found no files."""
        self.assertGreater(len(list(TEMPLATE_ROOT.rglob("*.html"))), 100)
