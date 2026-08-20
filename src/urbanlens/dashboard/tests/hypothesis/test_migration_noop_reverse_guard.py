"""Fail the build when a new data migration reverses to ``noop`` without a reason.

``RunPython.noop`` as a reverse is a claim: "undoing this migration needs no
data work". That is usually true - a backfill's column is dropped by the schema
reverse anyway, a seeded row can stay, a cache invalidation has nothing to undo.

It was false twice, in the worst way available. Migrations 0039 and 0007 encrypt
existing columns **in place**; their ``noop`` reverses meant ``migrate dashboard
0038`` *succeeded* while leaving ciphertext in columns the pre-migration code
reads as plaintext. Nothing failed, and the data was unreadable. Both now carry
real decrypting reverses (audit chunk 459-460).

The distinction that matters is not "does the reverse restore the old values" -
plenty of data migrations are inherently lossy, and that is fine. It is:

    **After reversing, can the pre-migration code still interpret the data?**

A merge, a dedupe, a cap, a flag reset all leave values that are lossy but
valid. A format change - encryption, an encoding, a serialisation - does not.
Only the second kind must never reverse to ``noop``.

Reviewed entries are keyed by **file**, because migrations are append-only in
practice: a new decision arrives as a new file, which is exactly what this test
should stop in its tracks. Editing an old migration to add a noop op would slip
past, and that is an acceptable gap for a file nobody edits.
"""

from __future__ import annotations

import ast
from pathlib import Path

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import migrations as migrations_package

MIGRATIONS_DIR = Path(migrations_package.__file__).resolve().parent

#: Migrations whose ``noop`` reverses were read and judged correct, with why.
#: Audit chunks 459-460 covered 0026-0044; chunk 544 covered 0001-0020, which
#: that pass never reached.
REVIEWED: dict[str, str] = {
    "0001_initial.py": "backfill_pin_point / backfill_primary_email_normalized - fill new columns the schema reverse drops anyway.",
    "0003_v0_4_0_data.py": (
        "Eleven backfills and structural conversions (pins to child wikis, campus to boundaries, markup snapshots). "
        "Lossy on reverse - the converted-from rows are gone - but everything left is valid in the pre-migration schema, "
        "so the old code reads it fine."
    ),
    "0005_v0_4_0_pin_location_dedupe.py": "dedupe_pin_locations merges duplicates so the next migration can add a constraint. Un-merging is impossible and unnecessary: merged rows stay valid.",
    "0007_pinshare_bundled_with_markup_map_removed_flags.py": (
        "backfill_trip_slugs fills a new column; disable_auto_tagging resets three booleans to the new opt-in default. "
        "The reset loses prior preferences and cannot restore them, but leaves valid booleans. "
        "This file's *third* RunPython, the credential-token encryption, is exactly the case that must NOT be noop and carries a real decrypting reverse."
    ),
    "0008_add_image_media_labels.py": (
        "Slug backfills, share-location backfills, three Wikipedia cache invalidations, and a cap of "
        "max_upload_file_size_mb at 900. The cap is lossy and still an ordinary integer afterwards."
    ),
    "0010_v0_6_0.py": "Backfills (intro_seen, notification uuids, unchanged defaults) plus create_first_party_client, which seeds a row that is harmless to leave behind.",
    "0046_trip_calendar_link_event_unique.py": "drop_duplicate_event_links merges duplicates so a unique constraint can be added. Un-merging is impossible and unnecessary - what remains is valid in the old schema.",
    "0047_link_url_unique.py": "drop_duplicate_links, same shape as 0046: duplicates removed ahead of a constraint, and the survivors read fine either way.",
    "0020_seed_vip_subscription_role.py": "Seeds a subscription role. Leaving it on reverse is harmless; deleting it could orphan subscriptions referencing it.",
    "0033_quota_exemptions.py": "mark_existing_external_media_exempt sets a boolean on existing rows. Lossy, valid either way.",
    "0042_label_merge_duplicates.py": "merge_duplicate_labels clears the way for 0043's unique constraint. Same shape as 0005 - un-merging is impossible and merged labels stay valid.",
}


def _noop_reverse_files() -> dict[str, list[str]]:
    """Migration files containing a ``RunPython``/``RunSQL`` that reverses to noop.

    Returns:
        ``{filename: [forward callable names]}``.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"RunPython", "RunSQL"}:
                continue
            reverse = ""
            for keyword in node.keywords:
                if keyword.arg in {"reverse_code", "reverse_sql"}:
                    reverse = ast.unparse(keyword.value)
            if not reverse and len(node.args) >= 2:
                reverse = ast.unparse(node.args[1])
            if "noop" in reverse:
                forward = ast.unparse(node.args[0]) if node.args else "?"
                found.setdefault(path.name, []).append(forward)
    return found


class MigrationNoopReverseGuardTests(SimpleTestCase):
    def test_every_noop_reverse_is_reviewed(self) -> None:
        unreviewed = {name: ops for name, ops in _noop_reverse_files().items() if name not in REVIEWED}

        self.assertEqual(
            unreviewed,
            {},
            "a migration reverses to RunPython.noop without being reviewed - confirm the pre-migration code can still "
            "read the data after a reverse (a format change cannot), then add it to REVIEWED with the reason",
        )

    def test_no_reviewed_entry_is_stale(self) -> None:
        """A file that no longer has a noop reverse should leave the list."""
        actual = set(_noop_reverse_files())
        self.assertEqual(set(REVIEWED) - actual, set(), "REVIEWED names a migration that no longer reverses to noop")

    def test_every_run_python_declares_some_reverse(self) -> None:
        """Omitting a reverse entirely makes `migrate` refuse - which is a different, louder failure."""
        missing = []
        for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in {"RunPython", "RunSQL"}:
                    continue
                declared = len(node.args) >= 2 or any(kw.arg in {"reverse_code", "reverse_sql"} for kw in node.keywords)
                if not declared:
                    missing.append(f"{path.name}: {ast.unparse(node)[:60]}")
        self.assertEqual(missing, [], "these operations declare no reverse at all")

    # -- guard the guard ----------------------------------------------------

    def test_the_scan_still_finds_migrations(self) -> None:
        self.assertGreaterEqual(len(list(MIGRATIONS_DIR.glob("[0-9]*.py"))), 40, "the migration scan found almost nothing - the path resolution broke")

    def test_the_scan_still_finds_noop_reverses(self) -> None:
        """Without this, an AST change that matched nothing would pass silently."""
        found = _noop_reverse_files()
        self.assertGreaterEqual(len(found), 8, f"only {len(found)} files with noop reverses found - the matcher stopped working")
