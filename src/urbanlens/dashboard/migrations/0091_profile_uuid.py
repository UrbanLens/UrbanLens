import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0090_image_gallery_fields"),
    ]

    operations = [
        # Step 1: add as nullable so PostgreSQL doesn't need a constant default
        # for existing rows (a single constant would violate the unique constraint).
        migrations.AddField(
            model_name="profile",
            name="uuid",
            field=models.UUIDField(null=True, blank=True),
        ),
        # Step 2: backfill every existing row with a unique UUID.
        # gen_random_uuid() is called per-row, guaranteeing uniqueness.
        migrations.RunSQL(
            sql="UPDATE dashboard_profiles SET uuid = gen_random_uuid() WHERE uuid IS NULL",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Step 3: add NOT NULL + unique now that all rows have distinct values.
        migrations.AlterField(
            model_name="profile",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
