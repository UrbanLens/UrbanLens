"""
The one name migration 0030 imports from this path.

0030 references :func:`rule_choices` as a field's ``choices`` callable, and a migration cannot be edited, so the
function stays importable here. The field it belonged to moved out of core, and the callable only matters while
that migration's state is built, so it declares no choices.
"""

from __future__ import annotations


def rule_choices() -> list[tuple[str, str]]:
    """No choices: the model this callable described no longer lives in core."""
    return []
