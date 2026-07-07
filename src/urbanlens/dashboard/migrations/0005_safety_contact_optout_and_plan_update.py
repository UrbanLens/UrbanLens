from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_safetycheckin_final_warning_sent_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="safetycheckin",
            name="plan_update_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="SafetyContactOptOut",
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
                ("email", models.EmailField(blank=True, max_length=254, null=True)),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("checkin", "This check-in"),
                            ("owner", "All future check-ins from this person"),
                            ("global", "All safety check-in notifications"),
                        ],
                        max_length=10,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_safety_contact_opt_outs",
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="safetycontactoptout",
            name="contact_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="dashboard.profile",
            ),
        ),
        migrations.AddField(
            model_name="safetycontactoptout",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="dashboard.profile",
            ),
        ),
        migrations.AddField(
            model_name="safetycontactoptout",
            name="checkin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="contact_opt_outs",
                to="dashboard.safetycheckin",
            ),
        ),
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["contact_profile"], name="idxdb_scoo_profile"),
        ),
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["email"], name="idxdb_scoo_email"),
        ),
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["owner"], name="idxdb_scoo_owner"),
        ),
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["checkin"], name="idxdb_scoo_checkin"),
        ),
        migrations.AddConstraint(
            model_name="safetycontactoptout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("contact_profile__isnull", False),
                    ("email__isnull", False),
                    _connector="XOR",
                ),
                name="db_safety_contact_optout_exactly_one_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="safetycontactoptout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("scope", "checkin"),
                        ("checkin__isnull", False),
                        ("owner__isnull", True),
                    ),
                    models.Q(
                        ("scope", "owner"),
                        ("owner__isnull", False),
                        ("checkin__isnull", True),
                    ),
                    models.Q(
                        ("scope", "global"),
                        ("owner__isnull", True),
                        ("checkin__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="db_safety_contact_optout_scope_fields_match",
            ),
        ),
    ]
