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

from typing import Callable, Dict, Iterable, List, Collection, TYPE_CHECKING, NotRequired, Tuple, Any, Optional, NamedTuple
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from django import test

logger = logging.getLogger(__name__)

class TestCases(Iterable):
    entries : list[TestEntry]
    output_callback : Callable
    def __init__(self, entries : Iterable[TestEntry | tuple], callback : Callable | None = None):
        self.entries = [TestEntry(*entry) if isinstance(entry, tuple) else entry for entry in entries]
        self.output_callback = callback

    def items(self):
        # Return key : value pairs. Ensure callback is triggered.
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

    def __init__(self, entries: Iterable[TestEntry | tuple], substitutions: dict[str, str] | Callable, callback: Callable | None = None):
        final_entries = []
        for entry in entries:
            if isinstance(entry, tuple):
                entry = TestEntry(*entry)
            params, expected_output, message = entry

            if isinstance(params, str):
                params = (params,)
            if isinstance(substitutions, Callable):
                # If substitutions is a function, apply it directly
                results = substitutions(params, expected_output, message)
                final_entries.extend([TestEntry(*result) for result in results])
            else:
                # Apply each substitution to a fresh copy of params and expected_output
                for key, values in substitutions.items():
                    for in_value, out_value in values:
                        # Create fresh copies for each substitution
                        substituted_params = [
                            param.replace(key, in_value) if isinstance(param, str) else param
                            for param in params
                        ]
                        substituted_output = expected_output.replace(key, out_value) if isinstance(expected_output, str) else expected_output
                        if len(substituted_params) == 1:
                            substituted_params = substituted_params[0]

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
        ----------------------------------------------------------------------

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

class BasicTestCaseMeta(type):
    def __new__(cls, name, bases, attrs):
        test_cases = attrs.get('test_cases', {})
        for case_name, data in test_cases.items():
            method_name = re.sub(r'[^a-zA-Z0-9_]+', '_', f'test_{case_name}')
            
            # This ensures each dynamically generated method has its unique `data`
            def create_test_method(data):
                def test_method(self):
                    self.run_test_loop(data)
                return test_method

            if method_name not in attrs:
                attrs[method_name] = create_test_method(data)
                attrs[method_name].__name__ = method_name
                attrs[method_name].__doc__ = f"Procedural test for {case_name}"

        return super().__new__(cls, name, bases, attrs)

class BasicTestCase(TestCase, metaclass=BasicTestCaseMeta):
    '''
    Provides additional functionality to the django unittest TestCase. 
    
    - Adds a default message to all assertions.
    - Adds a run_test_loop method to run a series of test cases with minimal boilerplate.
    '''
    patches: dict[str, str] = {}
    patch_list : dict[str, MagicMock | AsyncMock] = {}
    fn : Callable | None = None
    default_message : str = 'Test {i} - {fn}({params}) failed -> \n    Returned {output_type}:\n        "{output}". \n    Expected {expected_type}:\n        "{expected}"'
    test_cases : dict[str, TestCases] = {}
    _loop_count : int = 0

    @property
    def class_name(self) -> str | None:
        """
        Get the class name of the test case

        Returns:
            str | None: The class name of the test case
        """
        if self.target is None:
            if self.fn is None:
                return None
            return self.fn.__name__
        return self.target.__name__

    @property
    def module_path(self) -> str | None:
        """
        Get the path to the module for the target class

        Returns:
            str | None: The module path (e.g. "core.tests.testcase")
        """
        if self.target is None:
            if self.fn is None:
                return None
            return self.fn.__module__
        return self.target.__module__
    
    def setUp(self):
        """
        Set up the test case
        """
        super().setUp()
        self.set_targets()
        self.set_patches()
        self.after_setup()

    def tearDown(self):
        """
        Tear down the test case
        """
        super().tearDown()
        self._loop_count = 0

    def set_targets(self):
        """
        Set the targets for the test case. This method will be removed as soon as self.target and self.method_name (deprecated attributes) are removed from the codebase.

        Therefore, this method is also deprecated.
        """
        if self.target is None and self.fn is not None:
            # If the fn is a function (not a class method), set the target to the module the function is in
            if not hasattr(self.fn, '__self__'):
                self.target = self.fn.__module__
            else:
                # Set the target to the class the method is defined in
                self.target = self.fn.__class__

        if self.method_name is None and self.fn is not None:
            self.method_name = self.fn.__name__

    def set_patches(self):
        """
        Set up the patches for the test case
        """
        for patch_name, patch_path in self.patches.items():
            patcher = patch(patch_path)
            self.patch_list[patch_name] = patcher.start()
            self.addCleanup(patcher.stop)

    def patch_return(self, patch_name: str, return_value: Any):
        """
        Patch a method to return a specific value

        Args:
            patch_name (str): The name of the method to patch
            return_value (Any): The value to return when the method is called
        """
        if patch_name not in self.patch_list:
            raise ValueError(f"Patch {patch_name} not found in patch_list")
        
        self.patch_list[patch_name].return_value = return_value

    def get_patch(self, patch_name: str) -> MagicMock | AsyncMock:
        """
        Get a patch from the patch list

        Args:
            patch_name (str): The name of the patch to get

        Returns:
            MagicMock | AsyncMock: The patch from the patch list
        """
        return self.patch_list[patch_name]

    def after_setup(self):
        """
        Run after setting the patches. Subclasses will override this.

        NOTE: The base method in TestCase (here) should always be left empty, so that subclasses are not required to call super().after_setup()
        """
        # Do not implement this here
        pass

    def run_test_loop(self, test_cases: TestCases, message : str | None = None):
        """
        Test a series of test entries

        Args:
            test_cases (TestCases): A tuple of data to use for each test case in our loop
            message (str, optional): If provided, will be used as the default message for each test case. This will be overridden if a message is defined for a specific test case.
        """
        self._loop_count = 0
        if not isinstance(test_cases, TestCases):
            if not isinstance(test_cases, list):
                raise ValueError(f"test_cases must be a TestCases object or a list of test cases. Got {type(test_cases)}")
            test_cases = TestCases(test_cases)

        for entry in test_cases:
            self._loop_count += 1
            self.run_single_test(entry, message)

    def setup_single_test(self, entry : TestEntry):
        """
        Set up a single test case. Subclasses will override this.

        NOTE: The base method in TestCase (here) should always be left empty, so that subclasses are not required to call super().setup_single_test()

        Args:
            input (Any): The input to the test case
            expected (Any): The expected output of the test case
        """
        # Do not implement this here
        pass

    def run_single_test(self, entry : TestEntry, default_message : str | None = None) -> None:
        """
        Test a single test case. Subclasses may override this, or the setup_single_test() method, to modify this behavior.

        Args:
            name (str): The name of the test case
            params (Any): The input to the test case
            expected_output (Any): The expected output of the test case
        """
        if self.fn is None:
            raise ValueError("No function to test. Hint: Set self.fn to the function you want to test with run_test_loop()")
        
        self.setup_single_test(entry)

        (params, expected_output, message) = entry

        if not message:
            message = default_message or self.default_message
        
        if not isinstance(params, tuple):
            params = (params,)

        result = self.fn(*params)
        message = self.format_test_message(message, params, expected_output, result)
        if entry.expected_output is None:
            return self.assertIsNone(result, msg=message)
        
        return self.assertEqual(result, expected_output, msg=message)

    def format_test_message(self, message : str | None, params : tuple[Any], expected_output : Any, real_output : Any) -> str:
        """
        Format the test message, by replacing variables inside the str with our params or output

        Args:
            message (str | None): The message to use for the test case
            params (tuple[Any]): The input to the test case
            expected_output (Any): The expected output of the test case

        Returns:
            str: The formatted test message
        """
        if not message:
            return None

        # Replace {params} with the input to the test case
        message = message.replace('{fn}', self.fn.__name__)
        message = message.replace('{params}', str(params)[:100])
        message = message.replace('{expected}', str(expected_output)[:100])
        message = message.replace('{expected_type}', str(type(expected_output))[:50])
        message = message.replace('{output}', str(real_output)[:100])
        message = message.replace('{output_type}', str(type(real_output))[:50])
        message = message.replace('{i}', str(self._loop_count))

        return message