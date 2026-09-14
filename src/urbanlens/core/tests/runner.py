from __future__ import annotations

import logging
import os
from typing import Any
import unittest

from django import conf
from django.db import connections
from django.test.runner import DiscoverRunner

from urbanlens.core.testing_network import (
    ExternalNetworkGuardVerificationError,
    LocalhostOnlyNetwork,
    verify_external_network_blocked,
)
from urbanlens.core.tests.ai_guard import patched_ai_gateway
from urbanlens.core.tests.result import MessageResult


class BufferingLogHandler(logging.Handler):
    """Buffer records; flush only on failure."""

    def __init__(self):
        super().__init__()
        self.buffer = []

    def emit(self, record):
        self.buffer.append(record)

    def flush_logs(self, condition: bool):
        """Replay buffered records when condition is True."""
        if condition:
            for record in self.buffer:
                logging.getLogger(record.name).handle(record)
        self.buffer.clear()


class QuietTestRunner(unittest.TextTestRunner):
    """Suppress log output for passing tests."""

    def run(self, test):
        """Run with logs buffered."""
        # Copy: removeHandler mutates the same list in place.
        default_handlers = list(logging.root.handlers)
        for handler in default_handlers:
            logging.root.removeHandler(handler)

        log_handler = BufferingLogHandler()
        logging.root.addHandler(log_handler)

        result = super().run(test)

        for handler in default_handlers:
            logging.root.addHandler(handler)
        logging.root.removeHandler(log_handler)

        log_handler.flush_logs(not result.wasSuccessful())

        return result


class TestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs: Any) -> None:
        os.environ["DJANGO_TESTING"] = "1"

        super().setup_test_environment(**kwargs)

        conf.settings.TESTING = True
        conf.settings.UNSAFE_ALLOW_HTTP = True
        conf.settings.SECURE_SSL_REDIRECT = False

        # Same chokepoint list as conftest.py; this hook covers `manage.py test`.
        self._ai_guard = patched_ai_gateway()
        self._ai_guard.__enter__()

        if os.getenv("UL_ALLOW_TEST_INTERNET", "False").lower() not in {"true", "1", "yes"}:
            self._network_guard = LocalhostOnlyNetwork().start()
            try:
                verify_external_network_blocked()
            except ExternalNetworkGuardVerificationError as exc:
                self._network_guard.stop()
                self._network_guard = None
                raise SystemExit(str(exc)) from exc

    def teardown_test_environment(self, **kwargs: Any) -> None:
        network_guard = getattr(self, "_network_guard", None)
        if network_guard:
            network_guard.stop()
        ai_guard = getattr(self, "_ai_guard", None)
        if ai_guard:
            ai_guard.__exit__(None, None, None)
        super().teardown_test_environment(**kwargs)

    def run_suite(self, suite, **kwargs):
        return QuietTestRunner(
            verbosity=self.verbosity,
            failfast=self.failfast,
            resultclass=MessageResult,
            **kwargs,
        ).run(suite)

    def teardown_databases(self, old_config, **kwargs):
        for alias in connections:
            connections[alias].close()

        super().teardown_databases(old_config, **kwargs)
