"""Redesign NotificationLog: fix typo, fix status values, add profile FK, title, url.
Add NotificationPreference model."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0052_email_verification"),
    ]

    operations = [
        # 1. Drop old broken indexes that reference the typo'd field name
        migrations.RunSQL(
            "DROP INDEX IF EXISTS dashboard_n_notific_045386_idx;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "DROP INDEX IF EXISTS dashboard_n_status_ff9f27_idx;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "DROP INDEX IF EXISTS dashboard_n_importa_7c7684_idx;",
            reverse_sql=migrations.RunSQL.noop,
        ),

        # 2. Rename typo'd column
        migrations.RenameField(
            model_name="notificationlog",
            old_name="notificaiton_type",
            new_name="notification_type",
        ),

        # 3. Fix status column: old values "read"/"unread" are swapped in the code
        #    The DB may have rows with the literal values "read" / "unread" already.
        #    We just need the column constraints to accept the correct set; values match.
        migrations.AlterField(
            model_name="notificationlog",
            name="status",
            field=models.CharField(
                max_length=17,
                choices=[("unread", "Unread"), ("read", "Read")],
                default="unread",
            ),
        ),

        # 4. Fix notification_type choices and max_length
        migrations.AlterField(
            model_name="notificationlog",
            name="notification_type",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("trip_updated", "Trip Updated"),
                    ("friend_request", "Friend Request Received"),
                    ("message", "Message Received"),
                    ("comment_reply", "Reply to Comment"),
                    ("comment_liked", "Comment Liked"),
                    ("friend_accepted", "Friend Request Accepted"),
                    ("added_to_trip", "Added to Trip"),
                    ("wiki_updated", "Community Wiki Updated"),
                    ("error", "Error"),
                    ("warning", "Warning"),
                    ("info", "Info"),
                ],
                default="info",
            ),
        ),

        # 5. Add profile FK
        migrations.AddField(
            model_name="notificationlog",
            name="profile",
            field=models.ForeignKey(
                to="dashboard.Profile",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notifications",
                null=True,
                blank=True,
            ),
        ),

        # 6. Add title field
        migrations.AddField(
            model_name="notificationlog",
            name="title",
            field=models.CharField(max_length=255, blank=True, default=""),
            preserve_default=False,
        ),

        # 7. Add url field
        migrations.AddField(
            model_name="notificationlog",
            name="url",
            field=models.CharField(max_length=500, blank=True, default=""),
            preserve_default=False,
        ),

        # 8. Add new indexes
        migrations.AddIndex(
            model_name="notificationlog",
            index=models.Index(fields=["profile", "status"], name="dashboard_notif_profile_status_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationlog",
            index=models.Index(fields=["status"], name="dashboard_notif_status_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationlog",
            index=models.Index(fields=["importance"], name="dashboard_notif_importance_idx"),
        ),
        migrations.AddIndex(
            model_name="notificationlog",
            index=models.Index(fields=["notification_type"], name="dashboard_notif_type_idx"),
        ),

        # 9. Create NotificationPreference
        migrations.CreateModel(
            name="NotificationPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("trip_updated", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("friend_request", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("message", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("comment_reply", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("comment_liked", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("friend_accepted", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("added_to_trip", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                ("wiki_updated", models.CharField(max_length=10, choices=[("none", "None"), ("site", "Site"), ("email", "Email"), ("both", "Both")], default="site")),
                (
                    "profile",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_preferences",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={"db_table": "dashboard_notification_preferences", "abstract": False},
        ),
    ]
