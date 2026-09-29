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


def _repair_owned_links(model, owner):
    """Repair each link's url; one that becomes a url its owner already links folds into that link."""
    from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

    for link in model.objects.all().iterator():
        try:
            cleaned = clean_link_url(link.url, max_length=_MAX_LINK_URL_LENGTH)
        except InvalidLinkUrlError:
            link.delete()
            continue
        if cleaned == link.url:
            continue
        existing = model.objects.filter(**{owner: getattr(link, f"{owner}_id")}, url=cleaned).exclude(pk=link.pk).first()
        if existing is None:
            model.objects.filter(pk=link.pk).update(url=cleaned)
            continue
        filled = {field: getattr(link, field) for field in ("name", "wayback_url") if not getattr(existing, field) and getattr(link, field)}
        if filled:
            model.objects.filter(pk=existing.pk).update(**filled)
        link.delete()


def repair_links(apps, schema_editor):
    for model_name, owner in (("PinLink", "pin"), ("WikiLink", "wiki")):
        model = apps.get_model("dashboard", model_name)
        _repair_owned_links(model, owner)
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
