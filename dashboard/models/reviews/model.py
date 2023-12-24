from django.db import models
from django.contrib.auth.models import User
from dashboard.models.locations.model import Location

class Review(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    rating = models.IntegerField()
    review = models.TextField()

    class Meta:
        unique_together = ('user', 'location')
