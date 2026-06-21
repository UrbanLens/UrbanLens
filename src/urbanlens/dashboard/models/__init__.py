"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    __init__.py                                                                                          *
*        Path:    /dashboard/models/__init__.py                                                                        *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

# Abstract Base Classes
from urbanlens.dashboard.models.abstract import Model, QuerySet, Manager, ViewSet, Serializer
from urbanlens.dashboard.models.categories import Category
from urbanlens.dashboard.models.comments import Comment
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.models.friendship import Friendship
from urbanlens.dashboard.models.images import Image
from urbanlens.dashboard.models.pin import Pin, PinNote
from urbanlens.dashboard.models.location import Location
from urbanlens.dashboard.models.notifications import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.reviews import Review
from urbanlens.dashboard.models.badges import Badge, BadgeCustomization, ICON_CHOICES, COLOR_CHOICES, BadgeSerializer
from urbanlens.dashboard.models.cache import GeocodedLocation
from urbanlens.dashboard.models.trips import Trip, TripActivity, TripComment
from urbanlens.dashboard.models.campus import Campus
from urbanlens.dashboard.models.social_link import SocialLink
from urbanlens.dashboard.models.visits import PinVisit, VisitSource
from urbanlens.dashboard.models.aliases import PinAlias, LocationAlias
from urbanlens.dashboard.models.markup import MarkupType, PinMarkup
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.reactions import Reaction
from urbanlens.dashboard.models.profile.note import ProfileNote
from urbanlens.dashboard.models.badges.profile_assignment import ProfileBadgeAssignment
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation