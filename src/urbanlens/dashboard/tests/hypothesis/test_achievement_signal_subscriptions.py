"""Every achievement subscription must get its own live receiver.

``connect()`` registers one ``post_save`` handler per entry in
``_SUBSCRIPTIONS``, and Django dedupes receivers by ``(dispatch_uid, sender)``.
The uid used to be built from the sender model alone, so two subscriptions
naming the same model would collapse to one - the second connect silently
replacing the first, with one set of triggers and its streak bucket simply
never firing again. Nothing raises, nothing logs, and the achievements it fed
just stop being awarded.

No two subscriptions name the same model today, so this was latent rather than
live. It stays latent only while nobody adds a second trigger for a model
already in the list, which is an ordinary thing to want: ``Pin`` already feeds
one, and a second pin-derived achievement would be the obvious way to add
another.

The uid now includes the subscription's index. These tests assert the property
rather than the format, so a future keying scheme is free to change as long as
distinct subscriptions stay distinct.
"""

from __future__ import annotations

from django.db.models.signals import post_save

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.achievements import signals as achievement_signals

#: Prefix used only by the generated per-subscription receivers. Deliberately
#: not "achievements_", which the two standalone achievement receivers also
#: use - counting those alongside these hid a real collision behind a total
#: that happened to come out right.
_SUBSCRIPTION_UID_PREFIX = "achievement_subscription_"


def _subscription_uids() -> list[str]:
    """dispatch_uids of the connected per-subscription receivers.

    ``post_save.receivers`` holds ``((uid_or_id, sender_id), receiver)``.
    """
    return [key[0] for key, *_rest in post_save.receivers if isinstance(key[0], str) and key[0].startswith(_SUBSCRIPTION_UID_PREFIX)]


class AchievementSubscriptionUidTests(SimpleTestCase):
    def test_every_subscription_has_a_distinct_uid(self) -> None:
        uids = _subscription_uids()

        self.assertEqual(sorted(set(uids)), sorted(uids), "two subscription receivers share a dispatch_uid")

    def test_two_subscriptions_on_one_model_do_not_collapse(self) -> None:
        """The property the old model-only uid silently violated."""
        connected = _subscription_uids()

        # Exactly one live receiver per subscription: fewer means a later
        # connect() replaced an earlier one that shared its key.
        self.assertEqual(len(connected), len(achievement_signals._SUBSCRIPTIONS), f"{len(achievement_signals._SUBSCRIPTIONS)} subscriptions but {len(connected)} live receivers - two are sharing a dispatch_uid")

    def test_the_scan_is_finding_the_receivers_at_all(self) -> None:
        """A prefix rename would otherwise make every assertion above vacuous."""
        self.assertGreater(len(_subscription_uids()), 5, "no per-subscription receivers found - has the uid prefix changed?")
