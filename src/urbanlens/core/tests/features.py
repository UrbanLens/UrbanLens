"""Test helper for granting gated site features to fixture users.

Every game route (SpotGuessr, Trivia, Consensus) sits behind
``SiteFeature.ALPHA_FEATURES`` via ``controllers.games.AlphaFeatureRequiredMixin``,
so gameplay fixtures must entitle their users explicitly. Only the first user
created in a test database is auto-promoted to site admin (and thereby passes
every feature check implicitly); relying on that ordering hides exactly the
class of bug the feature-gate tests probe for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import User

#: Slug of the role shared by every ``grant_alpha_features`` call in one test.
ALPHA_TESTER_ROLE_SLUG = "test-alpha-testers"


def grant_alpha_features(user: User) -> None:
    """Grant ``SiteFeature.ALPHA_FEATURES`` to the user via a role subscription.

    Reuses one shared "Alpha testers" role per test, so entitling several
    users in a single fixture cannot collide on the role's unique slug.

    Args:
        user: The user to entitle.
    """
    from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

    role, _ = SubscriptionRole.objects.get_or_create(
        slug=ALPHA_TESTER_ROLE_SLUG,
        defaults={"name": "Alpha testers", "features": SiteFeature.ALPHA_FEATURES},
    )
    grant_subscription(user, role, user, None)
