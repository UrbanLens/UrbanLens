
from __future__ import annotations

import logging

from django import test
from hypothesis.extra.django._impl import HypothesisTestCase as _HypothesisMixin

logger = logging.getLogger(__name__)


class _MessagePrefixMixin:
    """
    Shared message-prefixing behavior for our custom TestCase/SimpleTestCase variants.
    """
    # Deprecated, in favor of fn
    target: type | None = None
    # Deprecated, in favor of fn
    method_name: str | None = None

    @property
    def class_name(self) -> str | None:
        """
        Get the class name of the test case

        Returns:
            str | None: The class name of the test case
        """
        if self.target is None:
            return None
        return self.target.__name__

    @property
    def module_path(self) -> str | None:
        """
        Get the path to the module for the target class

        Returns:
            str | None: The module path (e.g. "core.tests.testcase")
        """
        if self.target is None:
            return None
        return self.target.__module__

    def create_message(self, msg: str) -> str:
        """
        Prepend the class name and method name to the message, so we know what code in our project was being tested

        This appears in the header of the test output. e.g.:

        ======================================================================
        FAIL: test_example_foo (dashboard.tests.models.example.FooTestCase)
        Test that something or another is true

        Args:
            msg (str): The message to prepend

        Returns:
            str: The message with the class name and method name prepended
        """
        prefix = self.get_message_prefix()

        if msg is None:
            output = f"{prefix} failed"
        else:
            output = f"{prefix}{msg}"

        if suffix := self.append_to_failure():
            hr = "\n      - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -\n"
            output += f"{hr}\n{suffix}\n{hr}"

        return output

    def get_message_prefix(self) -> str:
        """
        Get the prefix to prepend to the message

        Returns:
            str: The prefix to prepend to the message (e.g. "core.tests.testcase:TestCase.test_my_method()")
        """
        parts = []
        if self.class_name is not None:
            parts.append(f"{self.module_path}:{self.class_name}")
        if self.method_name is not None:
            parts.append(f"{self.method_name}()")

        if not parts:
            return ""

        return ".".join(parts) + "\n"

    def append_to_failure(self) -> str:
        """
        Append the data to the failure message. Individual tests will override this.
        """
        return ""


class TestCase(_MessagePrefixMixin, _HypothesisMixin, test.TestCase):
    """
    Provides additional functionality to the django unittest TestCase.

    - Adds a default message to all assertions.

    Use this for tests that need database access (model creation, ORM
    queries, the Django test client, etc). Each test runs in its own
    transaction that is rolled back afterwards.
    """


class SimpleTestCase(_MessagePrefixMixin, _HypothesisMixin, test.SimpleTestCase):
    """
    Provides additional functionality to the django unittest SimpleTestCase.

    - Adds a default message to all assertions.

    Use this for tests that never touch the database - pure functions,
    serializer/form field validation without saving, parsing, etc. It skips
    the per-test transaction wrapping that TestCase pays for, so prefer it
    whenever a test doesn't need the database. Django raises
    ``DatabaseOperationForbidden`` if a test accidentally performs a query.
    """
