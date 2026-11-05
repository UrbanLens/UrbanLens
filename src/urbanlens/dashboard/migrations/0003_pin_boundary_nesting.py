from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_describe_seeded_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="nested_by_boundary_pin",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.pin"),
        ),
        # Added with default=True so every pin that exists at deploy is exempt from automatic nesting (a constant default
        # is metadata-only on Postgres, no table rewrite); the default then becomes False for pins created afterwards.
        migrations.AddField(
            model_name="pin",
            name="parenting_is_manual",
            field=models.BooleanField(db_default=True, default=True),
        ),
        migrations.AlterField(
            model_name="pin",
            name="parenting_is_manual",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
