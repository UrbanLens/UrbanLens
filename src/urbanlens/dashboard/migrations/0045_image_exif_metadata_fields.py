"""EXIF-sourced photo metadata, kept separate from any user-provided equivalent.

Adds the original camera-reported GPS position (``exif_latitude``/``exif_longitude``,
closing the provenance gap ``Image.latitude``/``longitude`` has carried since they
could be overwritten by a user drag with no way back to what the camera said),
plus altitude, the two heading axes ``direction`` didn't cover, camera/lens
identification, and exposure settings. See ``models.images.model.Image`` for why
each is a separate column rather than folding into an existing one.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add EXIF-only metadata fields to Image."""

    dependencies = [
        ("dashboard", "0044_image_pending_created_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="exif_latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_altitude",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=7, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_pitch",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_roll",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_camera_make",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_camera_model",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_lens_model",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_shutter_speed",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_aperture",
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_focal_length",
            field=models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="exif_floor",
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
