"""Mark which root pins the user placed in the hierarchy by hand. Existing rows count as manual so the property nesting pass leaves them alone."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0004_backfill_pin_import_keys")]

    operations = [
        migrations.AddField(model_name="pin", name="parenting_is_manual", field=models.BooleanField(default=True)),
        migrations.AlterField(model_name="pin", name="parenting_is_manual", field=models.BooleanField(db_default=False, default=False)),
    ]
