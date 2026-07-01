# Generated manually to preserve replies when their parent comment is deleted.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0023_pin_sharing"),
    ]

    operations = [
        migrations.AlterField(
            model_name="comment",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="replies",
                to="dashboard.comment",
            ),
        ),
        migrations.AlterField(
            model_name="tripcomment",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="replies",
                to="dashboard.tripcomment",
            ),
        ),
    ]
