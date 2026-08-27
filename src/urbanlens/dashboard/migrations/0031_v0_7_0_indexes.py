from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0030_v0_7_0"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        # idxdb_album_pin, idxdb_album_wiki, idxdb_albumitem_album,
        # idxdb_albumitem_image and idx_floorplan_element_kind are deliberately not
        # here: 0030_v0_7_0.py's squashed range also contains a later migration
        # (0045_drop_duplicate_fk_indexes) that removes each of them as a redundant
        # FK index. Splitting their AddIndex into this later-running migration while
        # the RemoveIndex stayed in 0030 made 0030 try to remove an index that,
        # from a fresh database, does not exist yet - `ValueError: No index named
        # idxdb_album_pin on model Album`. Since nothing runs between the add and
        # the remove, the pair is a pure no-op; both sides were deleted rather than
        # reordered.
        migrations.AddIndex(
            model_name="image",
            index=models.Index(
                fields=["profile", "quota_exempt_reason"],
                name="idxdb_image_profile_quota",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplan",
            index=models.Index(
                fields=["place", "profile", "valid_from"],
                name="idx_floorplan_place_owner_date",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanmarker",
            index=models.Index(
                fields=["connector_id"], name="idx_floorplan_marker_connector"
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanwall",
            index=models.Index(
                fields=["floor", "kind"], name="idx_floorplan_wall_kind"
            ),
        ),
    ]
