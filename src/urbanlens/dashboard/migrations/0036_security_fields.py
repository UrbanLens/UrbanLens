from django.db import migrations, models

_SECURITY_CHOICES = [("unknown", "Unknown"), ("no", "No"), ("some", "Some"), ("everywhere", "Everywhere")]


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0035_pinmarkup"),
    ]

    operations = [
        # ── Location security fields ──────────────────────────────────────────
        migrations.AddField(
            model_name="location",
            name="fences",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="alarms",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="cameras",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="security",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="signs",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="vps",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="plywood",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="locked",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="date_abandoned",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="date_last_active",
            field=models.DateField(blank=True, null=True),
        ),
        # ── Pin security fields ───────────────────────────────────────────────
        migrations.AddField(
            model_name="pin",
            name="fences",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="alarms",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="cameras",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="security",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="signs",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="vps",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="plywood",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="locked",
            field=models.CharField(choices=_SECURITY_CHOICES, default="unknown", max_length=20),
        ),
        migrations.AddField(
            model_name="pin",
            name="date_abandoned",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pin",
            name="date_last_active",
            field=models.DateField(blank=True, null=True),
        ),
    ]
