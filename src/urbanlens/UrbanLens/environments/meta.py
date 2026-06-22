from __future__ import annotations

from enum import StrEnum


class EnvironmentTypes(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"
    STAGING = "staging"
    LOCAL = "local"


class DebugTypes(StrEnum):
    OVERRIDE_ON = "override_on"
    OVERRIDE_OFF = "override_off"
    DEFAULT = "default"
