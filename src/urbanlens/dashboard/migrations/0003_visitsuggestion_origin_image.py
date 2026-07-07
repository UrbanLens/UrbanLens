"""Add VisitSuggestion.origin_image (photo-raised suggestions) and widen the origin constraint."""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import CheckConstraint, Q


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_pinvisit_map_data_image_visit"),
    ]

    operations = [
        migrations.AddField(
            model_name="visitsuggestion",
            name="origin_image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="visit_suggestions",
                to="dashboard.image",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="visitsuggestion",
            name="db_visit_suggestion_exactly_one_origin",
        ),
        migrations.AddConstraint(
            model_name="visitsuggestion",
            constraint=CheckConstraint(
                condition=(
                    (Q(origin_visit__isnull=False) & Q(trip_activity__isnull=True) & Q(safety_checkin__isnull=True) & Q(origin_image__isnull=True))
                    | (Q(origin_visit__isnull=True) & Q(trip_activity__isnull=False) & Q(safety_checkin__isnull=True) & Q(origin_image__isnull=True))
                    | (Q(origin_visit__isnull=True) & Q(trip_activity__isnull=True) & Q(safety_checkin__isnull=False) & Q(origin_image__isnull=True))
                    | (Q(origin_visit__isnull=True) & Q(trip_activity__isnull=True) & Q(safety_checkin__isnull=True) & Q(origin_image__isnull=False))
                ),
                name="db_visit_suggestion_exactly_one_origin",
            ),
        ),
    ]
