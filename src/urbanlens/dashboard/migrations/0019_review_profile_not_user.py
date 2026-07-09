"""Point Review at Profile instead of User.

Every other user-owned model in this app (Pin, PinShare, badges, comments,
safety records, ...) links to ``Profile``; ``Review`` was the one outlier
(tracked by a long-standing TODO on the field). This backfills the new
``profile`` column from the existing ``user`` FK before dropping ``user``, so
no review data is lost.
"""

from django.db import migrations, models
import django.db.models.deletion


def backfill_review_profile(apps, schema_editor):
    Review = apps.get_model("dashboard", "Review")
    Profile = apps.get_model("dashboard", "Profile")
    profile_by_user_id = dict(Profile.objects.values_list("user_id", "id"))
    reviews = []
    for review in Review.objects.all().iterator():
        review.profile_id = profile_by_user_id[review.user_id]
        reviews.append(review)
    Review.objects.bulk_update(reviews, ["profile_id"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0018_image_exif_data_image_file_size_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="review",
            name="profile",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reviews",
                to="dashboard.profile",
            ),
        ),
        migrations.RunPython(backfill_review_profile, migrations.RunPython.noop, elidable=True),
        migrations.AlterUniqueTogether(
            name="review",
            unique_together=set(),
        ),
        migrations.RemoveField(
            model_name="review",
            name="user",
        ),
        migrations.AlterField(
            model_name="review",
            name="profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reviews",
                to="dashboard.profile",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="review",
            unique_together={("profile", "pin")},
        ),
    ]
