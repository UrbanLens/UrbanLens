
from urbanlens.dashboard.models.abstract.choices import TextChoices


class VisibilityChoice(TextChoices):
    """Who can see a particular piece of profile data, or who can perform an action."""

    ANYONE = "anyone", "Anyone (Logged In)"
    FRIENDS = "friends", "Friends Only"
    COMMON_PIN = "common_pin", "Users with a pin in common"
    COMMON_FRIEND = "common_friend", "Users with a friend in common"
    COMMON_TRIP = "common_trip", "Users with a trip in common"
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


class GuidanceLevel(TextChoices):
    """How in-app help is shown: walkthrough cards and/or hover hints."""

    ALL = "all", "Guides & hints"
    TOOLTIPS = "tooltips", "Hints only"
    NONE = "none", "Off"
