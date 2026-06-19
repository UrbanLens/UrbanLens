"""Tests for environment configuration classes and factory.

No database access — pure Pydantic model logic.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from urbanlens.UrbanLens.environments.base import BaseEnvironment
from urbanlens.UrbanLens.environments.dev import Development
from urbanlens.UrbanLens.environments.factory import select_environment
from urbanlens.UrbanLens.environments.local import Local
from urbanlens.UrbanLens.environments.meta import DebugTypes, EnvironmentTypes
from urbanlens.UrbanLens.environments.prod import Production
from urbanlens.UrbanLens.environments.staging import Staging
from urbanlens.UrbanLens.environments.test import Testing


_HYP = dict(max_examples=50, deadline=None)


class DebugResolutionTests(unittest.TestCase):
	"""BaseEnvironment.debug resolves correctly from override and default."""

	def test_override_on_forces_debug_true_despite_true_default(self) -> None:
		env = Local(debug_override=DebugTypes.OVERRIDE_ON)
		self.assertTrue(env.debug)

	def test_override_on_forces_debug_true_despite_false_default(self) -> None:
		env = Staging(debug_override=DebugTypes.OVERRIDE_ON)
		self.assertTrue(env.debug)

	def test_override_off_forces_debug_false_despite_true_default(self) -> None:
		env = Development(debug_override=DebugTypes.OVERRIDE_OFF)
		self.assertFalse(env.debug)

	def test_override_off_forces_debug_false_despite_false_default(self) -> None:
		env = Staging(debug_override=DebugTypes.OVERRIDE_OFF)
		self.assertFalse(env.debug)

	def test_default_uses_true_when_debug_default_is_true(self) -> None:
		env = Development()  # debug_default=True, override=DEFAULT
		self.assertTrue(env.debug)

	def test_default_uses_false_when_debug_default_is_false(self) -> None:
		env = Staging()  # debug_default=False, override=DEFAULT
		self.assertFalse(env.debug)

	def test_production_debug_is_always_false(self) -> None:
		self.assertFalse(Production().debug)

	@given(st.booleans())
	@settings(**_HYP)
	def test_override_on_dominates_any_default(self, use_true_default: bool) -> None:
		env = Local(debug_override=DebugTypes.OVERRIDE_ON) if use_true_default else Staging(debug_override=DebugTypes.OVERRIDE_ON)
		self.assertTrue(env.debug)

	@given(st.booleans())
	@settings(**_HYP)
	def test_override_off_dominates_any_default(self, use_true_default: bool) -> None:
		env = Local(debug_override=DebugTypes.OVERRIDE_OFF) if use_true_default else Staging(debug_override=DebugTypes.OVERRIDE_OFF)
		self.assertFalse(env.debug)


class BaseEnvironmentEqualityTests(unittest.TestCase):
	"""BaseEnvironment.__eq__ compares against EnvironmentTypes, str, and other BaseEnvironment."""

	def test_eq_with_matching_env_type_enum(self) -> None:
		self.assertEqual(Local(), EnvironmentTypes.LOCAL)

	def test_neq_with_different_env_type_enum(self) -> None:
		self.assertNotEqual(Local(), EnvironmentTypes.PROD)

	def test_eq_with_matching_lowercase_string(self) -> None:
		self.assertEqual(Local(), "local")

	def test_eq_with_matching_uppercase_string(self) -> None:
		self.assertEqual(Local(), "LOCAL")

	def test_eq_case_insensitive_mixed_string(self) -> None:
		self.assertEqual(Local(), "LoCaL")

	def test_neq_with_different_string(self) -> None:
		self.assertNotEqual(Local(), "prod")

	def test_eq_with_same_type_environment(self) -> None:
		self.assertEqual(Local(), Local())

	def test_neq_with_different_type_environment(self) -> None:
		self.assertNotEqual(Local(), Development())

	def test_eq_against_unsupported_type_returns_not_implemented(self) -> None:
		self.assertEqual(Local().__eq__(42), NotImplemented)

	def test_eq_against_none_returns_not_implemented(self) -> None:
		self.assertEqual(Local().__eq__(None), NotImplemented)

	@given(st.sampled_from(list(EnvironmentTypes)))
	@settings(**_HYP)
	def test_env_equals_its_own_env_type(self, env_type: EnvironmentTypes) -> None:
		env = select_environment(env_type)
		self.assertEqual(env, env_type)

	@given(st.sampled_from(list(EnvironmentTypes)))
	@settings(**_HYP)
	def test_env_equals_its_own_string_value(self, env_type: EnvironmentTypes) -> None:
		env = select_environment(env_type)
		self.assertEqual(env, env_type.value)


class BaseEnvironmentValidatorTests(unittest.TestCase):
	"""Field validators coerce string values into proper enum types."""

	def test_env_type_coerced_from_lowercase_string(self) -> None:
		env = Local(env_type="local")
		self.assertIsInstance(env.env_type, EnvironmentTypes)
		self.assertEqual(env.env_type, EnvironmentTypes.LOCAL)

	def test_debug_override_coerced_from_override_on_string(self) -> None:
		env = Local(debug_override="override_on")
		self.assertIsInstance(env.debug_override, DebugTypes)
		self.assertEqual(env.debug_override, DebugTypes.OVERRIDE_ON)

	def test_debug_override_coerced_from_override_off_string(self) -> None:
		env = Local(debug_override="override_off")
		self.assertEqual(env.debug_override, DebugTypes.OVERRIDE_OFF)

	def test_debug_override_coerced_from_default_string(self) -> None:
		env = Local(debug_override="default")
		self.assertEqual(env.debug_override, DebugTypes.DEFAULT)

	@given(st.sampled_from(list(DebugTypes)))
	@settings(**_HYP)
	def test_debug_override_enum_values_accepted_as_strings(self, debug_type: DebugTypes) -> None:
		env = Local(debug_override=debug_type.value)
		self.assertEqual(env.debug_override, debug_type)


class BaseEnvironmentReprTests(unittest.TestCase):
	"""__str__ and __repr__ include usable information."""

	def test_str_contains_environment_name(self) -> None:
		env = Local()
		self.assertIn("local", str(env).lower())

	def test_repr_contains_name_field(self) -> None:
		env = Local()
		self.assertIn("local", repr(env).lower())

	def test_repr_contains_env_type(self) -> None:
		env = Local()
		self.assertIn("local", repr(env).lower())

	@given(st.sampled_from(list(EnvironmentTypes)))
	@settings(**_HYP)
	def test_str_is_non_empty_for_all_env_types(self, env_type: EnvironmentTypes) -> None:
		env = select_environment(env_type)
		self.assertTrue(str(env))


class ProductionEnvironmentTests(unittest.TestCase):
	"""Production always has debug=False and the correct metadata."""

	def test_debug_is_false(self) -> None:
		self.assertFalse(Production().debug)

	def test_is_public(self) -> None:
		self.assertTrue(Production().is_public)

	def test_in_network(self) -> None:
		self.assertTrue(Production().in_network)

	def test_env_type_is_prod(self) -> None:
		self.assertEqual(Production().env_type, EnvironmentTypes.PROD)

	def test_debug_override_is_override_off(self) -> None:
		self.assertEqual(Production().debug_override, DebugTypes.OVERRIDE_OFF)

	def test_name_is_production(self) -> None:
		self.assertIn("Production", Production().name)


class SelectEnvironmentTests(unittest.TestCase):
	"""select_environment returns the correct subclass for each EnvironmentTypes."""

	def test_local_returns_local(self) -> None:
		self.assertIsInstance(select_environment(EnvironmentTypes.LOCAL), Local)

	def test_dev_returns_development(self) -> None:
		self.assertIsInstance(select_environment(EnvironmentTypes.DEV), Development)

	def test_test_returns_testing(self) -> None:
		self.assertIsInstance(select_environment(EnvironmentTypes.TEST), Testing)

	def test_staging_returns_staging(self) -> None:
		self.assertIsInstance(select_environment(EnvironmentTypes.STAGING), Staging)

	def test_prod_returns_production(self) -> None:
		self.assertIsInstance(select_environment(EnvironmentTypes.PROD), Production)

	def test_accepts_string_input(self) -> None:
		self.assertIsInstance(select_environment("local"), Local)

	def test_string_dev_returns_development(self) -> None:
		self.assertIsInstance(select_environment("dev"), Development)

	def test_env_var_is_used_when_none_passed(self) -> None:
		with patch.dict(os.environ, {"UL_ENVIRONMENT": "dev"}):
			self.assertIsInstance(select_environment(None), Development)

	def test_default_used_when_no_env_var_set(self) -> None:
		stripped = {k: v for k, v in os.environ.items() if k != "UL_ENVIRONMENT"}
		with patch.dict(os.environ, stripped, clear=True):
			result = select_environment(None, default=EnvironmentTypes.LOCAL)
			self.assertIsInstance(result, Local)

	def test_env_var_staging_returns_staging(self) -> None:
		with patch.dict(os.environ, {"UL_ENVIRONMENT": "staging"}):
			self.assertIsInstance(select_environment(None), Staging)

	@given(st.sampled_from(list(EnvironmentTypes)))
	@settings(**_HYP)
	def test_every_env_type_produces_a_base_environment(self, env_type: EnvironmentTypes) -> None:
		result = select_environment(env_type)
		self.assertIsInstance(result, BaseEnvironment)

	@given(st.sampled_from(list(EnvironmentTypes)))
	@settings(**_HYP)
	def test_result_env_type_matches_input(self, env_type: EnvironmentTypes) -> None:
		result = select_environment(env_type)
		self.assertEqual(result.env_type, env_type)
