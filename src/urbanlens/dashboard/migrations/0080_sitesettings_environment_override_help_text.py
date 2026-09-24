from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0066_safetycheckincontact_email_normalized_index")]

    operations = [
        migrations.AlterField(
            model_name="sitesettings",
            name="environment_override",
            field=models.CharField(
                choices=[
                    ("default", "Default (from environment variable)"),
                    ("production", "Production"),
                    ("development", "Development"),
                    ("testing", "Testing"),
                    ("staging", "Staging"),
                ],
                default="default",
                help_text="Override the deployment environment. Default uses the UL_ENVIRONMENT variable (production when unset).",
                max_length=20,
                verbose_name="Environment",
            ),
        ),
    ]
