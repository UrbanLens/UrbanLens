"""Stored pin-share notifications stop naming the sender's own pin (P210).

Until P193, ``create_pin_share`` wrote ``"{sender} shared {pin.display_label} with you."``: the sender's private name for
the pin, which a share without ``shared_name`` never consents to pass on. New rows use ``PinShare.safe_place_label``;
this rewrites the label in rows already stored, keeping the sender as it was resolved for the recipient and any
sentence after the first. The label logic is a copy of ``PinShare.safe_place_label`` as of this migration, since a
migration's models carry fields only.
"""

import re

from django.db import migrations

_MESSAGE = re.compile(r"^(?P<sender>.+?) shared (?P<label>.+) with you\.(?P<rest>.*)$", re.DOTALL)
_USA = frozenset({"united states", "united states of america", "usa", "us"})


def _area(location) -> str:
    city = (location.locality or "").strip()
    state = (location.administrative_area_level_1 or "").strip()
    country = (location.country or "").strip()
    in_usa = not country.replace(".", "").casefold() or country.replace(".", "").casefold() in _USA
    parts = [city, state] if in_usa else [city or state, country]
    return ", ".join(part for part in parts if part)


def _address(location) -> str:
    parts = []
    if location.street_number:
        parts.append(location.street_number)
    if location.route:
        parts.append(f"{location.route}," if location.locality or location.administrative_area_level_1 or location.zipcode else location.route)
    if location.locality:
        parts.append(f"{location.locality}," if location.administrative_area_level_1 or location.zipcode else location.locality)
    if location.administrative_area_level_1:
        parts.append(location.administrative_area_level_1)
    if location.zipcode:
        parts.append(location.zipcode)
    return " ".join(parts)


def _safe_label(share, wiki_names: dict) -> str:
    if share.shared_name:
        return share.shared_name
    location = share.location
    if location is None:
        return "a location"
    name = wiki_names.get(location.pk) or location.official_name
    if not name and (area := _area(location)):
        name = f"Unnamed Location in {area}"
    if name:
        return name
    return _address(location) or f"{location.latitude}, {location.longitude}"


def relabel_pin_share_notifications(apps, schema_editor) -> None:
    PinShare = apps.get_model("dashboard", "PinShare")
    Wiki = apps.get_model("dashboard", "Wiki")
    NotificationLog = apps.get_model("dashboard", "NotificationLog")

    shares = list(PinShare.objects.filter(notification__isnull=False, notification__notification_type="pin_shared").select_related("notification", "location"))
    location_ids = {share.location_id for share in shares if share.location_id is not None}
    wiki_names = {location_id: name for location_id, name in Wiki.objects.filter(location_id__in=location_ids).values_list("location_id", "name") if name}

    changed = []
    for share in shares:
        notification = share.notification
        match = _MESSAGE.match(notification.message or "")
        if match is None:
            continue
        label = _safe_label(share, wiki_names)
        if match.group("label") == label:
            continue
        notification.message = f"{match.group('sender')} shared {label} with you.{match.group('rest')}"
        changed.append(notification)
    NotificationLog.objects.bulk_update(changed, ["message"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0074_safety_contact_address_hash_and_notice_retry"),
    ]

    operations = [
        migrations.RunPython(relabel_pin_share_notifications, migrations.RunPython.noop),
    ]
