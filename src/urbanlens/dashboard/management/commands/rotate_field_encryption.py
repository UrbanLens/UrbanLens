"""Re-encrypt every ``EncryptedTextField`` column under the currently-active key.

The second half of a key change. Because ``EncryptedTextField`` reads through
every key in ``encryption_keys()`` but writes under the first, adding a new key
alone leaves the database in a mixed state that works but still depends on the
retired key. This command removes that dependency so the old key can be dropped:

    1. Set the new key as ``UL_FIELD_ENCRYPTION_KEY`` and move the old one into
       ``UL_FIELD_ENCRYPTION_KEY_FALLBACKS``. Deploy. Nothing breaks - old rows
       still decrypt under the fallback.
    2. Run this command. Every row is rewritten under the new key.
    3. Remove the old key from ``UL_FIELD_ENCRYPTION_KEY_FALLBACKS``. Deploy.

Skipping step 2 is what turns a routine key change into permanent data loss.

Columns are discovered from the app registry rather than a hard-coded list, so a
field added later is covered automatically - a static list would silently skip
whatever it forgot, which is precisely the failure this command exists to prevent.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from urbanlens.dashboard.models.fields import EncryptedTextField, _fernet, encryption_keys, reset_encryption_keys

if TYPE_CHECKING:
    from django.db.models import Model


def _encrypted_columns() -> list[tuple[str, str, str, str]]:
    """Find every concrete ``EncryptedTextField`` column in the project.

    Returns:
        One ``(label, table, pk_column, column)`` tuple per encrypted column,
        where ``label`` is a human-readable ``Model.field`` identifier.
    """
    found: list[tuple[str, str, str, str]] = []
    model: type[Model]
    for model in apps.get_models():
        meta = model._meta  # noqa: SLF001 - _meta is Django's supported model-introspection API
        if meta.proxy or not meta.managed:
            continue
        pk_field = meta.pk
        if pk_field is None or pk_field.column is None:
            continue
        for field in meta.local_fields:
            # A concrete local field always has a column; the guard is for the
            # type checker, since Field.column is Optional in the stubs.
            if isinstance(field, EncryptedTextField) and field.column is not None:
                label = f"{model.__name__}.{field.name}"
                found.append((label, meta.db_table, pk_field.column, field.column))
    return sorted(found)


class Command(BaseCommand):
    """Rewrite every encrypted column under the active encryption key."""

    help = "Re-encrypt all EncryptedTextField columns under the active field encryption key, so retired keys can be dropped."

    def add_arguments(self, parser: Any) -> None:
        """Register command-line arguments.

        Args:
            parser: The argument parser supplied by Django.
        """
        parser.add_argument("--dry-run", action="store_true", help="Report what would be re-encrypted without writing.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Re-encrypt every discovered column.

        Args:
            *args: Unused positional arguments.
            **options: Parsed command-line options.

        Raises:
            CommandError: When one or more rows could not be decrypted with any
                configured key.
        """
        dry_run: bool = options["dry_run"]

        # Read straight from settings rather than trusting a key set cached
        # earlier in this process (e.g. by an import that touched a model).
        reset_encryption_keys()
        key_count = len(encryption_keys())
        columns = _encrypted_columns()
        self.stdout.write(f"{len(columns)} encrypted column(s) across the project; {key_count} key(s) configured.")
        if key_count == 1:
            self.stdout.write(
                self.style.WARNING(
                    "Only one key is configured, so this is a no-op re-write. If you are rotating, set the new key as UL_FIELD_ENCRYPTION_KEY and the old one in UL_FIELD_ENCRYPTION_KEY_FALLBACKS before running this.",
                ),
            )

        rotated_total = 0
        failures: list[str] = []

        # A dry run only decrypts, so it needs no transaction of its own - and
        # must not open one, since rolling it back would mark an enclosing
        # transaction (a caller's, or a test's) for rollback too.
        with transaction.atomic() if not dry_run else nullcontext():
            for label, table, pk_column, column in columns:
                rotated, column_failures = self._rotate_column(table, pk_column, column, label=label, dry_run=dry_run)
                rotated_total += rotated
                failures.extend(column_failures)
                if rotated:
                    verb = "would re-encrypt" if dry_run else "re-encrypted"
                    self.stdout.write(f"  {label}: {verb} {rotated} value(s).")

        if failures:
            for failure in failures[:20]:
                self.stderr.write(self.style.ERROR(f"  {failure}"))
            if len(failures) > 20:
                self.stderr.write(self.style.ERROR(f"  ... and {len(failures) - 20} more."))
            raise CommandError(
                f"{len(failures)} value(s) could not be decrypted with any configured key and were left untouched. "
                "Add the key they were written under to UL_FIELD_ENCRYPTION_KEY_FALLBACKS and re-run; do not drop any "
                "key until this command completes cleanly.",
            )

        summary = "Would re-encrypt" if dry_run else "Re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"{summary} {rotated_total} value(s) under the active key."))
        if not dry_run and rotated_total:
            self.stdout.write("Retired keys can now be removed from UL_FIELD_ENCRYPTION_KEY_FALLBACKS.")

    def _rotate_column(self, table: str, pk_column: str, column: str, *, label: str, dry_run: bool) -> tuple[int, list[str]]:
        """Re-encrypt every non-empty value in one column.

        Uses a raw cursor rather than the ORM so the stored ciphertext is
        handled directly - going through the model would decrypt on read and
        re-encrypt on write via the same key set, making the rotation invisible
        and any undecryptable row an unhandled exception mid-iteration.

        Args:
            table: Database table name.
            pk_column: Primary key column name, used to target the update.
            column: The encrypted column to rewrite.
            label: Human-readable ``Model.field`` name, for error reporting.
            dry_run: When True, decrypt to verify but do not write.

        Returns:
            A ``(rotated_count, failures)`` pair.
        """
        multi = _fernet()
        rotated = 0
        failures: list[str] = []

        with connection.cursor() as cursor:
            cursor.execute(f"SELECT {pk_column}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''")  # noqa: S608 # nosec B608 - identifiers come from Django's model registry, not user input
            rows = cursor.fetchall()

            for pk, ciphertext in rows:
                try:
                    rotated_value = multi.rotate(ciphertext.encode()).decode()
                except InvalidToken:
                    failures.append(f"{label} [pk={pk}]: no configured key can decrypt this value.")
                    continue
                if not dry_run:
                    cursor.execute(f"UPDATE {table} SET {column} = %s WHERE {pk_column} = %s", [rotated_value, pk])  # noqa: S608 # nosec B608 - identifiers come from Django's model registry, not user input
                rotated += 1

        return rotated, failures
