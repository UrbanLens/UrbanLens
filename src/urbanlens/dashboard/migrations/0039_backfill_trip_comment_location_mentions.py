from django.db import migrations

#: Comments read per round trip, matching 0037's backfill of the pin/wiki side.
BATCH = 1000


def backfill(apps, schema_editor):
    """Record what every existing trip comment already names."""
    from urbanlens.dashboard.services.notifications.mentions import LOCATION_MENTION_MARKER, extract_location_uuids

    TripComment = apps.get_model("dashboard", "TripComment")
    Mention = apps.get_model("dashboard", "CommentLocationMention")

    rows = []
    for comment_id, text in TripComment.objects.filter(text__contains=LOCATION_MENTION_MARKER).values_list("id", "text").iterator(chunk_size=BATCH):
        rows.extend(Mention(trip_comment_id=comment_id, location_uuid=value) for value in set(extract_location_uuids(text or "")))
        if len(rows) >= BATCH:
            Mention.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        Mention.objects.bulk_create(rows, ignore_conflicts=True)


def drop(apps, schema_editor):
    """Remove only the trip-side rows, leaving 0037's alone."""
    apps.get_model("dashboard", "CommentLocationMention").objects.filter(trip_comment__isnull=False).delete()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0038_trip_comment_location_mentions")]
    operations = [migrations.RunPython(backfill, drop)]
