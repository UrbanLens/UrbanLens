"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    runner.py                                                                                            *
*        Path:    /core/tests/runner.py                                                                                *
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

from random import randrange
from typing import Any, TYPE_CHECKING, List, Tuple
import os
import unittest
import logging
from unittest.mock import patch

from django import conf
from django.test.runner import DiscoverRunner
from django.db import connections

# Django imports
from django.db.models import (BigIntegerField, BinaryField, BooleanField,
                              CharField, DateField, DateTimeField,
                              DecimalField, DurationField, EmailField,
                              FileField, FloatField, ForeignKey,
                              GenericIPAddressField, ImageField, IntegerField,
                              IPAddressField, ManyToManyField, OneToOneField,
                              PositiveSmallIntegerField,
                              SlugField, SmallIntegerField, TextField,
                              TimeField, URLField, UUIDField, PositiveIntegerField)

# 3rd Party imports
from model_bakery import baker, seq
from model_bakery.generators import default_mapping

from urbanlens.core.tests.result import MessageResult
from faker import Faker

class BufferingLogHandler(logging.Handler):
    """
    A logging handler that buffers log records and only outputs them
    under certain conditions, such as when a test fails.
    """
    def __init__(self):
        super().__init__()
        self.buffer = []

    def emit(self, record):
        self.buffer.append(record)

    def flush_logs(self, condition: bool):
        """
        Output buffered log records if condition is True.
        """
        if condition:
            for record in self.buffer:
                logging.getLogger(record.name).handle(record)
        self.buffer.clear()

class QuietTestRunner(unittest.TextTestRunner):
    """
    A test runner that suppresses log output when tests pass.
    """
    def run(self, test):
        """
        Wrap the super().run(test) call with log suppression logic.
        """
        # Remove all existing handlers
        default_handlers = logging.root.handlers
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        # Before running the test, add the custom log handler to root logger.
        log_handler = BufferingLogHandler()
        logging.root.addHandler(log_handler)

        result = super().run(test)

        # Add the handlers back
        for handler in default_handlers:
            logging.root.addHandler(handler)
        logging.root.removeHandler(log_handler)

        # Determine if the test(s) passed and conditionally flush the log buffer.
        test_passed = result.wasSuccessful()
        log_handler.flush_logs(not test_passed)

        return result

class TestRunner(DiscoverRunner):

    def setup_test_environment(self, **kwargs : 'Any') -> None:
        # Set env var first — checked by signals before settings are fully loaded.
        os.environ['DJANGO_TESTING'] = '1'

        super().setup_test_environment(**kwargs)

        # Mark settings as test mode for any code that checks settings.TESTING.
        conf.settings.TESTING = True

        # Patch the AI gateway so no test ever makes a real external API call.
        # send_prompt is the single chokepoint shared by all LLMGateway subclasses.
        self._ai_patcher = patch(
            'urbanlens.dashboard.services.ai.gateway.LLMGateway.send_prompt',
            return_value=None,
        )
        self._ai_patcher.start()

    def teardown_test_environment(self, **kwargs: 'Any') -> None:
        patcher = getattr(self, '_ai_patcher', None)
        if patcher:
            patcher.stop()
        super().teardown_test_environment(**kwargs)

    def run_suite(self, suite, **kwargs):
        # Run the test suite
        return QuietTestRunner(
            verbosity=self.verbosity,
            failfast=self.failfast,
            resultclass=MessageResult,
            **kwargs
        ).run(suite)

    def teardown_databases(self, old_config, **kwargs):
        # Explicitly close the database connections
        for alias in connections:
            connections[alias].close()

        # Teardown the databases
        super().teardown_databases(old_config, **kwargs)

fake = Faker()

def generate_guid():
    prefix = randrange(10000000, 99999999)
    return seq(f'{prefix}-0000-0000-0000-', start=int(1e12))

def generate_onechar():
    return chr(randrange(65, 91))

def generate_uniquechar():
    return seq('A', 'Z')

def generate_dict():
    total_elements = randrange(1, 10)
    result = {fake.word(): fake.word() for _ in range(total_elements)}
    return result

def generate_list():
    total_elements = randrange(1, 10)
    return [fake.word() for _ in range(total_elements)]

def generate_pickledobject():
    options = [generate_dict, generate_list]
    index = randrange(0, len(options))
    return options[index]()