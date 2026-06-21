from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0089_profile_photo_visibility"),
    ]

    operations = [
        migrations.AlterField(
            model_name="image",
            name="pin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="images",
                to="dashboard.pin",
            ),
        ),
        migrations.AlterField(
            model_name="image",
            name="image",
            field=models.ImageField(upload_to="pin_images/"),
        ),
        migrations.AddField(
            model_name="image",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="images",
                to="dashboard.location",
            ),
        ),
        migrations.AddField(
            model_name="image",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="uploaded_images",
                to="dashboard.profile",
            ),
        ),
        migrations.AddField(
            model_name="image",
            name="caption",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
    ]
