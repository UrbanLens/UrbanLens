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


def _pre_0062_normalize(email: str) -> str:
    """normalize_email as it was before this migration: googlemail.com kept its own domain."""
    local, _, domain = email.strip().lower().rpartition("@")
    if not local or domain not in {"gmail.com", "googlemail.com"}:
        return email.strip().lower()
    mailbox = local.split("+", 1)[0].replace(".", "")
    return f"{mailbox}@{domain}" if mailbox else email.strip().lower()


def _is_googlemail(email: str | None) -> bool:
    return (email or "").strip().lower().rstrip(".").endswith("@googlemail.com")


def _update(model, pk: int, **fields) -> None:
    """Update one row, leaving it as it was when the new value would collide with another row's."""
    try:
        with transaction.atomic():
            model.objects.filter(pk=pk).update(**fields)
    except IntegrityError:
        pass


def _refold_googlemail(apps, normalize) -> None:
    """Recompute every stored form of a googlemail.com address from the address itself, under ``normalize``."""
    Profile = apps.get_model("dashboard", "Profile")
    ProfileEmail = apps.get_model("dashboard", "ProfileEmail")
    FriendInvitation = apps.get_model("dashboard", "FriendInvitation")
    TripInvitation = apps.get_model("dashboard", "TripInvitation")

    for pk, email, verified in list(Profile.objects.filter(user__email__iendswith="@googlemail.com").values_list("pk", "user__email", "verified_primary_email")):
        fields = {"primary_email_normalized": normalize(email)}
        if verified in {normalize_email(email), _pre_0062_normalize(email)}:
            fields["verified_primary_email"] = normalize(email)
        _update(Profile, pk, **fields)
    for pk, email in list(FriendInvitation.objects.filter(email__iendswith="@googlemail.com").values_list("pk", "email")):
        _update(FriendInvitation, pk, email_normalized=normalize(email))
    # These addresses are encrypted, so every row is read and filtered here.
    for row in list(ProfileEmail.objects.only("pk", "email")):
        if _is_googlemail(row.email):
            _update(ProfileEmail, row.pk, normalized_email=normalize(row.email))
    for row in list(TripInvitation.objects.only("pk", "email")):
        if _is_googlemail(row.email):
            _update(TripInvitation, row.pk, email_hash=hashlib.sha256(normalize(row.email).encode("utf-8")).hexdigest())


def fold_googlemail_into_gmail(apps, schema_editor):
    """googlemail.com is the same mailbox as gmail.com, and now normalizes to it."""
    _refold_googlemail(apps, normalize_email)


def unfold_googlemail(apps, schema_editor):
    _refold_googlemail(apps, _pre_0062_normalize)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0061_profile_username_key"),
    ]

    operations = [
        migrations.RunPython(backfill_username_keys, migrations.RunPython.noop),
        migrations.RunPython(fold_googlemail_into_gmail, unfold_googlemail),
    ]
