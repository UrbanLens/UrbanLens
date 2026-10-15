from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import unittest

from urbanlens.core.tests.testcase import _MessagePrefixMixin

if TYPE_CHECKING:
    from unittest.runner import _TextTestStream

    # The stream type TextTestRunner hands its result class; unparameterized, the base would take its narrower default.
    _TextTestResult = unittest.TextTestResult[_TextTestStream]
else:
    _TextTestResult = unittest.TextTestResult

logger = logging.getLogger(__name__)


class MessageResult(_TextTestResult):
    def getDescription(self, test: unittest.TestCase) -> str:
        """Include the class and method name in the test description."""
        message = super().getDescription(test)
        if isinstance(test, _MessagePrefixMixin):
            return test.create_message(message)
        logger.error("TestCase instance %s does not inherit from urbanlens.core.tests.testcase.TestCase", test)
        return message
