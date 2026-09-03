"""Composite index for the Vault gallery's page query.

Every Vault gallery page runs ``WHERE profile_id = ? AND media_type = ?
ORDER BY created DESC, id DESC LIMIT 24 OFFSET n``, and again for the ``.count()``
each page fetch issues. Verified against the dev database's ``pg_indexes`` before
adding: of the four declared indexes on ``dashboard_images``, none contained
``created`` and none paired ``profile`` with ``media_type``, so only
``dashboard_images_profile_id_c6ff6357`` was usable - Postgres read every row the
profile owns, filtered ``media_type`` on the heap, and sorted the whole set to
return 24.

Descending because that is the default sort. The oldest-first sort is the exact
reverse of these columns, which Postgres serves by scanning the same index
backwards at the same cost. The date-taken and name sorts order by an expression
(``Coalesce``/``Lower``, see ``models/images/sort.py``) and cannot use it.

Measured rather than assumed, with ``EXPLAIN (ANALYZE, BUFFERS)`` over 200k rows
across 20 profiles (9,412 photos for the profile queried), against a table
carrying the four indexes this one joins:

===================================  =========  =========
Query                                Before     After
===================================  =========  =========
One gallery page (LIMIT 24 OFFSET)   1,881 buf  6 buf
That page's ``count()``              1,881 buf  61 buf
===================================  =========  =========

Both become heap-free index-only scans, and the oldest-first sort plans as
``Index Only Scan Backward`` on the same index. Both numbers are post-``VACUUM``:
an index-only scan needs the visibility map, so on a table autovacuum has not
reached yet the planner falls back to the bitmap heap scan it used before.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ``idxdb_img_profile_kind_recent``."""

    dependencies = [
        ("dashboard", "0050_image_analysis_thumbnail"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="image",
            index=models.Index(fields=["profile", "media_type", "-created", "-id"], name="idxdb_img_profile_kind_recent"),
        ),
    ]
