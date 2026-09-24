from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0100_wiki_edit_boundary_revisions")]

    operations = [
        migrations.AddIndex(
            model_name="boundaryrevision",
            index=models.Index(fields=["wiki", "boundary_type", "-id"], name="idxdb_bndrev_wiki_type"),
        ),
    ]
