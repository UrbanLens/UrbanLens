"""Backward-compatibility shim - import from models.badges instead."""
from urbanlens.dashboard.models.badges.queryset import BadgeQuerySet as TagQuerySet, BadgeManager as TagManager
from urbanlens.dashboard.models.badges.model import Badge as Tag, ICON_CHOICES, COLOR_CHOICES
from urbanlens.dashboard.models.badges.customization import BadgeCustomization as TagCustomization
from urbanlens.dashboard.models.badges.serializer import BadgeSerializer as TagSerializer
