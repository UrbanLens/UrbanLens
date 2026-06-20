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
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
