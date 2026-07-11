# v0.4.0 upgrade, part 5 of 5: every new index and constraint, applied last.
# By the time this runs, 0003's data backfills and 0005's pin-location dedupe
# have committed in their own transactions, so none of these CREATE INDEX /
# ADD CONSTRAINT statements can hit PostgreSQL's "pending trigger events"
# restriction - and the unique constraints are only added after the data is
# fully de-duplicated.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0005_v0_4_0_pin_location_dedupe"),
    ]

    operations = [
        # --- Pin / PinAlias / PinVisit / PinMarkup
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["location"], name="idxdb_pin_location"),
        ),
        migrations.AddConstraint(
            model_name="pin",
            constraint=models.UniqueConstraint(
                condition=models.Q(("parent_pin__isnull", True)),
                fields=("location", "profile"),
                name="db_pin_unique_location_per_profile",
            ),
        ),
        migrations.AddIndex(
            model_name="pinalias",
            index=models.Index(fields=["pin", "kind"], name="idxdb_palias_pin_kind"),
        ),
        migrations.AddIndex(
            model_name="pinalias",
            index=models.Index(
                fields=["pin", "source"], name="idxdb_palias_pin_source"
            ),
        ),
        migrations.AddIndex(
            model_name="pinvisit",
            index=models.Index(fields=["pin", "tentative"], name="idxdb_pv_pin_tent"),
        ),
        migrations.AddIndex(
            model_name="pinvisit",
            index=models.Index(
                fields=["pin", "visited_at", "tentative"], name="idxdb_pv_pin_vat_tent"
            ),
        ),
        migrations.AddIndex(
            model_name="pinmarkup",
            index=models.Index(fields=["parent_wiki"], name="idxdb_pm_wiki"),
        ),
        migrations.AddIndex(
            model_name="pinmarkup",
            index=models.Index(fields=["parent_map"], name="idxdb_pm_map"),
        ),
        # --- Review
        migrations.AlterUniqueTogether(
            name="review",
            unique_together={("profile", "pin")},
        ),
        # --- Wiki family
        migrations.AddIndex(
            model_name="wiki",
            index=models.Index(fields=["uuid"], name="idxdb_wiki_uuid"),
        ),
        migrations.AddIndex(
            model_name="wiki",
            index=models.Index(fields=["name"], name="idxdb_wiki_name"),
        ),
        migrations.AddIndex(
            model_name="wiki",
            index=models.Index(fields=["location"], name="idxdb_wiki_location"),
        ),
        migrations.AddIndex(
            model_name="wiki",
            index=models.Index(fields=["parent_wiki"], name="idxdb_wiki_parent_wiki"),
        ),
        migrations.AddIndex(
            model_name="wikialias",
            index=models.Index(fields=["wiki"], name="idxdb_walias_wiki"),
        ),
        migrations.AddIndex(
            model_name="wikialias",
            index=models.Index(fields=["wiki", "kind"], name="idxdb_walias_wiki_kind"),
        ),
        migrations.AddIndex(
            model_name="wikialias",
            index=models.Index(
                fields=["wiki", "source"], name="idxdb_walias_wiki_source"
            ),
        ),
        migrations.AddConstraint(
            model_name="wikialias",
            constraint=models.UniqueConstraint(
                fields=("wiki", "name"), name="db_walias_unique"
            ),
        ),
        migrations.AddIndex(
            model_name="wikiedit",
            index=models.Index(fields=["wiki"], name="idxdb_we_wiki"),
        ),
        migrations.AddIndex(
            model_name="wikiedit",
            index=models.Index(fields=["wiki", "created"], name="idxdb_we_created"),
        ),
        migrations.AddIndex(
            model_name="wikistatvote",
            index=models.Index(fields=["wiki", "field"], name="idxdb_wsv_wiki_field"),
        ),
        migrations.AddConstraint(
            model_name="wikistatvote",
            constraint=models.UniqueConstraint(
                fields=("wiki", "profile", "field"), name="db_wiki_stat_vote_unique"
            ),
        ),
        # --- Boundary
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("pin__isnull", True),
                    ("profile__isnull", True),
                    ("wiki__isnull", True),
                ),
                fields=("location", "boundary_type"),
                name="boundary_unique_location_default",
            ),
        ),
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(("pin__isnull", True), ("wiki__isnull", False)),
                fields=("wiki", "boundary_type"),
                name="boundary_unique_wiki",
            ),
        ),
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(("pin__isnull", False)),
                fields=("pin", "boundary_type"),
                name="boundary_unique_pin",
            ),
        ),
        # --- MarkupMap / UndoAction
        migrations.AddIndex(
            model_name="markupmap",
            index=models.Index(fields=["uuid"], name="idxdb_mm_uuid"),
        ),
        migrations.AddIndex(
            model_name="markupmap",
            index=models.Index(fields=["profile"], name="idxdb_mm_profile"),
        ),
        migrations.AddIndex(
            model_name="undoaction",
            index=models.Index(
                fields=["profile", "created"], name="idxdb_undo_profile_created"
            ),
        ),
        # --- Email / external participants / calendar links
        migrations.AddIndex(
            model_name="emailsendlog",
            index=models.Index(
                fields=["sender", "created"], name="idxdb_esl_sender_created"
            ),
        ),
        migrations.AddIndex(
            model_name="emailsendlog",
            index=models.Index(
                fields=["sender", "recipient_hash"], name="idxdb_esl_sender_recipient"
            ),
        ),
        migrations.AddIndex(
            model_name="externalvisitparticipant",
            index=models.Index(fields=["email_hash"], name="idxdb_evp_email_hash"),
        ),
        migrations.AddIndex(
            model_name="externalvisitparticipant",
            index=models.Index(fields=["visit"], name="idxdb_evp_visit"),
        ),
        migrations.AddIndex(
            model_name="tripcalendarlink",
            index=models.Index(
                fields=["profile", "google_event_id"], name="idxdb_tcl_profile_event"
            ),
        ),
        migrations.AddConstraint(
            model_name="tripcalendarlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("activity__isnull", True)),
                fields=("trip", "profile"),
                name="db_trip_calendar_link_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="tripcalendarlink",
            constraint=models.UniqueConstraint(
                fields=("trip", "profile", "activity"),
                name="db_trip_calendar_link_activity_unique",
            ),
        ),
        # --- SiteSettings / VisitSuggestion check constraints
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("external_data_cache_days__gte", 1)),
                name="external_data_cache_days_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("storage_quota_gb__gte", 0)),
                name="storage_quota_gb_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("email_limit_per_hour__gte", 0)),
                name="email_limit_per_hour_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("email_limit_per_day__gte", 0)),
                name="email_limit_per_day_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("email_limit_per_month__gte", 0)),
                name="email_limit_per_month_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("image_downscale_max_dimension__gte", 256)),
                name="image_downscale_max_dim_gte_256",
            ),
        ),
        migrations.AddConstraint(
            model_name="visitsuggestion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("origin_visit__isnull", False),
                        ("trip_activity__isnull", True),
                        ("safety_checkin__isnull", True),
                        ("origin_image__isnull", True),
                        ("from_my_activity", False),
                    ),
                    models.Q(
                        ("origin_visit__isnull", True),
                        ("trip_activity__isnull", False),
                        ("safety_checkin__isnull", True),
                        ("origin_image__isnull", True),
                        ("from_my_activity", False),
                    ),
                    models.Q(
                        ("origin_visit__isnull", True),
                        ("trip_activity__isnull", True),
                        ("safety_checkin__isnull", False),
                        ("origin_image__isnull", True),
                        ("from_my_activity", False),
                    ),
                    models.Q(
                        ("origin_visit__isnull", True),
                        ("trip_activity__isnull", True),
                        ("safety_checkin__isnull", True),
                        ("origin_image__isnull", False),
                        ("from_my_activity", False),
                    ),
                    models.Q(
                        ("origin_visit__isnull", True),
                        ("trip_activity__isnull", True),
                        ("safety_checkin__isnull", True),
                        ("origin_image__isnull", True),
                        ("from_my_activity", True),
                    ),
                    _connector="OR",
                ),
                name="db_visit_suggestion_exactly_one_origin",
            ),
        ),
    ]
