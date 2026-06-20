import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0090_image_gallery_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        # Populate existing rows, then enforce uniqueness.
        migrations.RunSQL(
            sql="UPDATE dashboard_profile SET uuid = gen_random_uuid() WHERE uuid IS NULL OR uuid = '00000000-0000-0000-0000-000000000000'",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="profile",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
