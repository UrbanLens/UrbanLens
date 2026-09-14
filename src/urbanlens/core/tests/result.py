from __future__ import annotations

import logging
import unittest

from urbanlens.core.tests.testcase import TestCase

logger = logging.getLogger(__name__)


class MessageResult(unittest.TextTestResult):
    def getDescription(self, test: TestCase) -> str:
        """Include the class and method name in the test description.

        Args:
            test (TestCase): The test case instance

        Returns:
            str: The description of the test case
        """
        message = super().getDescription(test)
        try:
            return test.create_message(message)
        except AttributeError as e:
            logger.exception(
                "TestCase instance %s does not inherit from urbanlens.core.tests.testcase.TestCase: %s", test, e
            )
            return message
