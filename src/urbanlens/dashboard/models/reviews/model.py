from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import CASCADE
from django.db.models.fields import IntegerField, TextField
from django.db.models.fields.related import ForeignKey

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.queryset import Manager


class Review(abstract.Model):
    rating = IntegerField(validators=[MinValueValidator(0), MaxValueValidator(5)])
    review = TextField()

    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name="reviews",
    )
    pin = ForeignKey(
        Pin,
        on_delete=CASCADE,
        related_name="reviews",
    )

    objects = Manager()

    class Meta(abstract.Model.Meta):
        unique_together = ("user", "pin")
        get_latest_by = "created"
