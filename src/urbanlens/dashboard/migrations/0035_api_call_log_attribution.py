import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0034_trip_comment_parent_deleted")]
    operations = [
        migrations.AddField(
            model_name="apicalllog",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                help_text="Whose behalf this call was made on, from the actor bound for the request. Null for the site's own scheduled work, which is nobody's consumption, and for a call whose account has since been deleted - the usage stays in the record, unattributed.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="api_calls",
                to="dashboard.profile",
            ),
        ),
        migrations.AddIndex(model_name="apicalllog", index=models.Index(fields=["service", "created", "profile"], name="idxdb_apilog_svc_cdt_prf")),
    ]
