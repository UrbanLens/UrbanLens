from rest_framework import serializers

from urbanlens.dashboard.models.profile.model import Profile


class ProfileSerializer(serializers.ModelSerializer):
    """Serializer for a user's own profile.

    Exposes a deliberately small, self-service subset of Profile. Account
    settings, privacy options, and contact details are managed through the
    dedicated settings pages, not this API.
    """

    class Meta:
        model = Profile
        fields = ["user", "created", "updated", "avatar", "bio", "area"]
        # ``user`` must never be writable: reassigning it would hand the
        # profile (and everything hanging off it) to a different account.
        read_only_fields = ["user"]
        extra_kwargs = {
            "avatar": {"read_only": True},
        }
