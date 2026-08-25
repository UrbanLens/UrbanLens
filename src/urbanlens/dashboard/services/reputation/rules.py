"""The reputation rule registry.

A rule is not a constant. The whole point of this system, and the reason
Consensus points could not host it, is that *a contribution's value is a
function of how badly the target needed it at the moment it arrived* - so a
rule receives the target and works its own value out, rather than looking a
number up in a table.

Registration mirrors ``services.achievements.metrics``: a module-level dict, a
``register()`` function, and choices exposed as a callable so adding a rule
never generates a migration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.reputation.meta import TargetKind

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from urbanlens.dashboard.models.reputation.model import ReputationEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoreResult:
    """What a rule decided a contribution was worth.

    Attributes:
        value: The raw worth, before decay and caps - those are applied
            centrally by the scorer so every rule obeys them.
        inputs: The snapshot the rule worked from, stored on the row. Must be
            JSON-serialisable, and must not carry anything that would be a
            privacy problem to keep - it is read by the admin dashboard.
    """

    value: Decimal
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    """One way of earning reputation.

    Attributes:
        key: Stable identifier, stored on every row it produces. Renaming one
            orphans its history, so treat it as permanent.
        label: Human-readable name, for the admin breakdown.
        description: What earns it, in a sentence.
        target_kind: What sort of object this rule scores.
        score: Given the target, return its worth and the inputs behind it.
            Returns None when the contribution does not qualify at all - a
            materialised external photo attributed to whoever voted for it, for
            instance, which is not a contribution by that profile.
        decays: Whether repeated use within a period is subject to diminishing
            returns. Off for rules the database already bounds (a stat vote is
            unique per wiki, per field, per profile - it cannot be farmed).
        capped: Whether the per-rule and per-wiki period ceilings apply.
    """

    key: str
    label: str
    description: str
    target_kind: str
    score: Callable[[Any], ScoreResult | None]
    decays: bool = True
    capped: bool = True


_RULES: dict[str, Rule] = {}


def register(rule: Rule) -> Rule:
    """Add *rule* to the registry, replacing any rule with the same key.

    Args:
        rule: The rule to register.

    Returns:
        The rule, so this reads as a decorator if a caller wants it to.
    """
    if rule.key in _RULES and _RULES[rule.key] is not rule:
        logger.info("Replacing already-registered reputation rule %s", rule.key)
    _RULES[rule.key] = rule
    return rule


def get_rule(key: str) -> Rule | None:
    """Return the rule registered under *key*, or None."""
    return _RULES.get(key)


def all_rules() -> list[Rule]:
    """Return every registered rule, ordered by label."""
    return sorted(_RULES.values(), key=lambda rule: rule.label)


def rule_choices() -> list[tuple[str, str]]:
    """Return ``(key, label)`` pairs for a model field's choices."""
    return [(rule.key, rule.label) for rule in all_rules()]


def score_with(rule: Rule, target: Any) -> ScoreResult | None:
    """Run *rule* against *target*, converting a failure into "no award".

    A rule that raises - a plugin's model went away, a target was deleted
    between the row being written and the scorer reaching it - must not take
    down the batch it happens to be in. Mirrors ``Metric.value_for``.

    Args:
        rule: The rule to run.
        target: The object it scores, or None for target-less rules.

    Returns:
        The result, or None when the contribution does not qualify.
    """
    try:
        return rule.score(target)
    except Exception:
        logger.exception("Reputation rule %s failed while scoring %r", rule.key, target)
        return None


#: Which model a target kind dereferences to. Kept here rather than on the enum
#: so ``models.reputation.meta`` stays free of model imports.
TARGET_MODEL_PATHS: dict[str, str] = {
    TargetKind.WIKI_EDIT: "urbanlens.dashboard.models.wiki_edit.model:WikiEdit",
    TargetKind.IMAGE: "urbanlens.dashboard.models.images.model:Image",
    TargetKind.COMMENT: "urbanlens.dashboard.models.comments.model:Comment",
    TargetKind.PIN: "urbanlens.dashboard.models.pin.model:Pin",
    TargetKind.WIKI: "urbanlens.dashboard.models.wiki.model:Wiki",
    TargetKind.FRIEND_INVITATION: "urbanlens.dashboard.models.friendship.invitation.model:FriendInvitation",
    TargetKind.PROFILE: "urbanlens.dashboard.models.profile.model:Profile",
}


def resolve_target(event: ReputationEvent) -> Any | None:
    """Load the object an event is about.

    Args:
        event: The ledger row.

    Returns:
        The target, or None when it has none or has since been deleted.
    """
    path = TARGET_MODEL_PATHS.get(event.target_kind)
    if path is None or event.target_id is None:
        return None
    import importlib

    module_path, class_name = path.split(":")
    model = getattr(importlib.import_module(module_path), class_name)
    return model.objects.filter(pk=event.target_id).first()
