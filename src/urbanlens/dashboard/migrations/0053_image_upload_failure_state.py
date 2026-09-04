"""Give a failed upload a state its owner can act on.

``process_image_upload`` marked success and nothing at all on permanent failure,
so a row whose task died kept ``upload_processed_at = None`` forever - the
uploader saw a photo that never finished, with no error and no retry, and
nothing distinguished "still running" from "died three days ago".

``Image.upload_failed_at`` is that missing counterpart, and
``upload_sweep_attempts`` is what stops the recovery sweep re-feeding a
child-killing file to a two-slot sandbox worker every hour. It is incremented by
the sweep rather than by the task: the task legitimately re-runs on
already-processed rows and has its own retry ladder, so counting there would
count several unrelated things at once.

On ``PhotoUploadFailure``: ``image`` is the row to re-run (NULL for a rejected
upload, which never became one, and after a discard - the failure outlives the
photo so its filename can say what went away), ``kind`` separates those two
cases, and ``user_retries`` bounds the retry button.

``kind`` defaults to ``upload_rejected``, which is what every existing row is,
so the five call sites that predate it stay correct with no data migration.

The unique constraint is the idempotence mechanism, deliberately in place of a
read-then-create: the sweep and any later ``task_failure`` receiver can both
reach the recorder for one row, and a pre-read races between them.
"""


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0052_wiki_edit_consensus_points'),
    ]

    operations = [
        migrations.AddField(
            model_name='image',
            name='upload_failed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='image',
            name='upload_sweep_attempts',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='photouploadfailure',
            name='image',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='upload_failures', to='dashboard.image'),
        ),
        migrations.AddField(
            model_name='photouploadfailure',
            name='kind',
            field=models.CharField(choices=[('upload_rejected', 'Upload rejected'), ('processing_failed', 'Processing failed')], default='upload_rejected', max_length=20),
        ),
        migrations.AddField(
            model_name='photouploadfailure',
            name='user_retries',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name='photouploadfailure',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'pending')), fields=('image',), name='uq_photo_fail_pending_image'),
        ),
    ]
