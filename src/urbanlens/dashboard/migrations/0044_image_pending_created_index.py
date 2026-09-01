"""Partial index for the stalled-upload recovery sweep.

``tasks.requeue_stalled_pending_uploads`` runs hourly and filters
``pending_scan=True, created__lt=...`` ordered by ``created``. Neither column
was indexed, so it sequentially scanned ``dashboard_images`` every hour.

Partial rather than a plain index on ``created``: the qualifying set is almost
always empty (a row is pending for seconds), so a full index would cost a write
on every upload for a query that reads a handful of rows.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``idxdb_image_pending_created``."""

    dependencies = [
        # Renumbered from 0043 on merge: the branch grew its own 0043 in
        # parallel, and two migrations depending on 0042 is a fork `migrate`
        # refuses. Chained behind it rather than merged, since neither touches
        # the other's tables.
        ("dashboard", "0043_external_tag_group"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="image",
            index=models.Index(condition=models.Q(("pending_scan", True)), fields=["created"], name="idxdb_image_pending_created"),
        ),
    ]
