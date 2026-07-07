from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import CASCADE
from django.db.models.fields import IntegerField, TextField
from django.db.models.fields.related import ForeignKey

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.queryset import Manager


class Review(abstract.DashboardModel):
    rating = IntegerField(validators=[MinValueValidator(0), MaxValueValidator(5)])
    review = TextField()

    # TODO: This should link to Profile, not User
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

    class Meta(abstract.DashboardModel.Meta):
        unique_together = ("user", "pin")
        get_latest_by = "created"
