"""Add uuid fields to Location and Pin.

Adds the column nullable first so PostgreSQL can populate each row with
gen_random_uuid() before the NOT NULL + UNIQUE constraints are applied.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0021_location_wiki"),
    ]

    operations = [
        # ── Location.uuid ───────────────────────────────────────────────────

        # 1. Add nullable column with no default.
        migrations.AddField(
            model_name="location",
            name="uuid",
            field=models.UUIDField(null=True, blank=True, editable=False),
        ),
        # 2. Populate every existing row with a unique UUID.
        migrations.RunSQL(
            sql='UPDATE "dashboard_location" SET "uuid" = gen_random_uuid() WHERE "uuid" IS NULL',
            reverse_sql=migrations.RunSQL.noop,
        ),
        # 3. Tighten to NOT NULL + UNIQUE.
        migrations.AlterField(
            model_name="location",
            name="uuid",
            field=models.UUIDField(unique=True, editable=False),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["uuid"], name="dashboard_loc_uuid_idx"),
        ),

        # ── Pin.uuid ────────────────────────────────────────────────────────

        # 1. Add nullable column with no default.
        migrations.AddField(
            model_name="pin",
            name="uuid",
            field=models.UUIDField(null=True, blank=True, editable=False),
        ),
        # 2. Populate every existing row.
        migrations.RunSQL(
            sql='UPDATE "dashboard_user_pins" SET "uuid" = gen_random_uuid() WHERE "uuid" IS NULL',
            reverse_sql=migrations.RunSQL.noop,
        ),
        # 3. Tighten to NOT NULL + UNIQUE.
        migrations.AlterField(
            model_name="pin",
            name="uuid",
            field=models.UUIDField(unique=True, editable=False),
        ),
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["uuid"], name="dashboard_pin_uuid_idx"),
        ),
    ]
