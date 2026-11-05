"""
Shared placeless places (ADR35 section 2, UL-367): a synthetic place's anchor, its members, and note-merge offers.

Depends on both migration leaves (0003_profiletrustsignals and 0004_place_evidence) because the evidence-ladder and trust-signals stories were built in parallel; this is the join, linearised later (UL-371).
"""

import django.contrib.gis.db.models.fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_profiletrustsignals"),
        ("dashboard", "0004_place_evidence"),
    ]

    operations = [
        migrations.AddField(
            model_name="place",
            name="anchor",
            field=django.contrib.gis.db.models.fields.PointField(blank=True, geography=True, null=True, srid=4326),
        ),
        migrations.CreateModel(
            name="SharedPlaceMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("place", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shared_members", to="dashboard.place")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shared_place_memberships", to="dashboard.profile")),
            ],
            options={"db_table": "dashboard_shared_place_members", "abstract": False},
        ),
        migrations.CreateModel(
            name="NotesOffer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined")], default="pending", max_length=10)),
                ("answered_at", models.DateTimeField(blank=True, null=True)),
                ("place", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notes_offers", to="dashboard.place")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notes_offers", to="dashboard.profile")),
                ("source_location", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.location")),
            ],
            options={"db_table": "dashboard_notes_offers", "abstract": False},
        ),
        migrations.AddConstraint(model_name="sharedplacemember", constraint=models.UniqueConstraint(fields=("place", "profile"), name="shared_place_member_unique")),
        migrations.AddIndex(model_name="sharedplacemember", index=models.Index(fields=["profile", "created"], name="idxdb_spm_profile_created")),
        migrations.AddConstraint(model_name="notesoffer", constraint=models.UniqueConstraint(fields=("profile", "place"), name="notes_offer_unique")),
        migrations.AddIndex(model_name="notesoffer", index=models.Index(fields=["profile", "status"], name="idxdb_notesoffer_prof_status")),
    ]
