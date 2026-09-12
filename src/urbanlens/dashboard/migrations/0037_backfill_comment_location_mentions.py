from django.db import migrations

#: Comments read per round trip. The text column is unbounded in principle and
#: only the mentioning subset is fetched, so this trades a larger working set
#: for fewer queries rather than the other way round.
BATCH = 1000


def _extract(text):
    """Location uuids named by one comment's text.

    Deliberately the same parser the application uses rather than a copy: the
    rows this backfill writes are an index of the text, and an index built by a
    second implementation of the rule would be wrong wherever the two differ.
    """
    from urbanlens.dashboard.services.notifications.mentions import extract_location_uuids

    return extract_location_uuids(text or "")


def backfill(apps, schema_editor):
    """Record what every existing comment already names."""
    from urbanlens.dashboard.services.notifications.mentions import LOCATION_MENTION_MARKER

    Comment = apps.get_model("dashboard", "Comment")
    Mention = apps.get_model("dashboard", "CommentLocationMention")

    rows = []
    for comment_id, text in Comment.objects.filter(text__contains=LOCATION_MENTION_MARKER).values_list("id", "text").iterator(chunk_size=BATCH):
        rows.extend(Mention(comment_id=comment_id, location_uuid=value) for value in set(_extract(text)))
        if len(rows) >= BATCH:
            Mention.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        Mention.objects.bulk_create(rows, ignore_conflicts=True)


def drop(apps, schema_editor):
    """Remove every derived row, so the migration reverses cleanly."""
    apps.get_model("dashboard", "CommentLocationMention").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0036_comment_location_mentions")]
    operations = [migrations.RunPython(backfill, drop)]
