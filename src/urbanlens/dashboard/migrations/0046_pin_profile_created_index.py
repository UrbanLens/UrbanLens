from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0045_group_key_envelope_outlives_profile"),
    ]

    operations = [
        migrations.AddIndex(model_name="pin", index=models.Index(fields=["profile", "created"], name="idxdb_pin_pfile_created")),
    ]
