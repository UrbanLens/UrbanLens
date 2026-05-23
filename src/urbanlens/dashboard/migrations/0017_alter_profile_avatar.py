from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0016_alter_pin_categories_alter_pin_tags"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="avatar",
            field=models.ImageField(blank=True, null=True, upload_to="avatars/"),
        ),
    ]
