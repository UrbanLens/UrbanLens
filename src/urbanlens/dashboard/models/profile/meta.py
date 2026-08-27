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


class SyncAliasesDirection(TextChoices):
    """Which direction(s) newly-added aliases sync between a pin and its wiki.

    Sync is additive only in both directions (never deletes) and only fires
    for genuine new aliases, never edits to an existing one - see
    ``models.aliases.signals``.
    """

    OFF = "off", "Off"
    TO_WIKI = "to_wiki", "Sync to wiki"
    FROM_WIKI = "from_wiki", "Sync from wiki"
    BOTH = "both", "Sync both ways"


class ConsentPreferenceWording:
    """Shared label text for the interaction-preference choice fields below.

    A plain mixin, not itself an ``Enum`` - ``TextChoices`` can't be
    subclassed once it already defines members, so members can't be shared
    by inheritance the way regular class attributes can. Mixing this class
    in instead lets the *wording* common to several fields (e.g. "Please ask
    first") be edited in one place while each field still declares its own
    member set.
    """

    YES_PLEASE = "Yes, please."
    PLEASE_DONT = "Please don't"
    PLEASE_ASK_FIRST = "Please ask first"
    WITHOUT_FACE = "Only when my face isn't visible"
    OTHER_SPECIFY = "Other (please specify below)"


class PhotoTakingPreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile consents to having their photo taken, on or off this site."""

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    NO = "no", ConsentPreferenceWording.PLEASE_DONT
    WITHOUT_FACE = "without_face", ConsentPreferenceWording.WITHOUT_FACE
    ASK_FIRST = "ask_first", ConsentPreferenceWording.PLEASE_ASK_FIRST
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY


class PhotoSharingPreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile consents to having their photo shared or posted, on or off this site."""

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    NO = "no", ConsentPreferenceWording.PLEASE_DONT
    WITHOUT_FACE = "without_face", ConsentPreferenceWording.WITHOUT_FACE
    BLUR_FACE = "blur_face", "Please blur my face"
    ASK_FIRST = "ask_first", ConsentPreferenceWording.PLEASE_ASK_FIRST
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY


class PhotoTaggingPreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile consents to being tagged or identified in photos, on or off this site."""

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    NO = "no", ConsentPreferenceWording.PLEASE_DONT
    WITHOUT_FACE = "without_face", ConsentPreferenceWording.WITHOUT_FACE
    ASK_FIRST = "ask_first", ConsentPreferenceWording.PLEASE_ASK_FIRST
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY


class PhotoUsagePreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile consents to others using their photos, on or off this site."""

    FEEL_FREE = "feel_free", "Feel free!"
    WITH_ATTRIBUTION = "with_attribution", "With Attribution"
    TELL_ME_ABOUT_IT = "tell_me_about_it", "As long as you tell me about it."
    ASK_FIRST = "ask_first", "Ask first"
    NO = "no", ConsentPreferenceWording.PLEASE_DONT
    OTHER = "other", "Other (specify...)"


class FriendRequestPreference(ConsentPreferenceWording, TextChoices):
    """When this profile welcomes friend requests from other users.

    A social preference statement only - who is actually permitted to send a
    request is the separate ``friend_request_visibility`` setting.
    """

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    MET_IN_PERSON = "met_in_person", "Only if we've met in person"
    TALK_REGULARLY = "talk_regularly", "Only if we talk regularly"
    MUTUAL_FRIENDS = "mutual_friends", "Only if we have mutual friends"
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY


class MeetupPreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile is interested in meeting other users in person."""

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    DISLIKE = "dislike", "I do not like meetups"
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY


class ExploringWithOthersPreference(ConsentPreferenceWording, TextChoices):
    """Whether this profile prefers to explore locations solo or with others."""

    YES = "yes", ConsentPreferenceWording.YES_PLEASE
    PREFER_ALONE = "prefer_alone", "I prefer exploring alone."
    OTHER = "other", ConsentPreferenceWording.OTHER_SPECIFY
