"""Add per-trip settings fields."""
from __future__ import annotations
from django.db import migrations, models


class Migration(migrations.Migration):
	dependencies = [
		("dashboard", "0050_remove_trip_members_permission"),
	]
	operations = [
		migrations.AddField(
			model_name="trip",
			name="allow_add_members",
			field=models.BooleanField(default=False, help_text="Non-creator members can add new members."),
		),
		migrations.AddField(
			model_name="trip",
			name="allow_add_activities",
			field=models.BooleanField(default=True, help_text="Non-creator members can add activities."),
		),
		migrations.AddField(
			model_name="trip",
			name="allow_edit_activities",
			field=models.BooleanField(default=False, help_text="Non-creator members can edit or delete activities."),
		),
		migrations.AddField(
			model_name="trip",
			name="allow_comments",
			field=models.BooleanField(default=True, help_text="Comments are enabled for this trip."),
		),
	]
