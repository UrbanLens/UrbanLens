"""Fail the build when a new data migration reverses to ``noop`` without a reason."""

from __future__ import annotations

import ast
from pathlib import Path

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard import migrations as migrations_package

MIGRATIONS_DIR = Path(migrations_package.__file__).resolve().parent

#: Migrations whose ``noop`` reverses were read and judged correct, with why.
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
    "0020_seed_vip_subscription_role.py": "Seeds a subscription role. Leaving it on reverse is harmless; deleting it could orphan subscriptions referencing it.",
    # NOTE: the migration carries two noop reverses this entry does NOT cover - _0062_clear_generated_names and
    # _0062_renumber_levels, from the pre-squash 0062_floorplan_floor_designation.py.
    "0030_v0_7_0.py": (
        "mark_existing_external_media_exempt (was 0033) sets a boolean on existing rows - lossy, valid either way. "
        "merge_duplicate_labels (was 0042) clears the way for 0043's unique constraint, same shape as 0005 - "
        "un-merging is impossible and merged labels stay valid. drop_duplicate_event_links (was 0046) and "
        "drop_duplicate_links (was 0047) both merge/remove duplicates ahead of a unique constraint - un-merging is "
        "impossible and unnecessary, what remains is valid in the old schema."
    ),
    "0032_v0_8_0.py": (
        "The v0.8.0 squash. Backfills of columns this file adds and a reverse drops, so there is nothing to restore: "
        "_0049_backfill_friendinvitation_email_normalized, _0052__backfill, _0058_trust_verified_signups, "
        "_0062_backfill_username_keys, _0065_backfill, _0117_date_existing_blocks. "
        "Merges and dedupes ahead of a unique constraint, lossy but leaving ordinary rows the old code reads: "
        "_0054_merge_reciprocal_rows (keeps the lowest pk, as FriendshipQuerySet.between does), _0073_dedupe, and "
        "_0067_release_stale_and_duplicate_proofs (a cleared proof reads as an unproved address). "
        "_0091__move_to_day_rows moves the weather cache into rows; after a reverse the old code refetches it. "
        "_0092__clear_out_of_span nulls activity times outside 1900-2199, an unscheduled activity to the old code. "
        "_0098_repair_links prefixes https:// on scheme-less links, deletes links it can't read as http(s) with a "
        "top-level domain, and truncates over-long custom-field text. _0116_mark_linked relabels downloaded-URL "
        "images from upload to linked_url, a plain string the old code stores and shows. The two CREATE EXTENSION "
        "statements leave an extension nothing else depends on."
    ),
    "0033_v0_8_0_indexes.py": (
        "_0096_refuse_to_drop_url_only_overlays deletes nothing; it only clears a tile template an overlay with an "
        "image never drew. The rows left are valid overlays for the old code."
    ),
    "0034_reresolve_fiat_building_places.py": (
        "_reresolve_fiat_building_places moves Locations off outline-less buildings onto the place containing them, "
        "or onto none, and drops their parcel-buildings cache. A reverse leaves ordinary place links and unstamped "
        "Locations, which the old code reads and re-resolves; the cache is refetched."
    ),
    "0038_location_cache_drop_name_built_searches.py": (
        "drop_name_built_searches deletes cached name-built search rows, which may hold results found by someone's "
        "own names. They are a cache; the old code refetches them."
    ),
    "0041_location_slug_remint.py": (
        "remint_location_slugs replaces slugs drawn from user text with provider names or the uuid, and records each "
        "readable slug given up in LocationSlugHistory (0040). A reverse to 0040 keeps both; every slug is still valid, "
        "and old links resolve through the history."
    ),
    "0042_location_cache_drop_nearby_reference_documents.py": (
        "drop_nearby_reference_documents deletes the removed panel's cached rows. They are a cache; the old code refetches them."
    ),
    "0043_media_keys_without_tracking_params.py": (
        "rekey_tracked_media re-keys marks and copies to the hash of their URL without utm_ parameters. Which ones carried "
        "them is not recoverable, but the rows stay valid; after a reverse the old code matches them only when the "
        "upstream URL comes untracked, which is the behaviour it had before."
    ),
    "0044_session_participant_departure.py": (
        "_backfill_departures fills the new departure column, which the schema reverse drops."
    ),
    "0046_location_cache_drop_historic_registers.py": (
        "drop_historic_registers deletes cached Historic Registers rows so they refetch with each row's reference "
        "number and position. They are a cache; the old code refetches them."
    ),
    "0047_cris_site_rows_refetch.py": (
        "drop_stale_cris_rows deletes CRIS cache rows chosen by the old site rule. They are a cache; the old code "
        "refetches them."
    ),
    "0049_building_location_names.py": (
        "fix_building_locations clears names a building's location may not carry, names unnamed ones from their own "
        "CRIS record, re-mints their slugs (recording each readable one given up in LocationSlugHistory), drops "
        "misplaced CRIS cards and nests root wikis under their parcel's. A reverse keeps every value, each one the old "
        "code reads; the dropped rows are a cache it refetches, and old links resolve through the history."
    ),
    "0054_location_slug_follows_name.py": (
        "remint_mismatched_slugs moves Location and Wiki slugs onto their current provider name, or the uuid, recording "
        "each readable Location slug given up in LocationSlugHistory. A reverse keeps the new slugs, each one valid to "
        "the old code, and old links resolve through the history."
    ),
}


def _noop_reverse_files() -> dict[str, list[str]]:
    """Migration files containing a ``RunPython``/``RunSQL`` that reverses to noop.

    Returns:
        ``{filename: [forward callable names]}``.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
            tree = ast.parse(path.read_text(encoding="utf-8"))
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
        self.assertGreaterEqual(
            len(list(MIGRATIONS_DIR.glob("[0-9]*.py"))),
            20,
            "the migration scan found almost nothing - the path resolution broke",
        )

    def test_the_scan_still_finds_noop_reverses(self) -> None:
        """Without this, an AST change that matched nothing would pass silently."""
        found = _noop_reverse_files()
        self.assertGreaterEqual(
            len(found), 8, f"only {len(found)} files with noop reverses found - the matcher stopped working"
        )
