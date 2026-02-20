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
from lib.models.faker import fake

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
        # Override the DJANGO_SETTINGS_MODULE environment variable
        os.environ['DJANGO_SETTINGS_MODULE'] = 'UrbanLens.settings.test'

        # Apply the new settings
        conf.settings._setup()

        super().setup_test_environment(**kwargs)

        generator = MapGenerator()
        generator.map_generators()

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

class MapGenerator:
    models : list[tuple[Any, str]]

    def __init__(self):
        self.models = [
            # Lib
            (BooleanField, 'lib.models.fields.boolean.BooleanField'),
            (BooleanField, 'lib.models.fields.boolean.ExistsField'),
            (BigIntegerField, 'lib.models.fields.number.BigIntegerField'),
            (CharField, 'lib.models.fields.char.CharField'),
            (DateField, 'lib.models.fields.date.DateField'),
            (DateTimeField, 'lib.models.fields.date.DateTimeField'),
            (DateTimeField, 'lib.models.fields.date.InsertedNowField'),
            (DateTimeField, 'lib.models.fields.date.UpdatedNowField'),
            (DecimalField, 'lib.models.fields.number.DecimalField'),
            (DecimalField, 'lib.models.fields.number.CurrencyField'),
            (FloatField, 'lib.models.fields.number.FloatField'),
            (ForeignKey, 'lib.models.fields.relationships.ForeignKey'),
            (IntegerField, 'lib.models.fields.number.IntegerField'),
            (ManyToManyField, 'lib.models.fields.relationships.ManyToManyField'),
            (OneToOneField, 'lib.models.fields.relationships.OneToOneField'),
            (BooleanField, 'dashboard.models.fields.BooleanField'),
            (BigIntegerField, 'dashboard.models.fields.BigIntegerField'),
            # Dashboard
            (CharField, 'dashboard.models.fields.CharField'),
            (DateField, 'dashboard.models.fields.DateField'),
            (DateTimeField, 'dashboard.models.fields.DateTimeField'),
            (DateTimeField, 'dashboard.models.fields.InsertedNowField'),
            (DateTimeField, 'dashboard.models.fields.UpdatedNowField'),
            (DecimalField, 'dashboard.models.fields.DecimalField'),
            (FloatField, 'dashboard.models.fields.FloatField'),
            (ForeignKey, 'dashboard.models.fields.ForeignKey'),
            (IntegerField, 'dashboard.models.fields.IntegerField'),
            (BigIntegerField, 'dashboard.models.fields.BigIntegerField'),
            (PositiveIntegerField, 'dashboard.models.fields.PositiveIntegerField'),
            (TextField, 'dashboard.models.fields.TextField'),
        ]

    def map_generators(self):
        self.map_models()
        self.map_fields()

    def map_models(self):
        """
        Normal Models:

        Ensure normal fields (that we override) are handled the same way as django models.
        """
        for model, path in self.models:
            baker.generators.add(path, default_mapping[model])

    def map_fields(self):
        fields = {
            generate_emplid:        ['lib.models.fields.EmplIdField',      'urbanlens.dashboard.models.fields.EmplIdField'],
            generate_rowid:         ['lib.models.fields.RowIdField',       'urbanlens.dashboard.models.fields.RowIdField'],
            generate_guid:          ['lib.models.fields.GuidField',        'urbanlens.dashboard.models.fields.GuidField'],
            generate_onechar:       ['lib.models.fields.OneCharField',     'urbanlens.dashboard.models.fields.OneCharField'],
            generate_pickledobject: ['lib.models.fields.PickledObjectField'],
        }

        for generator, module in fields.items():
            for field in module:
                baker.generators.add(field, generator)

def generate_emplid():
    seq('R', start=int(1e7))

def generate_rowid():
    return seq('ROWID', start=int(1e7))

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

def generate_list():
    total_elements = randrange(1, 10)
    return [fake.word() for _ in range(total_elements)]

def generate_pickledobject():
    options = [generate_dict, generate_list]
    index = randrange(0, len(options))
    return options[index]()