"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    0001_initial.py                                                                                    *
*        - Path:    /dashboard/migrations/0001_initial.py                                                              *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-01-01                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@manlyphotos.com                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    0001_initial.py                                                                                      *
*        Path:    /dashboard/migrations/0001_initial.py                                                                *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-01-01                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@manlyphotos.com                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-01-01     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""
# Generated by Django 4.2.8 on 2024-01-01 23:14

from UrbanLens.settings.app import settings
import django.contrib.gis.db.models.fields
import django.contrib.gis.geos.point
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.django.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Category",
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
                ("name", models.CharField(max_length=255)),
                (
                    "icon",
                    models.CharField(
                        choices=[
                            ("church", "church"),
                            ("factory", "factory"),
                            ("home", "home"),
                            ("hospital", "hospital"),
                            ("school", "school"),
                            ("warehouse", "warehouse"),
                            ("office_building", "office_building"),
                            ("shopping_mall", "shopping_mall"),
                            ("hotel", "hotel"),
                            ("stadium", "stadium"),
                        ],
                        max_length=255,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_categories",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Tag",
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
                ("name", models.CharField(max_length=255)),
            ],
            options={
                "db_table": "dashboard_tags",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Profile",
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
                ("avatar", models.ImageField(upload_to="")),
                ("instagram", models.CharField(blank=True, max_length=255, null=True)),
                ("discord", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.django.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_profiles",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="NotificationLog",
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
                            ("read", "Notifcation is unread: has not been seen."),
                            ("unread", "Notifcaiton has been seen."),
                            ("dismissed", "Notification was dismissed."),
                        ],
                        default="read",
                        max_length=17,
                    ),
                ),
                (
                    "importance",
                    models.CharField(
                        choices=[
                            ("lowest", "Lowest"),
                            ("low", "Low"),
                            ("medium", "Medium"),
                            ("high", "High"),
                            ("critical", "Critical"),
                        ],
                        default="lowest",
                        max_length=17,
                    ),
                ),
                (
                    "notificaiton_type",
                    models.CharField(
                        choices=[
                            ("error", "Error"),
                            ("warning", "Warning"),
                            ("info", "Info"),
                        ],
                        default="error",
                        max_length=17,
                    ),
                ),
                ("message", models.CharField(blank=True, max_length=50000)),
            ],
            options={
                "db_table": "dashboard_notifications",
                "get_latest_by": "updated",
                "abstract": False,
                "indexes": [
                    models.Index(
                        fields=["status"], name="dashboard_n_status_ff9f27_idx"
                    ),
                    models.Index(
                        fields=["importance"], name="dashboard_n_importa_7c7684_idx"
                    ),
                    models.Index(
                        fields=["notificaiton_type"],
                        name="dashboard_n_notific_045386_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Location",
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
                ("name", models.CharField(max_length=255)),
                (
                    "description",
                    models.CharField(blank=True, max_length=500, null=True),
                ),
                ("priority", models.IntegerField(default=0)),
                ("last_visited", models.DateTimeField(blank=True, null=True)),
                ("latitude", models.DecimalField(decimal_places=6, max_digits=9)),
                ("longitude", models.DecimalField(decimal_places=6, max_digits=9)),
                ("icon", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("not visited", "Not Visited"),
                            ("visited", "Visited"),
                            ("wish to visit", "Wish To Visit"),
                            ("demolished", "Demolished"),
                        ],
                        default="wish to visit",
                    ),
                ),
                (
                    "location",
                    django.contrib.gis.db.models.fields.PointField(
                        default=django.contrib.gis.geos.point.Point(0, 0),
                        geography=True,
                        srid=4326,
                    ),
                ),
                (
                    "categories",
                    models.ManyToManyField(
                        blank=True, default=list, to="dashboard.category"
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locations",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "tags",
                    models.ManyToManyField(
                        blank=True, default=list, to="dashboard.tag"
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_locations",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Image",
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
                ("image", models.ImageField(upload_to="")),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="images",
                        to="dashboard.location",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_images",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Friendship",
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
                    "friend",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="friend",
                        to=settings.django.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user",
                        to=settings.django.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_friendships",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Comment",
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
                ("text", models.CharField(max_length=500)),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="dashboard.location",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_comments",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="Review",
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
                    "rating",
                    models.IntegerField(
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(5),
                        ]
                    ),
                ),
                ("review", models.TextField()),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="dashboard.location",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.django.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "abstract": False,
                "unique_together": {("user", "location")},
            },
        ),
        migrations.AddIndex(
            model_name="profile",
            index=models.Index(fields=["user"], name="dashboard_p_user_id_eb17ed_idx"),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["name"], name="dashboard_l_name_11ee30_idx"),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(
                fields=["priority"], name="dashboard_l_priorit_4ab683_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(
                fields=["last_visited"], name="dashboard_l_last_vi_f65ca3_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(
                fields=["latitude", "longitude"], name="dashboard_l_latitud_ce2d4e_idx"
            ),
        ),
        migrations.AlterUniqueTogether(
            name="friendship",
            unique_together={("user", "friend")},
        ),
    ]
