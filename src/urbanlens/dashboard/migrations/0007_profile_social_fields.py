"""Add social link fields to Profile."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0006_campus"),
    ]

    operations = [
        migrations.AddField(model_name="profile", name="bluesky", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="profile", name="uer_id", field=models.CharField(blank=True, max_length=20, null=True, verbose_name="UER poster ID")),
        migrations.AddField(model_name="profile", name="facebook", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="profile", name="flickr", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="profile", name="youtube", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="profile", name="twitch", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AddField(model_name="profile", name="website", field=models.CharField(blank=True, max_length=500, null=True)),
        # Resize existing fields to match the new max_length standard
        migrations.AlterField(model_name="profile", name="instagram", field=models.CharField(blank=True, max_length=100, null=True)),
        migrations.AlterField(model_name="profile", name="discord", field=models.CharField(blank=True, max_length=100, null=True)),
    ]
