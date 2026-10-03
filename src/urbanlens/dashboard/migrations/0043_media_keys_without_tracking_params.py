"""Re-key media marks and copies made while an item's URL carried ``utm_`` parameters (P215).

``media_item_key`` now hashes a URL without them. A relevance mark has no URL of its own, so its old key is matched
against the URLs in its location's cached results; a copied photo is matched the same way, and by the URL it was
fetched from.
"""

import hashlib

from django.db import migrations

from urbanlens.dashboard.services.core.tracking_params import without_tracking_params

_CHUNK = 500


def _sha1(url):
    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


def _tracked_urls(value):
    if isinstance(value, str):
        if "utm_" in value.casefold():
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _tracked_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from _tracked_urls(item)


def rekey_tracked_media(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    MediaRelevance = apps.get_model("dashboard", "MediaRelevance")
    Image = apps.get_model("dashboard", "Image")

    marked = MediaRelevance.objects.values_list("location_id", flat=True)
    copied = Image.objects.filter(media_item_key__isnull=False, location_id__isnull=False).values_list("location_id", flat=True)
    location_ids = sorted(set(marked) | set(copied))
    for start in range(0, len(location_ids), _CHUNK):
        chunk = location_ids[start : start + _CHUNK]
        renames = {}
        for location_id, data in LocationCache.objects.filter(location_id__in=chunk).values_list("location_id", "data").iterator():
            for url in _tracked_urls(data):
                clean = without_tracking_params(url)
                if clean != url:
                    renames.setdefault(location_id, {})[_sha1(url)] = _sha1(clean)
        for location_id, keys in renames.items():
            for mark in MediaRelevance.objects.filter(location_id=location_id, item_key__in=keys):
                new_key = keys[mark.item_key]
                taken = MediaRelevance.objects.filter(profile_id=mark.profile_id, location_id=location_id, source=mark.source, item_key=new_key).exists()
                if taken:
                    mark.delete()
                else:
                    MediaRelevance.objects.filter(pk=mark.pk).update(item_key=new_key)
            for old_key, new_key in keys.items():
                Image.objects.filter(location_id=location_id, media_item_key=old_key).update(media_item_key=new_key)

    for image in Image.objects.filter(media_item_key__isnull=False, source_media_url__icontains="utm_").only("pk", "media_item_key", "source_media_url").iterator():
        clean = without_tracking_params(image.source_media_url)
        if image.media_item_key == _sha1(image.source_media_url) and clean != image.source_media_url:
            Image.objects.filter(pk=image.pk).update(media_item_key=_sha1(clean))


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0042_location_cache_drop_nearby_reference_documents"),
    ]

    operations = [
        migrations.RunPython(rekey_tracked_media, migrations.RunPython.noop),
    ]
