"""Pinning what the runner would otherwise decide: the environment a test, or a probe it starts, runs as.

An unset ``UL_ENVIRONMENT`` reads as production. CI sets ``testing`` and the dev test-runner sets its own, but a worktree
with no ``.env`` sets nothing, so a test that reads it passed in CI and failed there.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest import mock

from urbanlens.UrbanLens.environments.meta import EnvironmentTypes
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Mapping


def probe_environ(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """*base* (this process's environment by default) with ``UL_ENVIRONMENT`` pinned to ``testing``.

    A child interpreter is not under pytest, so settings classify it by ``UL_ENVIRONMENT`` alone, and production
    refuses to load without a deployment's secrets. Pinned, a probe loads the same settings under every runner.

    Args:
        base: The environment to start from.

    Returns:
        A copy of it, for ``subprocess.run(env=...)``.
    """
    return {**(os.environ if base is None else base), "UL_ENVIRONMENT": "testing"}


class OffProductionMixin:
    """Makes ``AppSettings.environment_name`` ``testing`` for each test.

    For tests of a command that refuses production. A test of that refusal patches its own value over this one.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(app_settings, "environment_name", EnvironmentTypes.TESTING)
        patcher.start()
        self.addCleanup(patcher.stop)
