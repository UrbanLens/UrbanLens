"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    testcase.py                                                                                          *
*        Path:    /core/tests/testcase.py                                                                              *
*        Project: urbanlens                                                                                            *
*        Version: 1.0.0                                                                                                *
*        Created: 2024-02-19                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2024 Urban Lens                                                                                 *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-02-19     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations
import re

import collections.abc
from typing import Callable, Dict, Iterable, List, Collection, TYPE_CHECKING, NotRequired, Tuple, Any, Optional, NamedTuple, cast
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from django import test

logger = logging.getLogger(__name__)

class TestCases(Iterable):
    entries : list[TestEntry]
    output_callback: Callable[..., Any] | None
    def __init__(self, entries: Iterable[TestEntry | tuple], callback: Callable[..., Any] | None = None):
        self.entries = [TestEntry(*entry) if isinstance(entry, tuple) else entry for entry in entries]
        self.output_callback = callback

    def items(self):
        # Return (key, value) pairs. Ensure callback is triggered.
        for entry in self:
            yield entry.params, entry.expected_output

    def __iter__(self):
        for i, entry in enumerate(self.entries):
            if self.output_callback:
                output = self.output_callback(entry, i)
                yield TestEntry(entry.params, output, entry.message)
            else:
                yield entry
    
    def __getitem__(self, index):
        return self.entries[index]
    
    def __len__(self):
        return len(self.entries)
    
    def __add__(self, other):
        self.entries = self.entries + getattr(other, 'entries',  other)
        return self
    
class TestCasesTemplate(TestCases):

    def __init__(self, entries: Iterable[TestEntry | tuple], substitutions: dict[str, str] | Callable[..., Any], callback: Callable[..., Any] | None = None):
        final_entries = []
        for entry in entries:
            if isinstance(entry, tuple):
                entry = TestEntry(*entry)
            params, expected_output, message = entry

            if isinstance(params, str):
                params = (params,)
            if callable(substitutions):
                # If substitutions is a function, apply it directly
                results = cast(Callable[..., Any], substitutions)(params, expected_output, message)
                final_entries.extend([TestEntry(*result) for result in results])
            else:
                # Apply each substitution to a fresh copy of params and expected_output
                for key, values in cast(dict[str, Any], substitutions).items():
                    for in_value, out_value in values:
                        # Create fresh copies for each substitution
                        substituted_params = [
                            param.replace(key, in_value) if isinstance(param, str) else param
                            for param in params
                        ]
                        substituted_output = expected_output.replace(key, out_value) if isinstance(expected_output, str) else expected_output
                        if len(substituted_params) == 1:
                            substituted_params = substituted_params[0]  # type: ignore[assignment]

                        # Add the substituted entry
                        final_entries.append(TestEntry(substituted_params, substituted_output, message))

        super().__init__(final_entries, callback)

class TestEntry(NamedTuple):
    params: Any | tuple[Any]
    expected_output: Any | None = None
    message: str | None = None


class TestCase(test.TestCase):
    '''
    Provides additional functionality to the django unittest TestCase. 
    
    - Adds a default message to all assertions.
    '''
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
            parts.append(f'{self.module_path}:{self.class_name}')
        if self.method_name is not None:
            parts.append(f'{self.method_name}()')

        if not parts:
            return ""

        return ".".join(parts) + '\n'

    def append_to_failure(self) -> str:
        """
        Append the data to the failure message. Individual tests will override this.
        """
        return ''