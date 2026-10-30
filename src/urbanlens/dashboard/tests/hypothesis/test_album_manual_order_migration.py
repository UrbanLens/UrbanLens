"""Regression tests for preserving album ordering intent in the v0.8 migration."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import MagicMock

from django.db import migrations
from django.test import SimpleTestCase

_MIGRATION = import_module("urbanlens.dashboard.migrations.0032_v0_8_0")


class AlbumManualOrderMigrationTests(SimpleTestCase):
    def test_manual_order_is_converted_before_the_column_is_removed(self) -> None:
        add_sort = self._operation_index(migrations.AddField, model_name="album", name="sort")
        preserve = self._operation_index(migrations.RunPython, code=_MIGRATION._0032_preserve_album_manual_order)
        remove_manual_order = self._operation_index(migrations.RemoveField, model_name="album", name="manual_order")

        self.assertLess(add_sort, preserve)
        self.assertLess(preserve, remove_manual_order)

    def test_manual_ordered_albums_become_custom_sorted(self) -> None:
        album_manager = MagicMock()
        filtered = album_manager.filter.return_value
        apps = MagicMock()
        apps.get_model.return_value = type("HistoricalAlbum", (), {"objects": album_manager})

        _MIGRATION._0032_preserve_album_manual_order(apps, None)

        apps.get_model.assert_called_once_with("dashboard", "Album")
        album_manager.filter.assert_called_once_with(manual_order=True)
        filtered.update.assert_called_once_with(sort="custom")

    def test_custom_sorted_albums_restore_manual_order_on_reverse(self) -> None:
        album_manager = MagicMock()
        filtered = album_manager.filter.return_value
        apps = MagicMock()
        apps.get_model.return_value = type("HistoricalAlbum", (), {"objects": album_manager})

        _MIGRATION._0032_restore_album_manual_order(apps, None)

        apps.get_model.assert_called_once_with("dashboard", "Album")
        album_manager.filter.assert_called_once_with(sort="custom")
        filtered.update.assert_called_once_with(manual_order=True)

    def _operation_index(self, operation_type: type, **attrs: object) -> int:
        for index, operation in enumerate(_MIGRATION.Migration.operations):
            if isinstance(operation, operation_type) and all(
                getattr(operation, name, None) is value or getattr(operation, name, None) == value
                for name, value in attrs.items()
            ):
                return index
        message = f"Could not find {operation_type.__name__} with attributes {attrs!r}."
        raise AssertionError(message)
