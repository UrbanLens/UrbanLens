"""Record how a player left a SpotGuessr or Trivia game under way, so they stay on its scoreboard and in their history (P198).

Rows that left before this have no record of whether they had joined, or of a kick. A departed row with a guess or
answer in its session certainly played, so it becomes ``left``; one that left before answering anything stays blank.
"""

from django.db import migrations, models
from django.db.models import Exists, OuterRef

_DEPARTURE_CHOICES = [("left", "Left"), ("removed", "Removed")]


def _backfill_departures(apps, schema_editor):
    GameSessionParticipant = apps.get_model("dashboard", "GameSessionParticipant")
    Guess = apps.get_model("dashboard", "Guess")
    TriviaSessionParticipant = apps.get_model("dashboard", "TriviaSessionParticipant")
    TriviaAnswer = apps.get_model("dashboard", "TriviaAnswer")

    GameSessionParticipant.objects.filter(status="left", departure="").filter(
        Exists(Guess.objects.filter(round__session_id=OuterRef("session_id"), profile_id=OuterRef("profile_id"))),
    ).update(departure="left")
    TriviaSessionParticipant.objects.filter(status="left", departure="").filter(
        Exists(TriviaAnswer.objects.filter(round__session_id=OuterRef("session_id"), profile_id=OuterRef("profile_id"))),
    ).update(departure="left")


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0043_media_keys_without_tracking_params'),
    ]

    operations = [
        migrations.AddField(
            model_name='gamesessionparticipant',
            name='departure',
            field=models.CharField(blank=True, choices=_DEPARTURE_CHOICES, default='', max_length=10),
        ),
        migrations.AddField(
            model_name='triviasessionparticipant',
            name='departure',
            field=models.CharField(blank=True, choices=_DEPARTURE_CHOICES, default='', max_length=10),
        ),
        migrations.RunPython(_backfill_departures, migrations.RunPython.noop, elidable=True),
    ]
