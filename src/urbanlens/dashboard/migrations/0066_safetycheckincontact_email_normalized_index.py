from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0065_backfill_safetycheckincontact_email_normalized")]

    operations = [
        migrations.AddIndex(
            model_name="safetycheckincontact",
            index=models.Index(fields=["email_normalized"], name="idxdb_scc_email_normalized"),
        ),
    ]
