"""Add cluster_radius to Profile — null means use the default zoom-based function."""
from django.db import migrations, models


class Migration(migrations.Migration):
	dependencies = [
		("dashboard", "0037_category_extended"),
	]

	operations = [
		migrations.AddField(
			model_name="profile",
			name="cluster_radius",
			field=models.IntegerField(blank=True, null=True),
		),
	]
