
from __future__ import annotations

from typing import Dict, Iterable, List, Collection, TYPE_CHECKING, Tuple, Any, Optional

import logging
import traceback
import unittest

from urbanlens.core.tests.testcase import TestCase

logger = logging.getLogger(__name__)

class MessageResult(unittest.TextTestResult):

    def getDescription(self, test : 'TestCase') -> str:
        """
        Override the default getDescription method to include the class name and method name of the code we're testing

        Args:
            test (TestCase): The test case instance

        Returns:
            str: The description of the test case
        """
        message = super().getDescription(test)
        try:
            return test.create_message(message)
        except AttributeError as e:
            logger.error("TestCase instance %s does not inherit from urbanlens.core.tests.testcase.TestCase: %s", test, e)
            return message
