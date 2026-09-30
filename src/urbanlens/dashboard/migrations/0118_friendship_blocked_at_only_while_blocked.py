from django.db import migrations, models


class Migration(migrations.Migration):
    """Separate from 0115 so the constraint is not added in the transaction that just rewrote the rows."""

    dependencies = [
        ('dashboard', '0117_friendship_blocked_at'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='friendship',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('blocked_at__isnull', False), ('status', 'Blocked')), models.Q(models.Q(('status', 'Blocked'), _negated=True), ('blocked_at__isnull', True)), _connector='OR'), name='friendship_blocked_at_only_while_blocked'),
        ),
    ]
