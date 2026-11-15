from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0012_pin_parenting_is_manual"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="nested_by_boundary_pin",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.pin"),
        ),
    ]
