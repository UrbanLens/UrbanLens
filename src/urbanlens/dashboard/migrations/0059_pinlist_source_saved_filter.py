# Generated manually (see PinList.source_saved_filter's field docstring for why -
# lets SavedFilterEditView find and resync smart lists still pointing at an edited filter).

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0058_undo_action_payload"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinlist",
            name="source_saved_filter",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="derived_pin_lists",
                to="dashboard.savedfilter",
            ),
        ),
    ]
