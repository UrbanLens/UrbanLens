"""Rejoin the migration graph after a parallel-branch merge.

Two lines of work each added migrations from ``0045``: this branch's
``0046_emailtype_email_verification`` and the other's
``0049_passkey_unlock_wraps_and_prompt_snooze``. Both are already applied
wherever their own branch ran, so neither can be renumbered - the graph simply
needs a single leaf again, which is all this does.

No operations: the two lines touch different models, so there is nothing to
reconcile beyond the ordering.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0046_emailtype_email_verification"),
        ("dashboard", "0049_passkey_unlock_wraps_and_prompt_snooze"),
    ]

    operations = []
