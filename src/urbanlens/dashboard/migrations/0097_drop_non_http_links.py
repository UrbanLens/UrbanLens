from django.db import migrations
from django.db.models.functions import Left, Length

_MAX_LINK_URL_LENGTH = 2000
_MAX_CUSTOM_FIELD_TEXT_LENGTH = 5000


def drop_non_http_links(apps, schema_editor):
    from urbanlens.dashboard.services.security.link_urls import is_link_url

    for model_name in ("PinLink", "WikiLink"):
        model = apps.get_model("dashboard", model_name)
        bad = [pk for pk, url in model.objects.values_list("pk", "url").iterator() if not is_link_url(url, max_length=_MAX_LINK_URL_LENGTH)]
        model.objects.filter(pk__in=bad).delete()
        stale = [pk for pk, url in model.objects.exclude(wayback_url="").values_list("pk", "wayback_url").iterator() if not is_link_url(url, max_length=_MAX_LINK_URL_LENGTH)]
        model.objects.filter(pk__in=stale).update(wayback_url="")

    CustomFieldValue = apps.get_model("dashboard", "CustomFieldValue")
    url_values = CustomFieldValue.objects.filter(field__field_type="url").values_list("pk", "value_text")
    bad = [pk for pk, url in url_values.iterator() if not is_link_url(url, max_length=_MAX_LINK_URL_LENGTH)]
    CustomFieldValue.objects.filter(pk__in=bad).delete()
    CustomFieldValue.objects.annotate(text_length=Length("value_text")).filter(text_length__gt=_MAX_CUSTOM_FIELD_TEXT_LENGTH).update(
        value_text=Left("value_text", _MAX_CUSTOM_FIELD_TEXT_LENGTH)
    )


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0096_remove_mapimageoverlay_image_url")]

    operations = [migrations.RunPython(drop_non_http_links, migrations.RunPython.noop)]
