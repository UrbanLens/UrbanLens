"""A block the base layout renders on its own must not sit inside another block of a child template.

Django renders such a block twice: where the child nested it, and in the base's own slot. A page script nested in
``{% block content %}`` ran twice, and the second copy threw on a map that was already built.
"""

from __future__ import annotations

from pathlib import Path

from django.template import Engine
from django.template.base import FilterExpression, NodeList
from django.template.loader_tags import BlockNode, ExtendsNode
from django.test import SimpleTestCase

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def _names() -> list[str]:
    return sorted(path.relative_to(_TEMPLATES).as_posix() for path in _TEMPLATES.rglob("*.html"))


def _blocks(nodelist: NodeList) -> list[BlockNode]:
    return [node for node in nodelist.get_nodes_by_type(BlockNode) if isinstance(node, BlockNode)]


def _template_blocks(name: str) -> list[BlockNode]:
    return _blocks(Engine.get_default().get_template(name).nodelist)


def _parent(name: str) -> str | None:
    """The template *name* extends by a literal name, if any."""
    for node in Engine.get_default().get_template(name).nodelist.get_nodes_by_type(ExtendsNode):
        if not isinstance(node, ExtendsNode) or not isinstance(node.parent_name, FilterExpression):
            continue
        parent = node.parent_name.var
        return parent if isinstance(parent, str) else None
    return None


def _own_blocks(name: str) -> set[str]:
    """The blocks a layout renders in its own place, not nested inside another of its blocks."""
    blocks = _template_blocks(name)
    nested = {inner.name for outer in blocks for inner in _blocks(outer.nodelist)}
    return {block.name for block in blocks} - nested


def _layout_blocks(name: str) -> set[str]:
    """Every block rendered in its own place by a layout *name* extends, however far up."""
    blocks: set[str] = set()
    parent = _parent(name)
    while parent:
        blocks |= _own_blocks(parent)
        parent = _parent(parent)
    return blocks


class BlockNestingTests(SimpleTestCase):
    def test_no_template_nests_a_block_its_layout_renders_elsewhere(self) -> None:
        names = _names()
        extended = {parent for name in names if (parent := _parent(name))}
        self.assertTrue({"dashboard/themes/base.html", "dashboard/themes/auth_base.html"} <= extended, extended)
        self.assertIn("auth_scripts", _layout_blocks("registration/login.html"))
        offenders = []
        for name in names:
            layout = _layout_blocks(name)
            for outer in _template_blocks(name):
                inner = {node.name for node in _blocks(outer.nodelist)} & layout
                offenders += [
                    f"{name}: {{% block {block} %}} inside {{% block {outer.name} %}}" for block in sorted(inner)
                ]
        self.assertEqual(offenders, [])
