"""Tests for the abstract Serializer's context-driven field inclusion/exclusion.

No database access - all tests exercise the serializer's __init__ and classmethods
in isolation using in-process DRF serializers.
"""
from __future__ import annotations

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st
from rest_framework import serializers as drf_serializers

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.abstract.serializer import Serializer


_hyp = settings(max_examples=50, deadline=None)


# -- Concrete test serializer subclass -----------------------------------------

class _TestSerializer(Serializer):
    """Minimal concrete serializer used to exercise include/exclude logic."""

    name = drf_serializers.CharField(required=False, default="")
    score = drf_serializers.IntegerField(required=False, default=0)
    tag = drf_serializers.CharField(required=False, default="")

    class Meta(Serializer.Meta):
        fields = ["id", "name", "score", "tag"]
        generated_fields: list[str] = []


class _GeneratedSerializer(Serializer):
    """Serializer that declares some fields as 'generated' (computed, not native)."""

    value = drf_serializers.IntegerField(required=False, default=0)
    computed = drf_serializers.SerializerMethodField()

    def get_computed(self, obj) -> str:
        return ""

    class Meta(Serializer.Meta):
        fields = ["id", "value", "computed"]
        generated_fields: list[str] = ["computed"]


# -- No-context: all fields present --------------------------------------------

class SerializerNoContextTests(SimpleTestCase):
    """Without context, all declared fields are included."""

    def test_all_fields_present_with_no_context(self) -> None:
        s = _TestSerializer()
        self.assertSetEqual(set(s.fields.keys()), {"id", "name", "score", "tag"})

    def test_all_fields_present_with_empty_context(self) -> None:
        s = _TestSerializer(context={})
        self.assertSetEqual(set(s.fields.keys()), {"id", "name", "score", "tag"})


# -- exclude_fields -------------------------------------------------------------

class SerializerExcludeFieldsTests(SimpleTestCase):
    """context['exclude_fields'] removes the named fields from the serializer."""

    def test_single_field_excluded(self) -> None:
        s = _TestSerializer(context={"exclude_fields": ["score"]})
        self.assertNotIn("score", s.fields)
        self.assertIn("id", s.fields)
        self.assertIn("name", s.fields)
        self.assertIn("tag", s.fields)

    def test_multiple_fields_excluded(self) -> None:
        s = _TestSerializer(context={"exclude_fields": ["name", "score"]})
        self.assertNotIn("name", s.fields)
        self.assertNotIn("score", s.fields)
        self.assertIn("id", s.fields)
        self.assertIn("tag", s.fields)

    def test_empty_exclude_list_keeps_all_fields(self) -> None:
        s = _TestSerializer(context={"exclude_fields": []})
        self.assertSetEqual(set(s.fields.keys()), {"id", "name", "score", "tag"})

    def test_excluding_all_fields_leaves_empty(self) -> None:
        s = _TestSerializer(context={"exclude_fields": ["id", "name", "score", "tag"]})
        self.assertEqual(len(s.fields), 0)

    def test_excluding_non_existent_field_is_harmless(self) -> None:
        s = _TestSerializer(context={"exclude_fields": ["does_not_exist"]})
        self.assertSetEqual(set(s.fields.keys()), {"id", "name", "score", "tag"})

    @given(st.lists(st.sampled_from(["name", "score", "tag"]), min_size=0, max_size=3, unique=True))
    @_hyp
    def test_excluded_fields_are_absent(self, excluded: list[str]) -> None:
        s = _TestSerializer(context={"exclude_fields": excluded})
        for field in excluded:
            self.assertNotIn(field, s.fields)

    @given(st.lists(st.sampled_from(["name", "score", "tag"]), min_size=0, max_size=3, unique=True))
    @_hyp
    def test_non_excluded_fields_remain(self, excluded: list[str]) -> None:
        all_fields = {"id", "name", "score", "tag"}
        s = _TestSerializer(context={"exclude_fields": excluded})
        for field in (all_fields - set(excluded)):
            self.assertIn(field, s.fields)


# -- include_fields -------------------------------------------------------------

class SerializerIncludeFieldsTests(SimpleTestCase):
    """context['include_fields'] restricts the serializer to only those fields."""

    def test_single_field_included(self) -> None:
        s = _TestSerializer(context={"include_fields": ["name"]})
        self.assertIn("name", s.fields)
        self.assertNotIn("id", s.fields)
        self.assertNotIn("score", s.fields)
        self.assertNotIn("tag", s.fields)

    def test_multiple_fields_included(self) -> None:
        s = _TestSerializer(context={"include_fields": ["id", "score"]})
        self.assertIn("id", s.fields)
        self.assertIn("score", s.fields)
        self.assertNotIn("name", s.fields)
        self.assertNotIn("tag", s.fields)

    def test_include_all_fields_keeps_all(self) -> None:
        s = _TestSerializer(context={"include_fields": ["id", "name", "score", "tag"]})
        self.assertSetEqual(set(s.fields.keys()), {"id", "name", "score", "tag"})

    @given(st.lists(st.sampled_from(["id", "name", "score", "tag"]), min_size=1, max_size=4, unique=True))
    @_hyp
    def test_only_included_fields_are_present(self, included: list[str]) -> None:
        s = _TestSerializer(context={"include_fields": included})
        self.assertSetEqual(set(s.fields.keys()), set(included))

    @given(
        st.lists(st.sampled_from(["id", "name", "score", "tag"]), min_size=1, max_size=4, unique=True),
    )
    @_hyp
    def test_included_count_matches_input(self, included: list[str]) -> None:
        s = _TestSerializer(context={"include_fields": included})
        self.assertEqual(len(s.fields), len(included))


# -- get_fieldnames and get_native_fields ---------------------------------------

class SerializerClassMethodTests(SimpleTestCase):
    """get_fieldnames() and get_native_fields() return the expected field lists."""

    def test_get_fieldnames_returns_meta_fields(self) -> None:
        self.assertEqual(_TestSerializer.get_fieldnames(), ["id", "name", "score", "tag"])

    def test_get_fieldnames_base_class_returns_id_only(self) -> None:
        self.assertEqual(Serializer.get_fieldnames(), ["id"])

    def test_get_native_fields_excludes_generated(self) -> None:
        native = _GeneratedSerializer.get_native_fields()
        self.assertNotIn("computed", native)
        self.assertIn("id", native)
        self.assertIn("value", native)

    def test_get_native_fields_when_no_generated_matches_all(self) -> None:
        # _TestSerializer has no generated_fields, so native == all fields.
        native = _TestSerializer.get_native_fields()
        self.assertIn("id", native)
        self.assertIn("name", native)
        self.assertIn("score", native)
        self.assertIn("tag", native)


# -- TDD: get_native_fields has a mutation bug ---------------------------------

class SerializerGetNativeFieldsMutationBugTests(SimpleTestCase):
    """TDD: get_native_fields() must be idempotent - repeated calls should return the same result.

    Currently it calls get_fieldnames() which returns the actual Meta.fields list by
    reference, then mutates it via list.remove().  On the second call, the generated
    field is already gone from Meta.fields, so get_fieldnames() also returns fewer
    fields.  This test documents the correct expected behaviour.
    """

    _ORIGINAL_FIELDS = ["id", "value", "computed"]

    def setUp(self):
        # Hardcode the reset - don't read from the class, which may already be
        # mutated by earlier tests (SerializerClassMethodTests runs first).
        _GeneratedSerializer.Meta.fields = list(self._ORIGINAL_FIELDS)

    def tearDown(self):
        _GeneratedSerializer.Meta.fields = list(self._ORIGINAL_FIELDS)

    @unittest.expectedFailure
    def test_get_native_fields_does_not_shrink_meta_fields(self) -> None:
        """get_native_fields() must not alter the length of Meta.fields.

        Currently FAILS: get_fieldnames() returns the Meta.fields list by reference,
        and get_native_fields() calls .remove() on it, permanently shrinking it.
        Fix: return a copy in get_fieldnames().
        """
        count_before = len(_GeneratedSerializer.Meta.fields)
        _GeneratedSerializer.get_native_fields()
        count_after = len(_GeneratedSerializer.Meta.fields)
        self.assertEqual(count_before, count_after)

    @unittest.expectedFailure
    def test_get_fieldnames_not_mutated_after_get_native_fields(self) -> None:
        """get_fieldnames() must not be affected by a prior get_native_fields() call.

        Currently FAILS: get_native_fields() mutates the same list object that
        get_fieldnames() returns via cls.Meta.fields (no copy is made).
        """
        before = list(_GeneratedSerializer.get_fieldnames())
        _GeneratedSerializer.get_native_fields()
        after = list(_GeneratedSerializer.get_fieldnames())
        self.assertEqual(before, after)
