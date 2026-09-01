"""Provenance for a photo copied from a wiki onto the copier's own pin.

Adds ``copied_from``/``copied_from_profile``/``copied_from_location``/``copied_from_label`` and a
new ``QuotaExemption.WIKI_COPY`` value. See ``models.images.model.Image`` for why the FKs are
``SET_NULL`` and ``copied_from_label`` is a plain-text snapshot rather than always resolved live,
and ``services.photos.wiki_copy.copy_wiki_photo_to_pin`` for what populates them.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Add wiki-copy provenance fields and index to Image."""

    dependencies = [
        ("dashboard", "0046_profile_keyboard_shortcuts"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="copied_from",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="copies", to="dashboard.image"),
        ),
        migrations.AddField(
            model_name="image",
            name="copied_from_label",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="image",
            name="copied_from_location",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.location"),
        ),
        migrations.AddField(
            model_name="image",
            name="copied_from_profile",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.profile"),
        ),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(blank=True, choices=[("external_media", "Cached external media"), ("community", "Community-valued contribution"), ("shared_copy", "Copy of a shared photo"), ("deduplicated", "Same file already stored for this user"), ("wiki_copy", "Copy of a wiki photo")], default="", max_length=20),
        ),
        migrations.AddIndex(
            model_name="image",
            index=models.Index(fields=["profile", "copied_from_profile"], name="idxdb_img_profile_copied_from"),
        ),
    ]
