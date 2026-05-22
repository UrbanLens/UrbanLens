from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_pin_description_textfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="cid",
            field=models.DecimalField(blank=True, decimal_places=0, max_digits=20, null=True, unique=True),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["cid"], name="dashboard_l_cid_idx"),
        ),
    ]
