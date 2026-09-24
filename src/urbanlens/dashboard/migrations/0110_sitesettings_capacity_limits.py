import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0080_sitesettings_environment_override_help_text")]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="max_saved_filters_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of saved filters a user may keep. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max saved filters per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_pin_lists_per_user",
            field=models.IntegerField(
                default=500,
                help_text="Maximum number of pin lists a user may own. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100000)],
                verbose_name="Max lists per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_labels_per_user",
            field=models.IntegerField(
                default=2000,
                help_text="Maximum number of personal labels (of every kind) a user may own. Site-wide labels do not count. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100000)],
                verbose_name="Max labels per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_custom_fields_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of custom fields a user may define, across every entity type. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max custom fields per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_push_devices_per_user",
            field=models.IntegerField(
                default=10,
                help_text="Maximum number of active push devices a user may register. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1000)],
                verbose_name="Max push devices per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_photos_per_album",
            field=models.IntegerField(
                default=5000,
                help_text="Maximum number of photos one album may hold. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1000000)],
                verbose_name="Max photos per album",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_saved_filters_per_user__gte", 0)), name="max_saved_filters_per_user_gte_0"),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_pin_lists_per_user__gte", 0)), name="max_pin_lists_per_user_gte_0"),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_labels_per_user__gte", 0)), name="max_labels_per_user_gte_0"),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_custom_fields_per_user__gte", 0)), name="max_custom_fields_per_user_gte_0"),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_push_devices_per_user__gte", 0)), name="max_push_devices_per_user_gte_0"),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_photos_per_album__gte", 0)), name="max_photos_per_album_gte_0"),
        ),
    ]
