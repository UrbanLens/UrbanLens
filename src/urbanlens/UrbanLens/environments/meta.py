from __future__ import annotations

from enum import StrEnum


class EnvironmentTypes(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"
    STAGING = "staging"
    LOCAL = "local"


class DebugTypes(StrEnum):
    OVERRIDE_ON = "override_on"
    OVERRIDE_OFF = "override_off"
    DEFAULT = "default"
