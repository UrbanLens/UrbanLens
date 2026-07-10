from typing import TYPE_CHECKING

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

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="reviews",
    )
    pin = ForeignKey(
        Pin,
        on_delete=CASCADE,
        related_name="reviews",
    )

    if TYPE_CHECKING:
        profile_id: int
        pin_id: int

    objects = Manager()

    class Meta(abstract.DashboardModel.Meta):
        unique_together = ("profile", "pin")
        get_latest_by = "created"
