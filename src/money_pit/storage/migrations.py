"""Module containing release schema baselines, migration, and identity checks."""

# pyright: reportAny=false

import hashlib
import importlib.resources
import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import Final

from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.pipeline.identity import interpretation_bundle_id
from money_pit.pipeline.identity import ordered_work_fingerprint
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import parse_artifact_record_binding
from money_pit.storage.errors import BaselineApplyError
from money_pit.storage.errors import BaselineDiscoveryError
from money_pit.storage.errors import UnknownDatabaseSchemaError


_SCHEMA_PACKAGE: Final[str] = "money_pit.storage.sql"
_CURRENT_SCHEMA_RELEASE: Final[str] = "0.0.4"
_PREDECESSOR_RELEASE: Final[str] = "0.0.3"
_PREDECESSOR_BASELINE_FILENAME: Final[str] = "schema_0_0_3.sql"
_CURRENT_DELTA_FILENAME: Final[str] = "schema_0_0_4_delta.sql"


def _require_current_schema_release() -> None:
    """Fail closed when application and schema releases are not intentionally aligned."""
    if APP_VERSION != _CURRENT_SCHEMA_RELEASE:
        raise BaselineDiscoveryError(
            f"Application release {APP_VERSION} is not bound to schema release {_CURRENT_SCHEMA_RELEASE}."
        )


def _read_schema_resource(filename: str, *, release: str) -> str:
    try:
        return importlib.resources.files(_SCHEMA_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as error:
        raise BaselineDiscoveryError(f"The packaged {release} schema resource is unavailable.") from error


def _load_predecessor_baseline_sql() -> str:
    """Return the exact trusted 0.0.3 predecessor baseline."""
    return _read_schema_resource(_PREDECESSOR_BASELINE_FILENAME, release=_PREDECESSOR_RELEASE)


def load_baseline_sql() -> str:
    """Return the complete 0.0.4 baseline."""
    _require_current_schema_release()
    delta = _read_schema_resource(_CURRENT_DELTA_FILENAME, release=_CURRENT_SCHEMA_RELEASE)
    return f"{_load_predecessor_baseline_sql().rstrip()}\n\n{delta.lstrip()}"


def _sql_statements(script: str) -> tuple[str, ...]:
    """Split a baseline using SQLite's statement completeness parser."""
    statements: list[str] = []
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        candidate = "".join(pending)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():
                statements.append(candidate)
            pending.clear()
    if "".join(pending).strip():
        raise BaselineDiscoveryError("The packaged baseline ends with an incomplete SQL statement.")
    return tuple(statements)


def _catalog_rows(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    rows: list[sqlite3.Row] = connection.execute(
        """
        SELECT type, name, tbl_name, sql FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        ORDER BY type, name, tbl_name, sql
        """
    ).fetchall()
    return tuple((str(row["type"]), str(row["name"]), str(row["tbl_name"]), str(row["sql"])) for row in rows)


def catalog_fingerprint(connection: sqlite3.Connection) -> str:
    """Return a deterministic digest of the complete application schema catalog."""
    encoded = json.dumps(_catalog_rows(connection), separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _baseline_fingerprint(script: str, *, release: str) -> str:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for statement in _sql_statements(script):
            _ = connection.execute(statement)
        return catalog_fingerprint(connection)
    except sqlite3.Error as error:
        raise BaselineDiscoveryError(f"The packaged {release} baseline is not valid SQLite.") from error
    finally:
        connection.close()


def expected_schema_fingerprint() -> str:
    """Return the catalog digest of the packaged 0.0.4 baseline."""
    _require_current_schema_release()
    return _baseline_fingerprint(load_baseline_sql(), release=_CURRENT_SCHEMA_RELEASE)


def _expected_predecessor_schema_fingerprint() -> str:
    return _baseline_fingerprint(_load_predecessor_baseline_sql(), release=_PREDECESSOR_RELEASE)


def expected_predecessor_schema_fingerprint() -> str:
    """Return the catalog digest of the exact migratable predecessor."""
    return _expected_predecessor_schema_fingerprint()


def is_logically_empty(connection: sqlite3.Connection) -> bool:
    """Return whether the database has no application schema objects."""
    return not _catalog_rows(connection)


def apply_baseline(connection: sqlite3.Connection) -> None:
    """Apply the baseline only to a logically empty database."""
    if not is_logically_empty(connection):
        raise UnknownDatabaseSchemaError("A schema baseline can be applied only to an empty database.")
    expected_fingerprint = expected_schema_fingerprint()
    try:
        for statement in _sql_statements(load_baseline_sql()):
            _ = connection.execute(statement)
        initialized_at = datetime.now(tz=timezone.utc).isoformat()
        _ = connection.execute(
            """INSERT INTO schema_metadata
            (singleton, application_id, release, schema_fingerprint, initialized_at)
            VALUES (1, ?, ?, ?, ?)""",
            (APP_NAME, _CURRENT_SCHEMA_RELEASE, expected_fingerprint, initialized_at),
        )
        _ = connection.execute(
            """INSERT INTO execution_control
            (control_id, execution_disabled, changed_at, changed_by, reason, policy_version)
            VALUES (1, 1, ?, 'system', 'Execution is disabled until explicitly enabled.', 'unconfigured')""",
            (initialized_at,),
        )
    except sqlite3.Error as error:
        raise BaselineApplyError(f"Could not apply the {_CURRENT_SCHEMA_RELEASE} schema baseline.") from error


def _schema_identity(connection: sqlite3.Connection) -> tuple[str, str, str]:
    try:
        row = connection.execute(
            """SELECT application_id, release, schema_fingerprint FROM schema_metadata
            WHERE singleton = 1"""
        ).fetchone()
    except sqlite3.Error as error:
        raise UnknownDatabaseSchemaError("The nonempty database has no recognized schema metadata.") from error
    if row is None:
        raise UnknownDatabaseSchemaError("The nonempty database has no recognized schema metadata.")
    return str(row["application_id"]), str(row["release"]), str(row["schema_fingerprint"])


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _backfill_interpretation_work(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """SELECT attempt_id, source_item_id, content_version, asset_id, run_id,
                  interpreter_version, started_at, completed_at
           FROM claim_interpretation_attempts WHERE outcome = 'succeeded'
           ORDER BY source_item_id, content_version, interpreter_version, asset_id, attempt_id"""
    ).fetchall()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((str(row[1]), str(row[2]), str(row[5])), []).append(row)
    for (source_item_id, content_version, interpreter_version), attempts in groups.items():
        eligible_rows = connection.execute(
            """SELECT DISTINCT acquisition.asset_id
            FROM evidence_asset_acquisitions AS acquisition
            JOIN source_items AS item
              ON item.source_item_id = acquisition.source_item_id
             AND item.content_version = acquisition.content_version
            JOIN source_definition_revisions AS revision
              ON revision.definition_hash = acquisition.source_definition_hash
            WHERE acquisition.source_item_id = ? AND acquisition.content_version = ?
              AND EXISTS (
                SELECT 1 FROM json_each(revision.definition_json, '$.allowed_uses') AS allowed_use
                WHERE allowed_use.value = 'interpretation'
              )
              AND EXISTS (
                SELECT 1 FROM evidence_processing_attempts AS processing
                WHERE processing.source_item_id = acquisition.source_item_id
                  AND processing.content_version = acquisition.content_version
                  AND processing.asset_id = acquisition.asset_id
                  AND processing.status = 'succeeded'
              )
            ORDER BY acquisition.asset_id""",
            (source_item_id, content_version),
        ).fetchall()
        eligible_asset_ids = tuple(str(row[0]) for row in eligible_rows)
        if not eligible_asset_ids:
            continue
        attempts_by_asset = {str(row[3]): row for row in attempts}
        successful_asset_ids = tuple(sorted(attempts_by_asset))
        attempt_ids = tuple(str(row[0]) for row in attempts)
        fragment_rows = connection.execute(
            """SELECT fragment_id FROM evidence_fragments
            WHERE asset_id IN (SELECT value FROM json_each(?)) ORDER BY fragment_id""",
            (_canonical_json(eligible_asset_ids),),
        ).fetchall()
        fragment_ids = tuple(str(row[0]) for row in fragment_rows)
        fingerprint = ordered_work_fingerprint(fragment_ids or eligible_asset_ids)
        bundle_id = interpretation_bundle_id(
            source_item_id=source_item_id,
            content_version=content_version,
            interpreter_version=interpreter_version,
            input_fingerprint=fingerprint,
        )
        is_completed = successful_asset_ids == eligible_asset_ids
        completed_at = max(str(row[7]) for row in attempts) if is_completed else None
        bundle_json = _canonical_json(
            {
                "backfilled_attempt_ids": attempt_ids,
                "eligible_asset_ids": eligible_asset_ids,
                "fragment_ids": fragment_ids,
            }
        )
        _ = connection.execute(
            """INSERT INTO interpretation_bundles
            (bundle_id, source_item_id, content_version, interpreter_version, input_fingerprint,
             status, created_at, completed_at, bundle_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bundle_id,
                source_item_id,
                content_version,
                interpreter_version,
                fingerprint,
                "completed" if is_completed else "pending",
                min(str(row[6]) for row in attempts),
                completed_at,
                bundle_json,
            ),
        )
        for number, asset_id in enumerate(eligible_asset_ids):
            row = attempts_by_asset.get(asset_id)
            chunk_fingerprint = _digest("interpretation-chunk", bundle_id, asset_id)
            if row is None:
                _ = connection.execute(
                    """INSERT INTO interpretation_bundle_chunks
                    (chunk_id, bundle_id, chunk_number, input_fingerprint, status,
                     output_attempt_ids_json, output_json, chunk_json)
                    VALUES (?, ?, ?, ?, 'pending', '[]', 'null', ?)""",
                    (
                        _digest("interpretation-chunk-id", bundle_id, str(number)),
                        bundle_id,
                        number,
                        chunk_fingerprint,
                        _canonical_json(
                            {
                                "source_item_id": source_item_id,
                                "content_version": content_version,
                                "asset_ids": [asset_id],
                                "legacy_missing_interpretation": True,
                            }
                        ),
                    ),
                )
                continue
            attempt_id = str(row[0])
            _ = connection.execute(
                """INSERT INTO legacy_interpretation_reuse
                (source_item_id, content_version, asset_id, attempt_id, admitted_at)
                VALUES (?, ?, ?, ?, ?)""",
                (source_item_id, content_version, asset_id, attempt_id, str(row[7])),
            )
            _ = connection.execute(
                """INSERT INTO interpretation_bundle_chunks
                (chunk_id, bundle_id, chunk_number, input_fingerprint, status, claimed_run_id,
                 claimed_at, completed_at, output_attempt_ids_json, output_json, chunk_json)
                VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, 'null', ?)""",
                (
                    _digest("interpretation-chunk-id", bundle_id, str(number)),
                    bundle_id,
                    number,
                    chunk_fingerprint,
                    str(row[4]),
                    str(row[6]),
                    str(row[7]),
                    _canonical_json([attempt_id]),
                    _canonical_json({"backfilled_attempt_id": attempt_id}),
                ),
            )


def _backfill_discovery_work(connection: sqlite3.Connection) -> None:
    artifacts = connection.execute(
        """SELECT artifact_id, run_id, started_at, known_at, input_ids_json, output_ids_json,
                  payload_hash FROM stage_artifacts WHERE stage = 'A2' ORDER BY known_at, artifact_id"""
    ).fetchall()
    for artifact in artifacts:
        artifact_id = str(artifact[0])
        batch_id = _digest("discovery-batch", artifact_id)
        output_bindings = tuple(parse_artifact_record_binding(str(value)) for value in json.loads(str(artifact[5])))
        requested_output_ids = tuple(
            binding.record_id for binding in output_bindings if binding.kind is ArtifactRecordKind.CANDIDATE_THESIS
        )
        output_ids = tuple(
            candidate_id
            for candidate_id in requested_output_ids
            if connection.execute(
                "SELECT 1 FROM candidate_theses WHERE candidate_thesis_id = ?", (candidate_id,)
            ).fetchone()
            is not None
        )
        result_fingerprint = str(artifact[6])
        batch_json = _canonical_json({"backfilled_artifact_id": artifact_id})
        _ = connection.execute(
            """INSERT INTO discovery_batches
            (batch_id, run_id, status, created_at, completed_at, result_fingerprint,
             output_candidate_ids_json, batch_json)
            VALUES (?, ?, 'completed', ?, ?, ?, ?, ?)""",
            (
                batch_id,
                str(artifact[1]),
                str(artifact[2]),
                str(artifact[3]),
                result_fingerprint,
                _canonical_json(output_ids),
                batch_json,
            ),
        )
        for binding in json.loads(str(artifact[4])):
            binding_text = str(binding)
            parsed_binding = parse_artifact_record_binding(binding_text)
            subject_id = parsed_binding.record_id
            unit_id = _digest("discovery-unit", artifact_id, binding_text)
            fingerprint = _digest("discovery-input", artifact_id, binding_text)
            if parsed_binding.kind is ArtifactRecordKind.CANONICAL_CLAIM:
                source_row = connection.execute(
                    """SELECT item.source_id FROM claim_resolution_decisions AS resolution
                    JOIN claim_observations AS observation
                      ON observation.observation_id = resolution.subject_observation_id
                    JOIN source_items AS item ON item.source_item_id = observation.source_item_id
                    WHERE resolution.resolved_canonical_claim_key = ?
                    ORDER BY item.discovered_at DESC LIMIT 1""",
                    (subject_id,),
                ).fetchone()
            elif parsed_binding.kind is ArtifactRecordKind.OBSERVATION:
                source_row = connection.execute(
                    """SELECT item.source_id FROM claim_observations AS observation
                    JOIN source_items AS item ON item.source_item_id = observation.source_item_id
                    WHERE observation.observation_id = ? ORDER BY item.discovered_at DESC LIMIT 1""",
                    (subject_id,),
                ).fetchone()
            else:
                source_row = None
            source_id = None if source_row is None else str(source_row[0])
            _ = connection.execute(
                """INSERT INTO discovery_units
                (unit_id, unit_kind, subject_id, input_fingerprint, source_id, status,
                 created_at, completed_at, unit_json)
                VALUES (?, 'canonical_claim', ?, ?, ?, 'completed', ?, ?, ?)""",
                (
                    unit_id,
                    subject_id,
                    fingerprint,
                    source_id,
                    str(artifact[2]),
                    str(artifact[3]),
                    _canonical_json({"backfilled_artifact_id": artifact_id, "input_binding": binding_text}),
                ),
            )
            _ = connection.execute(
                "INSERT INTO discovery_batch_units (batch_id, unit_id) VALUES (?, ?)",
                (batch_id, unit_id),
            )
            _ = connection.execute(
                """INSERT INTO discovery_unit_origins (unit_id, origin_kind, origin_id)
                VALUES (?, ?, ?)""",
                (unit_id, parsed_binding.kind.value, subject_id),
            )
            for candidate_id in output_ids:
                _ = connection.execute(
                    """INSERT INTO candidate_discovery_origins
                    (candidate_thesis_id, unit_id, batch_id) VALUES (?, ?, ?)""",
                    (candidate_id, unit_id, batch_id),
                )
                _ = connection.execute(
                    """INSERT OR IGNORE INTO planned_research_task_origins (task_id, unit_id)
                    SELECT task_id, ? FROM planned_research_tasks
                    WHERE candidate_thesis_id = ?""",
                    (unit_id, candidate_id),
                )


def _backfill_research_jobs(connection: sqlite3.Connection) -> None:
    sessions = connection.execute(
        """SELECT session_id, run_id, scope_subject_id, started_at, status, stop_reason,
                  query_count, fetch_count, session_json
           FROM research_sessions WHERE scope_kind = 'candidate_thesis'
           ORDER BY started_at, session_id"""
    ).fetchall()
    for row in sessions:
        candidate_id = str(row[2])
        session_id = str(row[0])
        premise_fingerprint = _digest("legacy-research-session", candidate_id, session_id)
        job_id = _digest("research-job", candidate_id, premise_fingerprint)
        is_terminal = str(row[4]) != "active"
        status = "terminal" if is_terminal else "active"
        completed_row = connection.execute(
            "SELECT known_at FROM research_candidate_summaries WHERE session_id = ?", (session_id,)
        ).fetchone()
        completed_at = str(completed_row[0]) if completed_row is not None else None
        stop_reason = str(row[5]) if row[5] is not None else ("legacy_completed" if is_terminal else None)
        query_count = min(int(row[6]), 6)
        fetch_count = min(int(row[7]), 12)
        wave_count = _legacy_completed_wave_count(connection, session_id=session_id)
        checkpoint_digest = _legacy_research_checkpoint_digest(
            connection,
            candidate_id=candidate_id,
            session_id=session_id,
        )
        _ = connection.execute(
            """INSERT INTO research_jobs
            (job_id, candidate_thesis_id, premise_fingerprint, status, created_at,
             claimed_run_id, claimed_at, completed_at, stop_reason, wave_count,
             search_count, accepted_fetch_count, job_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                candidate_id,
                premise_fingerprint,
                status,
                str(row[3]),
                str(row[1]),
                str(row[3]),
                completed_at,
                stop_reason,
                wave_count,
                query_count,
                fetch_count,
                _canonical_json({"backfilled_session_id": session_id, "legacy_session": json.loads(str(row[8]))}),
            ),
        )
        _ = connection.execute(
            """INSERT INTO research_job_checkpoints
            (checkpoint_id, job_id, run_id, wave_number, search_count,
             accepted_fetch_count, recorded_at, digest_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _digest("research-checkpoint", job_id, "0"),
                job_id,
                str(row[1]),
                wave_count,
                query_count,
                fetch_count,
                completed_at or str(row[3]),
                _canonical_json(checkpoint_digest),
            ),
        )
        has_legacy_synthesis = connection.execute(
            "SELECT 1 FROM stage_artifacts WHERE run_id = ? AND stage = 'A4' LIMIT 1",
            (str(row[1]),),
        ).fetchone()
        if is_terminal and completed_row is not None and has_legacy_synthesis is None:
            input_fingerprint = _digest(_canonical_json(checkpoint_digest))
            _ = connection.execute(
                """INSERT INTO synthesis_units
                (unit_id, research_job_id, input_fingerprint, status, created_at,
                 unit_payload_hash, unit_json)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    _digest("synthesis-unit", job_id, input_fingerprint),
                    job_id,
                    input_fingerprint,
                    completed_at or str(row[3]),
                    hashlib.sha256(
                        _canonical_json(
                            {
                                "candidate_id": candidate_id,
                                "context": checkpoint_digest,
                                "origin_observation_ids": [],
                                "backfilled_session_id": session_id,
                            }
                        ).encode()
                    ).hexdigest(),
                    _canonical_json(
                        {
                            "candidate_id": candidate_id,
                            "context": checkpoint_digest,
                            "origin_observation_ids": [],
                            "backfilled_session_id": session_id,
                        }
                    ),
                ),
            )


def _legacy_completed_wave_count(connection: sqlite3.Connection, *, session_id: str) -> int:
    """Infer only waves that precede unfinished durable legacy tasks."""
    pending_row = connection.execute(
        "SELECT MIN(round_number) FROM research_tasks WHERE session_id = ? AND status = 'pending'",
        (session_id,),
    ).fetchone()
    earliest_pending = None if pending_row is None or pending_row[0] is None else int(pending_row[0])
    if earliest_pending is not None:
        return min(max(earliest_pending - 1, 0), 3)
    latest_row = connection.execute(
        "SELECT MAX(round_number) FROM research_tasks WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    latest_round = 0 if latest_row is None or latest_row[0] is None else int(latest_row[0])
    return min(max(latest_round, 0), 3)


def _legacy_research_checkpoint_digest(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    session_id: str,
) -> dict[str, object]:
    """Project an interrupted legacy session into the current resumable checkpoint contract."""
    query_rows = connection.execute(
        """SELECT provider, query_text FROM research_tasks
        WHERE session_id = ? AND status != 'pending'
        ORDER BY provider, query_text, task_id""",
        (session_id,),
    ).fetchall()
    normalized_queries = sorted(
        {f"{str(query_row[0]).casefold()}:{' '.join(str(query_row[1]).split()).casefold()}" for query_row in query_rows}
    )
    return {
        "candidate": {
            "candidate_thesis_id": candidate_id,
            "session_id": session_id,
            "rounds": [],
            "stop_reason": "unresolved",
            "planner_requests": [],
            "planner_responses": [],
            "normalized_queries": normalized_queries,
        },
        "contexts": [],
        "legacy_session_id": session_id,
    }


def _migrate_verified_predecessor(connection: sqlite3.Connection, *, target_fingerprint: str) -> None:
    try:
        delta = _read_schema_resource(_CURRENT_DELTA_FILENAME, release=_CURRENT_SCHEMA_RELEASE)
        for statement in _sql_statements(delta):
            _ = connection.execute(statement)
        _backfill_interpretation_work(connection)
        _backfill_discovery_work(connection)
        _backfill_research_jobs(connection)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BaselineApplyError("Could not migrate and backfill the verified 0.0.3 schema to 0.0.4.") from error
    if catalog_fingerprint(connection) != target_fingerprint:
        raise BaselineApplyError("The migrated 0.0.4 schema does not match the packaged baseline.")
    cursor = connection.execute(
        "UPDATE schema_metadata SET release = ?, schema_fingerprint = ? WHERE singleton = 1",
        (_CURRENT_SCHEMA_RELEASE, target_fingerprint),
    )
    if cursor.rowcount != 1:
        raise BaselineApplyError("Could not record the verified 0.0.4 schema identity.")


def verify_schema_identity(connection: sqlite3.Connection) -> None:
    """Verify this build or atomically migrate its exact trusted predecessor."""
    expected_fingerprint = expected_schema_fingerprint()
    stored_identity = _schema_identity(connection)
    expected_identity = APP_NAME, _CURRENT_SCHEMA_RELEASE, expected_fingerprint
    live_fingerprint = catalog_fingerprint(connection)
    if stored_identity == expected_identity and live_fingerprint == expected_fingerprint:
        return
    predecessor_fingerprint = _expected_predecessor_schema_fingerprint()
    predecessor_identity = APP_NAME, _PREDECESSOR_RELEASE, predecessor_fingerprint
    if stored_identity == predecessor_identity and live_fingerprint == predecessor_fingerprint:
        _migrate_verified_predecessor(connection, target_fingerprint=expected_fingerprint)
        if _schema_identity(connection) == expected_identity:
            return
        raise BaselineApplyError("The migrated database did not retain the verified 0.0.4 identity.")
    raise UnknownDatabaseSchemaError(
        "The database schema does not exactly match money-pit 0.0.4 or its trusted 0.0.3 predecessor."
    )
