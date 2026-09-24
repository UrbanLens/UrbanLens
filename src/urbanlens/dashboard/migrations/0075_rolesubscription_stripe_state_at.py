from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0066_safetycheckincontact_email_normalized_index")]

    operations = [
        migrations.AddField(
            model_name="rolesubscription",
            name="stripe_state_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Stripe-side time of the latest subscription state applied here: an event's created time, or when a live retrieve was sent. Older subscription events are ignored.",
                null=True,
            ),
        ),
        migrations.RemoveConstraint(model_name="rolesubscription", name="unique_active_role_subscription"),
        migrations.AddConstraint(
            model_name="rolesubscription",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("canceled", "incomplete_expired")), _negated=True),
                fields=("user", "role"),
                name="unique_active_role_subscription",
            ),
        ),
    ]
