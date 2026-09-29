from django.db import migrations
from django.db.models.functions import Left, Length

_MAX_LINK_URL_LENGTH = 2000
_MAX_CUSTOM_FIELD_TEXT_LENGTH = 5000


def _repair(queryset, column):
    from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

    unusable = []
    for pk, url in queryset.values_list("pk", column).iterator():
        try:
            cleaned = clean_link_url(url, max_length=_MAX_LINK_URL_LENGTH)
        except InvalidLinkUrlError:
            unusable.append(pk)
            continue
        if cleaned != url:
            queryset.filter(pk=pk).update(**{column: cleaned})
    return unusable


def repair_links(apps, schema_editor):
    for model_name in ("PinLink", "WikiLink"):
        model = apps.get_model("dashboard", model_name)
        model.objects.filter(pk__in=_repair(model.objects.all(), "url")).delete()
        model.objects.filter(pk__in=_repair(model.objects.exclude(wayback_url=""), "wayback_url")).update(wayback_url="")

    CustomFieldValue = apps.get_model("dashboard", "CustomFieldValue")
    url_values = CustomFieldValue.objects.filter(field__field_type="url")
    CustomFieldValue.objects.filter(pk__in=_repair(url_values, "value_text")).delete()
    CustomFieldValue.objects.annotate(text_length=Length("value_text")).filter(text_length__gt=_MAX_CUSTOM_FIELD_TEXT_LENGTH).update(
        value_text=Left("value_text", _MAX_CUSTOM_FIELD_TEXT_LENGTH)
    )


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0097_remove_mapimageoverlay_image_url")]

    operations = [migrations.RunPython(repair_links, migrations.RunPython.noop)]
