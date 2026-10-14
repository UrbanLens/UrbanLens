"""
Hand the paid-subscription models to the billing plugin's app, which adopts their tables.

State only: the tables stay where they are until the billing app's first migration renames them, and are left alone
where it is not installed. The Fact check constraint has existed since 0010 created it in SQL; this declares it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0077_role_pricing_out_of_core"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="billingcustomer",
                    name="user",
                ),
                migrations.RemoveField(
                    model_name="rolepricing",
                    name="role",
                ),
                migrations.RemoveConstraint(
                    model_name="rolesubscription",
                    name="unique_active_role_subscription",
                ),
                migrations.DeleteModel(
                    name="StripeProcessedRefund",
                ),
                migrations.DeleteModel(
                    name="StripeWebhookEvent",
                ),
                migrations.AddConstraint(
                    model_name="fact",
                    constraint=models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("image__isnull", True), ("location__isnull", False), ("wiki__isnull", True)),
                            models.Q(("image__isnull", True), ("location__isnull", True), ("wiki__isnull", False)),
                            models.Q(("image__isnull", False), ("location__isnull", True), ("wiki__isnull", True)),
                            _connector="OR",
                        ),
                        name="db_fact_exactly_one_subject",
                    ),
                ),
                migrations.DeleteModel(
                    name="BillingCustomer",
                ),
                migrations.DeleteModel(
                    name="RolePricing",
                ),
                migrations.DeleteModel(
                    name="RoleSubscription",
                ),
            ],
        ),
    ]
