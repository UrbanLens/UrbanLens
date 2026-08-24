"""Every tunable number in the reputation model, in one place.

Settled 2026-08-24 (decision 3 in ``docs/designs/reputation-and-gating.md``):
coefficients live in code, named, gathered here, and are promoted to
runtime-editable only once real data shows which ones actually need retuning.
Thirty admin knobs nobody has calibrated is its own cost.

Two things deliberately do **not** live here, and belong in ``SiteSettings``
when they are built: the gate thresholds, and the reveal-budget caps. Those are
operational safety valves rather than tuning - if the gate is wrong on launch
day it has to be loosened without a deploy.

Nothing here is calibrated yet. These are starting values chosen to hold the
*relative ordering* the source memo gave, which is the part that was actually
specified: a photo is worth notably more than an alias; up-voting somebody
else's photo is worth very little; a contribution to something that had nothing
is worth far more than the Nth of its kind.
"""

from __future__ import annotations

from decimal import Decimal

#: Base worth of a contribution type, before need, quality or decay. The memo
#: gave two anchors - a photo well above an alias, an up-vote near zero - and
#: everything else is interpolated between them.
BASE_VALUES: dict[str, Decimal] = {
    "photo_upload": Decimal(10),
    "article_revision": Decimal(12),
    "wiki_field_edit": Decimal(4),
    "wiki_created": Decimal(6),
    "comment": Decimal(2),
    "alias_added": Decimal(1),
    "link_added": Decimal(1),
    "stat_vote": Decimal("0.5"),
    "pin_created": Decimal("0.5"),
    "invite_accepted": Decimal(5),
    "active_day": Decimal("0.25"),
}

#: How much the target's prior emptiness multiplies a contribution. The memo's
#: worked example, for a photo: nothing at all on the wiki is worth "a
#: significant amount more" than external-only, which in turn beats adding to a
#: pile. Applied to every contribution type, not just photos.
NEED_FIRST_OF_ITS_KIND = Decimal("4.0")
NEED_FIRST_BY_A_USER = Decimal("2.0")
NEED_ROUTINE = Decimal("1.0")

#: Diminishing returns *within* a period: the first contribution of a kind is
#: worth its full value, the second half, the third a quarter. Rewards varied
#: regular participation over a burst of one cheap action. Resets each month.
DECAY_RATIO = Decimal("0.5")

#: Below this the row is not worth storing a value for; it rounds to nothing
#: and only adds noise to the admin breakdown.
DECAY_FLOOR = Decimal("0.01")

#: Ceiling on what one profile can earn from one wiki in one period, so no
#: single target can be farmed. Independent of the per-period cap: this one
#: bounds extraction from a target, that one bounds a total.
PER_WIKI_PERIOD_CAP = Decimal(60)

#: Ceiling on what one rule can contribute to one profile in one period, so no
#: single activity can dominate a total.
PER_RULE_PERIOD_CAP = Decimal(120)

#: Quality bonuses. Strictly *additive bonuses for metadata present*, never a
#: penalty for absence - EXIF extraction is skipped entirely when the uploader
#: has ``track_pin_visits`` off, so a penalty would quietly pay users less for
#: having a privacy setting enabled. See the design doc's implementation
#: findings.
QUALITY_HAS_CAPTURE_DATE = Decimal("1.5")
QUALITY_HAS_REAL_GPS = Decimal("1.5")

#: A photo that closes a *temporal* gap rather than a count gap - a recent shot
#: where everything on file is old. Worth real points because it is what makes
#: before/after comparison possible at all.
QUALITY_CLOSES_TIME_GAP = Decimal("3.0")

#: How old the existing photos must be before a new one counts as closing a
#: temporal gap.
TIME_GAP_DAYS = 365
