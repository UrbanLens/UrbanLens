"""Migration: merge Category model into Tag (kind discriminator).

Strategy:
1. Add kind field to Tag (default 'tag').
2. Add temporary M2M fields pin.categories_new and location.categories_new
   pointing at Tag with the new related names.
3. Copy all Category rows into Tag with kind='category', preserving M2M data.
4. Drop old M2M fields (pin.categories → dashboard_categories,
   location.categories → dashboard_categories).
5. Rename the temp fields to categories.
6. Delete the Category model (drops dashboard_categories table).
"""

from __future__ import annotations

from django.db import migrations, models


def copy_categories_to_tags(apps, schema_editor):
    """Copy every Category row into Tag with kind='category'.

    Also migrates Pin.categories and Location.categories M2M data from the
    old Category-backed through-table to the new Tag-backed through-table.
    """
    Category = apps.get_model("dashboard", "Category")
    Tag = apps.get_model("dashboard", "Tag")
    Pin = apps.get_model("dashboard", "Pin")
    Location = apps.get_model("dashboard", "Location")

    # Map old category_id → new tag_id so we can migrate M2M rows.
    id_map: dict[int, int] = {}

    for cat in Category.objects.prefetch_related("parents").all():
        tag, _ = Tag.objects.get_or_create(
            kind="category",
            name=cat.name,
            defaults={
                "description": cat.description,
                "color": cat.color,
                "icon": cat.icon,
                "order": cat.order,
                "profile": None,  # categories are always global
            },
        )
        id_map[cat.id] = tag.id

    # Wire up parent relationships between the newly-created Tag rows.
    for cat in Category.objects.prefetch_related("parents").all():
        tag = Tag.objects.get(id=id_map[cat.id])
        parent_tag_ids = [id_map[p.id] for p in cat.parents.all() if p.id in id_map]
        if parent_tag_ids:
            tag.parents.add(*Tag.objects.filter(id__in=parent_tag_ids))

    # Migrate Pin.categories (old → categories_new on Pin)
    for pin in Pin.objects.prefetch_related("categories").all():
        new_ids = [id_map[c.id] for c in pin.categories.all() if c.id in id_map]
        if new_ids:
            pin.categories_new.set(Tag.objects.filter(id__in=new_ids))

    # Migrate Location.categories (old → categories_new on Location)
    for loc in Location.objects.prefetch_related("categories").all():
        new_ids = [id_map[c.id] for c in loc.categories.all() if c.id in id_map]
        if new_ids:
            loc.categories_new.set(Tag.objects.filter(id__in=new_ids))

    # The M2M inserts above leave pending deferred FK trigger events on
    # dashboard_tags. PostgreSQL refuses to CREATE INDEX on a table with
    # pending triggers, so we flush them now before the schema editor's
    # deferred CREATE INDEX runs at the end of the migration transaction.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def noop_reverse(apps, schema_editor):
    """Reverting this migration is not supported (data is merged)."""


class Migration(migrations.Migration):
    """Merge Category model into Tag using kind='category' discriminator."""

    dependencies = [
        ("dashboard", "0038_profile_cluster_radius"),
    ]

    operations = [
        # 1. Add kind discriminator to Tag.
        migrations.AddField(
            model_name="tag",
            name="kind",
            field=models.CharField(
                choices=[("tag", "Tag"), ("category", "Category")],
                db_index=True,
                default="tag",
                max_length=20,
            ),
        ),
        # 2a. Temporary Pin → Tag M2M (will become pin.categories).
        migrations.AddField(
            model_name="pin",
            name="categories_new",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"kind": "category"},
                related_name="categorized_pins",
                to="dashboard.tag",
            ),
        ),
        # 2b. Temporary Location → Tag M2M (will become location.categories).
        migrations.AddField(
            model_name="location",
            name="categories_new",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"kind": "category"},
                related_name="categorized_locations",
                to="dashboard.tag",
            ),
        ),
        # 3. Copy Category rows → Tag rows + migrate M2M data.
        migrations.RunPython(copy_categories_to_tags, noop_reverse),
        # 4a. Drop old Pin.categories (points at dashboard_categories).
        migrations.RemoveField(model_name="pin", name="categories"),
        # 4b. Drop old Location.categories (points at dashboard_categories).
        migrations.RemoveField(model_name="location", name="categories"),
        # 5a. Rename temp field to categories on Pin.
        migrations.RenameField(model_name="pin", old_name="categories_new", new_name="categories"),
        # 5b. Rename temp field to categories on Location.
        migrations.RenameField(model_name="location", old_name="categories_new", new_name="categories"),
        # 6. Drop Category model (and dashboard_categories table).
        migrations.DeleteModel(name="Category"),
    ]
