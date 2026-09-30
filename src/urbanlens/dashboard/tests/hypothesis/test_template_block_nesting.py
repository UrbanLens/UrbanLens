"""A block the base layout renders on its own must not sit inside another block of a child template.

Django renders such a block twice: where the child nested it, and in the base's own slot. A page script nested in
``{% block content %}`` ran twice, and the second copy threw on a map that was already built.
"""

from __future__ import annotations

from pathlib import Path

from django.template.loader import get_template
from django.template.loader_tags import BlockNode
from django.test import SimpleTestCase

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_BASE = "dashboard/themes/base.html"


def _block_names(name: str) -> set[str]:
    return {node.name for node in get_template(name).template.nodelist.get_nodes_by_type(BlockNode)}


class BlockNestingTests(SimpleTestCase):
    def test_no_template_nests_a_block_the_base_renders_elsewhere(self) -> None:
        base_blocks = _block_names(_BASE)
        self.assertIn("scripts", base_blocks)
        offenders = []
        for path in sorted(_TEMPLATES.rglob("*.html")):
            name = path.relative_to(_TEMPLATES).as_posix()
            if name == _BASE:
                continue
            for outer in get_template(name).template.nodelist.get_nodes_by_type(BlockNode):
                inner = {node.name for node in outer.nodelist.get_nodes_by_type(BlockNode)} & base_blocks
                offenders += [
                    f"{name}: {{% block {block} %}} inside {{% block {outer.name} %}}" for block in sorted(inner)
                ]
        self.assertEqual(offenders, [])
