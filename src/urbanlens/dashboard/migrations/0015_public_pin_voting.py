# Public-pin voting (UL-58): candidate/vote models, the opt-out suggestion
# toggle on Profile, and the new "community" PinSuggestion origin.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0014_pinsuggestion_suggested_aliases_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="suggest_public_pins",
            field=models.BooleanField(
                default=True,
                help_text="Suggest community-approved public locations that you haven't pinned yet.",
            ),
        ),
        migrations.AlterField(
            model_name="pinsuggestion",
            name="origin",
            field=models.CharField(
                choices=[
                    ("immich", "Immich library scan"),
                    ("local_scan", "Local folder scan"),
                    ("external_api", "External app"),
                    ("community", "Community public location"),
                ],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PublicPinCandidate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("suspended", "Suspended"),
                            ("passed", "Passed"),
                            ("rejected", "Rejected"),
                        ],
                        default="open",
                        max_length=20,
                    ),
                ),
                ("opened_at", models.DateTimeField()),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                (
                    "location",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="public_candidate",
                        to="dashboard.location",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_public_pin_candidates",
                "abstract": False,
                "indexes": [models.Index(fields=["status"], name="idxdb_ppc_status")],
            },
        ),
        migrations.CreateModel(
            name="PublicPinVote",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("make_public", models.BooleanField()),
                (
                    "candidate",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="votes",
                        to="dashboard.publicpincandidate",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="public_pin_votes",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_public_pin_votes",
                "abstract": False,
                "constraints": [
                    models.UniqueConstraint(
                        fields=("candidate", "profile"), name="db_public_pin_vote_unique"
                    )
                ],
            },
        ),
    ]
