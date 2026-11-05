"""One advisory lock that serialises every automatic change to a profile's pin tree."""

from django.db import connection

PIN_TREE_LOCK_NAMESPACE = 7_357_363


def lock_profile_pin_tree(profile_id: int) -> None:
    """Take the transaction-scoped advisory lock for this profile's pin tree."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [PIN_TREE_LOCK_NAMESPACE, profile_id])
