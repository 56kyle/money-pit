import sqlite3

import pytest

from money_pit.storage.errors import MigrationApplyError
from money_pit.storage.errors import MigrationChecksumError
from money_pit.storage.errors import MigrationDiscoveryError
from money_pit.storage.errors import MigrationHistoryError
from money_pit.storage.migrations import Migration
from money_pit.storage.migrations import _sql_statements
from money_pit.storage.migrations import apply_pending_migrations
from money_pit.storage.migrations import discover_migrations


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def _migration(version=1, name="first", sql="CREATE TABLE example (id INTEGER);"):
    return Migration(version=version, name=name, sql=sql, checksum=f"checksum-{version}")


def test__sql_statements_preserves_complete_multiline_statements():
    script = "CREATE TABLE one (\n id INTEGER\n);\nINSERT INTO one VALUES (1);\n"

    assert _sql_statements(script) == (
        "CREATE TABLE one (\n id INTEGER\n);\n",
        "INSERT INTO one VALUES (1);\n",
    )


def test__sql_statements_rejects_incomplete_tail():
    with pytest.raises(MigrationDiscoveryError):
        _sql_statements("CREATE TABLE incomplete (id INTEGER)")


def test_apply_pending_migrations_rejects_checksum_change():
    connection = _connection()
    migration = _migration()
    apply_pending_migrations(connection, (migration,))
    changed = Migration(version=1, name="first", sql=migration.sql, checksum="changed")

    with pytest.raises(MigrationChecksumError):
        apply_pending_migrations(connection, (changed,))


def test_apply_pending_migrations_rejects_history_not_in_build():
    connection = _connection()
    apply_pending_migrations(connection, (_migration(),))

    with pytest.raises(MigrationHistoryError):
        apply_pending_migrations(connection, ())


def test_apply_pending_migrations_rejects_changed_name():
    connection = _connection()
    apply_pending_migrations(connection, (_migration(),))

    with pytest.raises(MigrationHistoryError):
        apply_pending_migrations(connection, (_migration(name="renamed"),))


def test_apply_pending_migrations_wraps_invalid_sql():
    connection = _connection()

    with pytest.raises(MigrationApplyError):
        apply_pending_migrations(connection, (_migration(sql="CREATE TABLE broken ();"),))


def test_claim_recorded_at_migration_deterministically_backfills_existing_rows():
    connection = _connection()
    connection.execute("PRAGMA foreign_keys = ON")
    migrations = discover_migrations()
    apply_pending_migrations(connection, migrations[:7])
    connection.execute(
        """
        INSERT INTO claim_observations (
            observation_id, canonical_claim_key, claim_text, claim_kind,
            source_item_id, asserted_at, evidence_fragment_ids_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "observation",
            "claim",
            "Revenue grew.",
            "factual",
            "source:item",
            "2000-01-01T00:00:00+00:00",
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO verification_results (
            verification_id, observation_id, status, checked_at, verifier_version
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "verification",
            "observation",
            "supported",
            "2001-01-01T00:00:00+00:00",
            "v1",
        ),
    )

    apply_pending_migrations(connection, migrations)

    observation = connection.execute("SELECT asserted_at, recorded_at FROM claim_observations").fetchone()
    verification = connection.execute("SELECT checked_at, recorded_at FROM verification_results").fetchone()
    foreign_key = connection.execute("PRAGMA foreign_key_list(verification_results)").fetchone()
    assert observation is not None
    assert verification is not None
    assert foreign_key is not None
    assert observation["recorded_at"] == verification["recorded_at"]
    assert observation["recorded_at"] > observation["asserted_at"]
    assert verification["recorded_at"] > verification["checked_at"]
    assert foreign_key["table"] == "claim_observations"


def test_source_asset_provenance_migration_backfills_legacy_rows():
    connection = _connection()
    migrations = discover_migrations()
    apply_pending_migrations(connection, migrations[:8])
    digest = "a" * 64
    connection.execute(
        """
        INSERT INTO source_cursors (source_id, cursor_json, updated_at)
        VALUES ('source', '{"value":"next"}', '2026-07-29T00:00:00+00:00')
        """,
    )
    connection.execute(
        """
        INSERT INTO evidence_assets (
            asset_id, content_hash, media_type, source_item_id,
            local_path, retrieved_at, metadata_json
        )
        VALUES (?, ?, 'text/plain', 'source:item', ?,
                '2026-07-29T00:00:00+00:00', '{}')
        """,
        (digest, digest, f"aa/{digest}"),
    )

    apply_pending_migrations(connection, migrations)

    cursor = connection.execute(
        "SELECT cursor_purpose, cursor_json FROM source_cursors",
    ).fetchone()
    asset = connection.execute(
        "SELECT asset_id, content_hash, local_path FROM evidence_assets",
    ).fetchone()
    acquisition = connection.execute(
        """
        SELECT asset_id, source_item_id, content_version, legacy_source_item_id, media_type
        FROM evidence_asset_acquisitions
        """,
    ).fetchone()
    assert tuple(cursor) == ("sync", '{"value":"next"}')
    assert tuple(asset) == (digest, digest, f"aa/{digest}")
    assert tuple(acquisition) == (digest, None, None, "source:item", "text/plain")


def test_exact_acquisition_migration_marks_legacy_provenance_unresolved():
    connection = _connection()
    connection.execute("PRAGMA foreign_keys = ON")
    migrations = discover_migrations()
    apply_pending_migrations(connection, migrations[:9])
    digest = "b" * 64
    connection.execute(
        """
        INSERT INTO evidence_assets (
            asset_id, content_hash, local_path, metadata_json
        ) VALUES (?, ?, ?, '{}')
        """,
        (digest, digest, f"bb/{digest}"),
    )
    connection.execute(
        """
        INSERT INTO evidence_asset_acquisitions (
            asset_id, source_item_id, retrieved_at, media_type
        ) VALUES (?, 'legacy:item', '2026-07-29T00:00:00+00:00', 'text/plain')
        """,
        (digest,),
    )

    apply_pending_migrations(connection, migrations)

    acquisition = connection.execute(
        """
        SELECT source_item_id, content_version, legacy_source_item_id, provenance_status
        FROM evidence_asset_acquisitions
        """,
    ).fetchone()
    assert tuple(acquisition) == (None, None, "legacy:item", "legacy_unresolved")


def test_exact_acquisition_composite_foreign_key_rejects_wrong_content_version():
    connection = _connection()
    connection.execute("PRAGMA foreign_keys = ON")
    apply_pending_migrations(connection, discover_migrations())
    digest = "c" * 64
    connection.execute(
        """
        INSERT INTO source_definitions (
            source_id, adapter_name, enabled, locator, cadence, tags_json,
            trust_profile, allowed_uses_json, adapter_config_json, registry_version,
            registered_at, definition_hash
        ) VALUES (
            'source', 'test', 1, 'https://example.com', NULL, '[]',
            NULL, '[]', '{}', 1, '2026-07-29T00:00:00+00:00', 'hash'
        )
        """,
    )
    connection.execute(
        """
        INSERT INTO source_items (
            source_item_id, source_id, canonical_uri, discovered_at, content_version
        ) VALUES (
            'source:item', 'source', 'https://example.com/item',
            '2026-07-29T00:00:00+00:00', 'version-1'
        )
        """,
    )
    connection.execute(
        """
        INSERT INTO evidence_assets (asset_id, content_hash, local_path, metadata_json)
        VALUES (?, ?, ?, '{}')
        """,
        (digest, digest, f"cc/{digest}"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO evidence_asset_acquisitions (
                asset_id, source_item_id, content_version, retrieved_at,
                media_type, provenance_status
            ) VALUES (
                ?, 'source:item', 'version-2', '2026-07-29T00:00:00+00:00',
                'text/plain', 'resolved'
            )
            """,
            (digest,),
        )
