# Generic imports
from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class NotificationType(TextChoices):
    TRIP_UPDATED = "trip_updated", "Trip Updated"
    FRIEND_REQUEST = "friend_request", "New Friend Request"
    MESSAGE = "message", "New Message"
    COMMENT_REPLY = "comment_reply", "Reply to Comment"
    COMMENT_LIKED = "comment_liked", "Comment Likes"
    COMMENT_UPLOAD_FAILED = "comment_upload_failed", "Comment Upload Failed"
    FRIEND_ACCEPTED = "friend_accepted", "Friend Request Accepted"
    ADDED_TO_TRIP = "added_to_trip", "Trip Invitation"
    WIKI_UPDATED = "wiki_updated", "Community Wiki Updated"
    PIN_SHARED = "pin_shared", "Pin Shared"
    MAP_SHARED = "map_shared", "Map Shared"
    VISIT_SUGGESTED = "visit_suggested", "Visit Suggested"
    SAFETY_CHECKIN_DUE = "safety_ci_due", "Safety Check-in Due"
    SAFETY_CHECKIN_FINAL_WARNING = "safety_ci_final_warning", "Safety Check-in Final Warning"
    SAFETY_CHECKIN_OVERDUE = "safety_ci_overdue", "Safety Check-in Overdue"
    SAFETY_CHECKIN_RESOLVED = "safety_ci_resolved", "Safety Check-in Resolved"
    SAFETY_CHECKIN_PLAN_UPDATED = "safety_ci_plan_updated", "Safety Check-in Plan Updated"
    WIKI_SAFETY_CHECKIN = "wiki_safety_checkin", "Community Wiki Safety Check-in"
    SAFETY_CHECKIN_PARTNER_INVITE = "safety_ci_partner_invite", "Safety Check-in Partner Request"
    SAFETY_CHECKIN_PARTNER_ACCEPTED = "safety_ci_partner_accepted", "Safety Check-in Partner Accepted"
    ACCOUNT_DELETION_REQUESTED = "account_deletion_requested", "Account Deletion Requested"
    ACCOUNT_DELETION_REMINDER = "account_deletion_reminder", "Account Deletion Reminder"
    AI_EXTRACTION = "ai_extraction", "AI Link Analysis Complete"
    FRIEND_SUGGESTION = "friend_suggestion", "Friend Suggestion"
    SPOTGUESSR_INVITE = "spotguessr_invite", "SpotGuessr Invitation"
    TRIVIA_INVITE = "trivia_invite", "Trivia Invitation"
    CONSENSUS_INVITE = "consensus_invite", "Consensus Invitation"
    PIN_IMPORT_COMPLETE = "pin_import_complete", "Pin Import Complete"
    ACHIEVEMENT_EARNED = "achievement_earned", "Achievement Unlocked"
    ERROR = "error", "Error"
    WARNING = "warning", "Warning"
    INFO = "info", "Info"


#: Notification types a friendship mute must never suppress.
#:
#: Mute is a volume control on someone's social activity - their shares, their
#: comments, their invitations. A safety check-in is not that: the whole point
#: of the feature is that somebody notices when a person does not come back
#: from a site, and a preference set weeks earlier about a friend's chatter is
#: not consent to stop watching for that. The partner invite/accepted pair is
#: here for a related reason - it is an actionable request whose sender is left
#: waiting for an answer that would never be asked for.
#:
#: Listed rather than derived from the ``safety_ci_`` value prefix so that a
#: new safety type is a decision somebody makes; ``test_friendship_mute``
#: fails when one is added and not considered here.
MUTE_EXEMPT_TYPES = frozenset(
    {
        NotificationType.SAFETY_CHECKIN_DUE,
        NotificationType.SAFETY_CHECKIN_FINAL_WARNING,
        NotificationType.SAFETY_CHECKIN_OVERDUE,
        NotificationType.SAFETY_CHECKIN_RESOLVED,
        NotificationType.SAFETY_CHECKIN_PLAN_UPDATED,
        NotificationType.SAFETY_CHECKIN_PARTNER_INVITE,
        NotificationType.SAFETY_CHECKIN_PARTNER_ACCEPTED,
        NotificationType.WIKI_SAFETY_CHECKIN,
    },
)
