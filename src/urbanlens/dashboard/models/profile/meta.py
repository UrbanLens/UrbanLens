from urbanlens.dashboard.models.abstract.choices import TextChoices


class VisibilityChoice(TextChoices):
    """Who can see a particular piece of profile data, or who can perform an action.

    Members are declared least → most restrictive; the settings page renders
    them in this order. Accepted friends qualify for every option except
    NO_ONE (enforced by each evaluator, e.g. ``Profile.visibility_permits``).
    """

    ANYONE = "anyone", "Anyone (Logged In)"
    ANYTHING_IN_COMMON = "anything_in_common", "Users with anything in common"
    COMMON_PIN = "common_pin", "Users with a pin in common"
    COMMON_FRIEND = "common_friend", "Users with a friend in common"
    COMMON_TRIP = "common_trip", "Users with a trip in common"
    FRIENDS = "friends", "Friends Only"
    NO_ONE = "no_one", "No one"


class MapViewChoice(TextChoices):
    STREET = "street", "Street"
    SATELLITE = "satellite", "Satellite"
    TOPOGRAPHIC = "topographic", "Topographic"
    REMEMBER = "remember", "Remember"


class MapCenterMode(TextChoices):
    AUTO = "auto", "Center on my pins"
    GPS = "gps", "Use my current location"
    CUSTOM = "custom", "Custom location"
    REMEMBER = "remember", "Remember last position"


class ThemeChoice(TextChoices):
    SYSTEM = "system", "System (follows your OS)"
    LIGHT = "light", "Light"
    DARK = "dark", "Dark"


class DistanceUnit(TextChoices):
    """Preferred unit for displaying distances and travel statistics."""

    KILOMETERS = "km", "Kilometers"
    MILES = "mi", "Miles"


class GuidanceLevel(TextChoices):
    """How in-app help is shown: walkthrough cards and/or hover hints."""

    ALL = "all", "Guides & hints"
    TOOLTIPS = "tooltips", "Hints only"
    NONE = "none", "Off"
