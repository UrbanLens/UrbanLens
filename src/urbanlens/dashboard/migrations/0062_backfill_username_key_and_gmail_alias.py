import hashlib

from django.db import IntegrityError, migrations, transaction

from urbanlens.dashboard.services.auth.email_normalization import normalize_email
from urbanlens.dashboard.services.auth.username import normalize_username_key


def backfill_username_keys(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    for pk, key, username in Profile.objects.values_list("pk", "username_key", "user__username").iterator():
        current = normalize_username_key(username or "")
        if current != key:
            Profile.objects.filter(pk=pk).update(username_key=current)


def _renormalize(queryset, field: str) -> None:
    """Rewrite ``field`` to its current normalized form, leaving a row whose new value would collide as it was."""
    for pk, value in list(queryset.values_list("pk", field)):
        normalized = normalize_email(value) if value else value
        if normalized == value:
            continue
        try:
            with transaction.atomic():
                queryset.model.objects.filter(pk=pk).update(**{field: normalized})
        except IntegrityError:
            continue


def fold_googlemail_into_gmail(apps, schema_editor):
    """googlemail.com is the same mailbox as gmail.com, and now normalizes to it."""
    Profile = apps.get_model("dashboard", "Profile")
    ProfileEmail = apps.get_model("dashboard", "ProfileEmail")
    FriendInvitation = apps.get_model("dashboard", "FriendInvitation")
    TripInvitation = apps.get_model("dashboard", "TripInvitation")

    for pk, email in list(Profile.objects.filter(user__email__iendswith="@googlemail.com").values_list("pk", "user__email")):
        Profile.objects.filter(pk=pk).update(primary_email_normalized=normalize_email(email))
    _renormalize(Profile.objects.filter(verified_primary_email__iendswith="@googlemail.com"), "verified_primary_email")
    _renormalize(ProfileEmail.objects.filter(normalized_email__iendswith="@googlemail.com"), "normalized_email")
    _renormalize(FriendInvitation.objects.filter(email_normalized__iendswith="@googlemail.com"), "email_normalized")

    # The address is encrypted, so every row is read and its hash recomputed from the plaintext.
    for invitation in list(TripInvitation.objects.only("pk", "email")):
        if not (invitation.email or "").strip().lower().endswith("@googlemail.com"):
            continue
        email_hash = hashlib.sha256(normalize_email(invitation.email).encode("utf-8")).hexdigest()
        try:
            with transaction.atomic():
                TripInvitation.objects.filter(pk=invitation.pk).update(email_hash=email_hash)
        except IntegrityError:
            continue


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0061_profile_username_key"),
    ]

    operations = [
        migrations.RunPython(backfill_username_keys, migrations.RunPython.noop),
        migrations.RunPython(fold_googlemail_into_gmail, migrations.RunPython.noop),
    ]
