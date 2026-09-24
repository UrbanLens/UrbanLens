from zoneinfo import ZoneInfo

from django.db import migrations
from django.db.models.functions import TruncDate


def _later_duplicates(rows):
    """Ids after the first of each key, from ``(id, *key)`` tuples ordered by key then id."""
    seen = set()
    extra = []
    for row_id, *key in rows:
        key = tuple(key)
        if key in seen:
            extra.append(row_id)
        else:
            seen.add(key)
    return extra


def dedupe(apps, schema_editor):
    PinVisit = apps.get_model("dashboard", "PinVisit")
    PinSuggestion = apps.get_model("dashboard", "PinSuggestion")
    MarkupMapShare = apps.get_model("dashboard", "MarkupMapShare")
    DeviceScanUpload = apps.get_model("dashboard", "DeviceScanUpload")

    visits = (
        PinVisit.objects.filter(source="geolocation")
        .annotate(day=TruncDate("visited_at", tzinfo=ZoneInfo("UTC")))
        .order_by("pin_id", "day", "id")
        .values_list("id", "pin_id", "day")
    )
    suggestions = PinSuggestion.objects.filter(origin="community", location__isnull=False).order_by("profile_id", "location_id", "id").values_list("id", "profile_id", "location_id")
    shares = MarkupMapShare.objects.order_by("markup_map_id", "from_profile_id", "to_profile_id", "id").values_list("id", "markup_map_id", "from_profile_id", "to_profile_id")
    uploads = DeviceScanUpload.objects.exclude(client_session_uuid="").order_by("client_session_uuid", "id").values_list("id", "client_session_uuid")

    for model, rows in ((PinVisit, visits), (PinSuggestion, suggestions), (MarkupMapShare, shares), (DeviceScanUpload, uploads)):
        extra = _later_duplicates(rows.iterator())
        if extra:
            model.objects.filter(pk__in=extra).delete()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0080_sitesettings_environment_override_help_text")]

    operations = [migrations.RunPython(dedupe, migrations.RunPython.noop)]
