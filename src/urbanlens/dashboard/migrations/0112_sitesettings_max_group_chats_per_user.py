import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0111_image_pending_created_skips_failed")]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="max_group_chats_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of group chats one user may belong to at once. Nobody can add them to another past this. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max group chats per user",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("max_group_chats_per_user__gte", 0)), name="max_group_chats_per_user_gte_0"),
        ),
    ]
