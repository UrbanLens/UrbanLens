from rest_framework import serializers

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review


class ReviewSerializer(serializers.ModelSerializer):
    """Serializer for a user's review (rating) of a pin."""

    class Meta:
        model = Review
        fields = ["id", "profile", "pin", "rating"]
        # Ownership is assigned server-side from request.user; a writable
        # profile field would let a user post reviews as someone else.
        read_only_fields = ["profile"]

    def __init__(self, *args, **kwargs):
        """Restrict the writable ``pin`` field to the requesting user's pins.

        An unrestricted queryset would let a user attach reviews to (and
        thereby enumerate) other users' pins by guessing sequential ids.
        """
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            self.fields["pin"].queryset = Pin.objects.filter(profile__user=request.user)
        else:
            self.fields["pin"].queryset = Pin.objects.none()
