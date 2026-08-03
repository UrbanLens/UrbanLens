
from __future__ import annotations

import logging

from django import test
from hypothesis.extra.django._impl import HypothesisTestCase as _HypothesisMixin

logger = logging.getLogger(__name__)


class _MessagePrefixMixin:
    """
    Shared message-prefixing behavior for our custom TestCase/SimpleTestCase variants.
    """
    # Deprecated, in favor of fn. Named with a leading underscore (unlike a
    # plain "target"/"method_name") because those are exactly the attribute
    # names test subclasses reach for on their own domain objects (see e.g.
    # ProfileDetailVisibilityTests.target, a Profile) - an un-prefixed name
    # here shadowed those, forcing mypy to widen every such attribute to
    # "type | None" and flag every subsequent access as a union-attr error.
    _message_target: type | None = None
    # Deprecated, in favor of fn
    _message_method_name: str | None = None

    @property
    def class_name(self) -> str | None:
        """
        Get the class name of the test case

        Returns:
            str | None: The class name of the test case
        """
        if self._message_target is None:
            return None
        return self._message_target.__name__

    @property
    def module_path(self) -> str | None:
        """
        Get the path to the module for the target class

        Returns:
            str | None: The module path (e.g. "core.tests.testcase")
        """
        if self._message_target is None:
            return None
        return self._message_target.__module__

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
        if self._message_method_name is not None:
            parts.append(f"{self._message_method_name}()")

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
    - Runs ``setUp``/``tearDown`` inside hypothesis's per-example transaction
      for ``@given`` tests (see :meth:`setup_example`).

    Use this for tests that need database access (model creation, ORM
    queries, the Django test client, etc). Each test runs in its own
    transaction that is rolled back afterwards.
    """

    def _is_hypothesis_test(self) -> bool:
        """
        Whether the test method currently running is decorated with ``@given``.

        Returns:
            bool: True if hypothesis drives this test method's fixtures.
        """
        method = getattr(self, self._testMethodName, None)
        return bool(getattr(method, "is_hypothesis_test", False))

    # unittest's own casing; both are one-liners upstream (``self.setUp()`` /
    # ``self.tearDown()``) and Django does not override either, so replacing
    # rather than delegating to them is safe.
    def _callSetUp(self) -> None:  # noqa: N802
        """Defer ``setUp`` to :meth:`setup_example` for a ``@given`` test."""
        if self._is_hypothesis_test():
            return
        self.setUp()

    def _callTearDown(self) -> None:  # noqa: N802
        """Defer ``tearDown`` to :meth:`teardown_example` for a ``@given`` test."""
        if self._is_hypothesis_test():
            return
        self.tearDown()

    def setup_example(self) -> None:
        """
        Enter hypothesis's per-example transaction, then run ``setUp`` inside it.

        ``hypothesis.extra.django``'s mixin routes ``@given`` tests through
        ``unittest.TestCase.__call__``, bypassing Django's usual
        ``_pre_setup``/``_post_teardown`` wrapper; hypothesis calls those per
        *example* instead, here and in :meth:`teardown_example`. ``setUp`` is
        still called once by ``unittest``'s ``run()`` though - before the first
        example, so outside every per-example transaction. Rows it wrote landed
        in the class-level atomic and survived to ``tearDownClass``, leaking
        into every later test in the class: any class mixing a row-writing
        ``setUp`` with a ``@given`` test saw the *next* test fail on a unique
        constraint (``RoundTripCommentsTests`` tripped
        ``dashboard_locations_latitude_longitude_uniq`` on a second ``Location``
        at the same coordinates).

        Running it here instead puts ``setUp`` back inside the transaction that
        gets rolled back, matching the guarantee non-hypothesis tests have.
        """
        super().setup_example()
        if self._is_hypothesis_test():
            self.setUp()

    def teardown_example(self, example) -> None:
        """
        Run ``tearDown`` and cleanups, then roll the example's transaction back.

        Cleanups are drained per example so that patchers started in ``setUp``
        don't stack up across examples.

        Args:
            example: The example hypothesis just finished, passed through.
        """
        if self._is_hypothesis_test():
            self.tearDown()
            self.doCleanups()
        super().teardown_example(example)


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
