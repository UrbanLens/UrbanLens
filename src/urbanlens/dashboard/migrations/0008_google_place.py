# Generated manually for GooglePlace refactor

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models
import django.db.models.deletion

_COORD_QUANT = Decimal("0.000001")


def _normalize(value) -> Decimal:
    return Decimal(str(value)).quantize(_COORD_QUANT, rounding=ROUND_HALF_UP)


def populate_google_places(apps, schema_editor):
    """Create GooglePlace rows from existing Location and Pin place metadata."""
    GooglePlace = apps.get_model("dashboard", "GooglePlace")
    Location = apps.get_model("dashboard", "Location")
    Pin = apps.get_model("dashboard", "Pin")

    aggregated: dict[tuple[Decimal, Decimal], dict[str, object]] = {}

    for loc in Location.objects.all().iterator():
        if loc.latitude is None or loc.longitude is None:
            continue
        key = (_normalize(loc.latitude), _normalize(loc.longitude))
        entry = aggregated.setdefault(key, {"name": None, "cid": None})
        if loc.cached_place_name and not entry["name"]:
            entry["name"] = loc.cached_place_name
        if loc.cid and not entry["cid"]:
            entry["cid"] = loc.cid

    for pin in Pin.objects.all().iterator():
        if pin.latitude is None or pin.longitude is None:
            continue
        key = (_normalize(pin.latitude), _normalize(pin.longitude))
        entry = aggregated.setdefault(key, {"name": None, "cid": None})
        if pin.cached_place_name and not entry["name"]:
            entry["name"] = pin.cached_place_name
        if pin.cid and not entry["cid"]:
            entry["cid"] = pin.cid

    for (latitude, longitude), data in aggregated.items():
        cid = data["cid"]
        if cid and GooglePlace.objects.filter(cid=cid).exists():
            cid = None
        GooglePlace.objects.create(
            latitude=latitude,
            longitude=longitude,
            cached_place_name=data["name"],
            cid=cid,
        )


def link_google_places(apps, schema_editor):
    """Point Location and Pin rows at the shared GooglePlace for their coordinates."""
    GooglePlace = apps.get_model("dashboard", "GooglePlace")
    Location = apps.get_model("dashboard", "Location")
    Pin = apps.get_model("dashboard", "Pin")

    lookup = {
        (_normalize(row.latitude), _normalize(row.longitude)): row.pk
        for row in GooglePlace.objects.all()
    }

    for loc in Location.objects.all().iterator():
        if loc.latitude is None or loc.longitude is None:
            continue
        google_place_id = lookup.get((_normalize(loc.latitude), _normalize(loc.longitude)))
        if google_place_id:
            Location.objects.filter(pk=loc.pk).update(google_place_id=google_place_id)

    for pin in Pin.objects.all().iterator():
        if pin.latitude is None or pin.longitude is None:
            continue
        google_place_id = lookup.get((_normalize(pin.latitude), _normalize(pin.longitude)))
        if google_place_id:
            Pin.objects.filter(pk=pin.pk).update(google_place_id=google_place_id)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0007_backup_settings"),
    ]

    operations = [
        migrations.CreateModel(
            name="GooglePlace",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("latitude", models.DecimalField(decimal_places=6, max_digits=9)),
                ("longitude", models.DecimalField(decimal_places=6, max_digits=9)),
                ("cached_place_name", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "cid",
                    models.DecimalField(blank=True, decimal_places=0, max_digits=20, null=True, unique=True),
                ),
                ("place_id", models.CharField(blank=True, max_length=255, null=True)),
            ],
            options={
                "db_table": "dashboard_google_places",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="googleplace",
            index=models.Index(fields=["latitude", "longitude"], name="dashboard_g_latitud_6a0f1d_idx"),
        ),
        migrations.AddIndex(
            model_name="googleplace",
            index=models.Index(fields=["cid"], name="dashboard_g_cid_0d8b62_idx"),
        ),
        migrations.AddConstraint(
            model_name="googleplace",
            constraint=models.UniqueConstraint(
                fields=("latitude", "longitude"),
                name="dashboard_google_place_unique_coordinates",
            ),
        ),
        migrations.RunPython(populate_google_places, migrations.RunPython.noop),
        migrations.AddField(
            model_name="location",
            name="google_place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.googleplace",
            ),
        ),
        migrations.AddField(
            model_name="pin",
            name="google_place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.googleplace",
            ),
        ),
        migrations.RunPython(link_google_places, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="location",
            name="dashboard_l_cid_c72999_idx",
        ),
        migrations.RemoveField(
            model_name="location",
            name="cached_place_name",
        ),
        migrations.RemoveField(
            model_name="location",
            name="cid",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="cached_place_name",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="cid",
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["google_place"], name="dashboard_l_google__f8d2a1_idx"),
        ),
        
        migrations.RenameIndex(
            model_name="googleplace",
            new_name="dashboard_g_latitud_4cc7c1_idx",
            old_name="dashboard_g_latitud_6a0f1d_idx",
        ),
        migrations.RenameIndex(
            model_name="googleplace",
            new_name="dashboard_g_cid_617a60_idx",
            old_name="dashboard_g_cid_0d8b62_idx",
        ),
        migrations.RenameIndex(
            model_name="location",
            new_name="dashboard_l_google__6d26b1_idx",
            old_name="dashboard_l_google__f8d2a1_idx",
        ),
    ]
