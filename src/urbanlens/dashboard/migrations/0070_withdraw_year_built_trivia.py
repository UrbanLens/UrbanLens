"""Withdraw every deterministic year-built trivia question, until the building records say the year is the building's (UrbanLens#321, "a parcel's assessor year").

``services.trivia.deterministic`` asked "What year was <building> built?" from whatever ``year_built`` a parcel's
building record carried. REData 0.3.6 says what that year dates (``year_built_basis``): often it is the assessor's one
year for the parcel's principal improvement, which dates no particular building. Every question asked so far was
asked without knowing, so each is withdrawn here: ``REJECTED``, with the reason the generator reads as "withdrawn".
The generator approves one again, with the building's own year, the next time it finds the parcel's building list
cached and that list dates the building itself. It runs for every candidate location as a trivia session starts.

Rows are withdrawn rather than deleted: votes, ratings and rounds hang off them.

Reverse is a no-op: a withdrawn question cannot be told from one the generator withdrew later.
"""

from django.db import migrations
from django.utils import timezone

#: ``services.trivia.deterministic.YEAR_BUILT_WITHDRAWN``, frozen as it is.
WITHDRAWN = "The building records do not date this building itself."


def withdraw_year_built_questions(apps, schema_editor):
    """Withdraw every approved deterministic question whose ``dedupe_key`` marks it a year-built question."""
    TriviaQuestion = apps.get_model("dashboard", "TriviaQuestion")
    TriviaQuestion.objects.filter(source="deterministic", status="approved", dedupe_key__startswith="year_built:").update(status="rejected", rejection_reason=WITHDRAWN, updated=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0069_calendar_privacy_followups"),
    ]

    operations = [
        migrations.RunPython(code=withdraw_year_built_questions, reverse_code=migrations.RunPython.noop),
    ]
