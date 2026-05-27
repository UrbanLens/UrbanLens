"""Rename tag_id → badge_id in all auto-created M2M through tables.

After migration 0044 renamed the Tag model to Badge (state-only), Django's
migration state already expects the FK column in every auto-created M2M
through table to be named badge_id (derived from the target model's class
name). However, the actual database columns were created when the model was
still called Tag, so they are still named tag_id.

This migration performs the real column renames so the DB matches the state.

Affected tables:
- dashboard_user_pins_tags            tag_id       → badge_id
- dashboard_user_pins_categories      tag_id       → badge_id
- dashboard_locations_tags            tag_id       → badge_id
- dashboard_locations_categories      tag_id       → badge_id
- dashboard_tags_parents              from_tag_id  → from_badge_id
                                      to_tag_id    → to_badge_id

No state operations are needed here: the rename performed by 0044 already
updated Django's migration state to expect badge_id in these columns.
"""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0045_sync_badgecustomization_field_state"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE dashboard_user_pins_tags RENAME COLUMN tag_id TO badge_id",
                "ALTER TABLE dashboard_user_pins_categories RENAME COLUMN tag_id TO badge_id",
                "ALTER TABLE dashboard_locations_tags RENAME COLUMN tag_id TO badge_id",
                "ALTER TABLE dashboard_locations_categories RENAME COLUMN tag_id TO badge_id",
                "ALTER TABLE dashboard_tags_parents RENAME COLUMN from_tag_id TO from_badge_id",
                "ALTER TABLE dashboard_tags_parents RENAME COLUMN to_tag_id TO to_badge_id",
            ],
            reverse_sql=[
                "ALTER TABLE dashboard_user_pins_tags RENAME COLUMN badge_id TO tag_id",
                "ALTER TABLE dashboard_user_pins_categories RENAME COLUMN badge_id TO tag_id",
                "ALTER TABLE dashboard_locations_tags RENAME COLUMN badge_id TO tag_id",
                "ALTER TABLE dashboard_locations_categories RENAME COLUMN badge_id TO tag_id",
                "ALTER TABLE dashboard_tags_parents RENAME COLUMN from_badge_id TO from_tag_id",
                "ALTER TABLE dashboard_tags_parents RENAME COLUMN to_badge_id TO to_tag_id",
            ],
        ),
    ]
