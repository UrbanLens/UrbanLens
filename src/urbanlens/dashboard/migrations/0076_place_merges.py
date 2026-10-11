"""PlaceMerge records duplicate building places merged into one, and building_child_count stops counting superseded buildings (P98).

``PlaceMerge`` is the audit row ``manage.py merge_duplicate_buildings`` writes per loser. ``PinMergeSuggestion`` gains the
``duplicate_building`` origin for the pin pairs such a merge leaves behind.

``building_child_count`` now counts only CURRENT buildings (``services.places.lineage.refresh_derived_flags``), so the
parcels that have a superseded building child are recounted here; no other parcel's count can change.
"""

import django.core.serializers.json
from django.db import migrations, models
from django.db.models import Count
import django.db.models.deletion


def recount_parcels_with_superseded_buildings(apps, schema_editor):
    """Recount ``building_child_count`` wherever a superseded building was being counted."""
    Place = apps.get_model("dashboard", "Place")
    parent_ids = set(Place.objects.filter(kind="building", status="superseded", parent_relation="part_of", parent__isnull=False).values_list("parent_id", flat=True))
    if not parent_ids:
        return
    counts = dict(
        Place.objects.filter(parent_id__in=parent_ids, parent_relation="part_of", kind="building", status="current").values_list("parent_id").annotate(total=Count("pk")).values_list("parent_id", "total"),
    )
    for parent_id in parent_ids:
        Place.objects.filter(pk=parent_id).exclude(building_child_count=counts.get(parent_id, 0)).update(building_child_count=counts.get(parent_id, 0))


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0075_pin_share_notification_labels"),
    ]

    # The recount first: the new table's unique index is the chain's only index, and goes last.
    operations = [
        migrations.RunPython(recount_parcels_with_superseded_buildings, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pinmergesuggestion",
            name="origin",
            field=models.CharField(choices=[("legacy_cid_collision", "Legacy CID coordinate repair collision"), ("duplicate_building", "Duplicate building records merged")], max_length=30),
        ),
        migrations.CreateModel(
            name="PlaceMerge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("gers_id", models.CharField(blank=True, default="", max_length=64)),
                ("old_keys", models.JSONField(blank=True, default=list)),
                ("moved", models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ("dropped", models.JSONField(blank=True, default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ("loser", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="merges_as_loser", to="dashboard.place")),
                ("survivor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="merges_absorbed", to="dashboard.place")),
            ],
            options={
                "db_table": "dashboard_place_merges",
                "get_latest_by": "created",
                "abstract": False,
                "constraints": [models.UniqueConstraint(condition=models.Q(("loser__isnull", False)), fields=("loser",), name="place_merge_one_per_loser")],
            },
        ),
    ]
