"""Replace hide_tooltips with guidance_level (all / tooltips / none)."""

from django.db import migrations, models


def migrate_hide_tooltips_to_guidance(apps, schema_editor):
    """Map the old boolean to the new three-way preference."""
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.filter(hide_tooltips=True).update(guidance_level="none")
    Profile.objects.filter(hide_tooltips=False).update(guidance_level="all")


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0005_optimize_map_pin_loading"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="guidance_level",
            field=models.CharField(
                choices=[
                    ("all", "Guides & hints"),
                    ("tooltips", "Hints only"),
                    ("none", "Off"),
                ],
                default="all",
                help_text="Whether to show feature walkthroughs, and hover hints.",
                max_length=10,
            ),
        ),
        migrations.RunPython(migrate_hide_tooltips_to_guidance, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="profile",
            name="hide_tooltips",
        ),
        migrations.RemoveIndex(
            model_name="pin",
            name="dashboard_pin_map_page_idx",
        ),
        migrations.RemoveIndex(
            model_name="pin",
            name="dashboard_pin_point_gist",
        ),
        migrations.RemoveIndex(
            model_name="review",
            name="dashboard_review_pin_latest",
        ),
    ]
