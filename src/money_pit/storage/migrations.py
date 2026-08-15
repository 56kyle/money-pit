"""Module containing release schema baselines, migration, and identity checks."""

# pyright: reportAny=false, reportUnusedFunction=false

import hashlib
import importlib.resources
import json
import sqlite3
from datetime import datetime
from datetime import timezone
from typing import Final
from typing import cast

from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.pipeline.identity import interpretation_bundle_id
from money_pit.pipeline.identity import ordered_work_fingerprint
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import parse_artifact_record_binding
from money_pit.schemas.theses import CandidateThesis
from money_pit.semantic_identity import HYPOTHESIS_REVIEW_POLICY_VERSION
from money_pit.semantic_identity import CandidateSemanticVariant
from money_pit.semantic_identity import candidate_review_dimensions
from money_pit.semantic_identity import candidate_semantic_variant
from money_pit.semantic_identity import canonical_hypothesis_group_id
from money_pit.semantic_identity import research_case_id
from money_pit.semantic_identity import research_premise_semantics_from_mappings
from money_pit.semantic_identity import research_scope_fingerprint
from money_pit.semantic_identity import research_task_semantics_from_mapping
from money_pit.storage.errors import BaselineApplyError
from money_pit.storage.errors import BaselineDiscoveryError
from money_pit.storage.errors import UnknownDatabaseSchemaError
from money_pit.synthesis_material import merge_research_contexts
from money_pit.synthesis_material import project_synthesis_material


_SCHEMA_PACKAGE: Final[str] = "money_pit.storage.sql"
_CURRENT_SCHEMA_RELEASE: Final[str] = "0.0.6"
_PREDECESSOR_RELEASE: Final[str] = "0.0.5"
_PREDECESSOR_BASELINE_FILENAME: Final[str] = "schema_0_0_3.sql"
_SCHEMA_0_0_4_DELTA_FILENAME: Final[str] = "schema_0_0_4_delta.sql"
_PREDECESSOR_DELTA_FILENAME: Final[str] = "schema_0_0_5_delta.sql"
_CURRENT_DELTA_FILENAME: Final[str] = "schema_0_0_6_delta.sql"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_LIST_ADAPTER = TypeAdapter(list[JsonValue])


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
    """Return the exact trusted 0.0.5 predecessor baseline."""
    baseline = _read_schema_resource(_PREDECESSOR_BASELINE_FILENAME, release="0.0.3")
    delta_0_0_4 = _read_schema_resource(_SCHEMA_0_0_4_DELTA_FILENAME, release="0.0.4")
    delta_0_0_5 = _read_schema_resource(_PREDECESSOR_DELTA_FILENAME, release=_PREDECESSOR_RELEASE)
    return f"{baseline.rstrip()}\n\n{delta_0_0_4.lstrip()}\n\n{delta_0_0_5.lstrip()}"


def load_baseline_sql() -> str:
    """Return the complete 0.0.6 baseline."""
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
    """Return the catalog digest of the packaged 0.0.6 baseline."""
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


def _backfill_canonical_hypotheses(connection: sqlite3.Connection) -> dict[str, str]:
    memberships: dict[str, str] = {}
    created: list[tuple[CandidateThesis, CandidateSemanticVariant]] = []
    for row in connection.execute(
        "SELECT candidate_thesis_id, created_at, candidate_json FROM candidate_theses ORDER BY candidate_thesis_id"
    ).fetchall():
        candidate = CandidateThesis.model_validate_json(str(row[2]))
        if candidate.candidate_thesis_id != str(row[0]):
            raise ValueError("Candidate row identity disagrees with its immutable payload.")
        semantic = candidate_semantic_variant(candidate)
        variant_id = semantic.variant_id
        group_id = canonical_hypothesis_group_id((variant_id,))
        _ = connection.execute(
            """INSERT OR IGNORE INTO hypothesis_variants
            (variant_id, semantic_fingerprint, capital_kind, capital_reference, availability,
             direction, horizon_class, theme_key, causal_mechanisms_json,
             regime_assumptions_json, created_at, variant_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                variant_id,
                semantic.fingerprint,
                semantic.capital_kind.value,
                semantic.capital_reference,
                "available" if semantic.capital_available else "unavailable",
                semantic.direction.value,
                semantic.horizon_class.value,
                semantic.theme,
                _canonical_json(semantic.causal_mechanisms),
                _canonical_json(semantic.regime_assumptions),
                str(row[1]),
                semantic.model_dump_json(),
            ),
        )
        _ = connection.execute(
            """INSERT OR IGNORE INTO canonical_hypothesis_groups (group_id, status, created_at)
            VALUES (?, 'current', ?)""",
            (group_id, str(row[1])),
        )
        _ = connection.execute(
            """INSERT OR IGNORE INTO canonical_hypothesis_group_variants (group_id, variant_id)
            VALUES (?, ?)""",
            (group_id, variant_id),
        )
        _ = connection.execute(
            """INSERT INTO candidate_hypothesis_memberships
            (candidate_thesis_id, variant_id, membership_kind, recorded_at)
            VALUES (?, ?, 'exact', ?)""",
            (candidate.candidate_thesis_id, variant_id, str(row[1])),
        )
        memberships[candidate.candidate_thesis_id] = group_id
        created.append((candidate, semantic))
    for index, (first_candidate, first_semantic) in enumerate(created):
        for second_candidate, second_semantic in created[index + 1 :]:
            differences = candidate_review_dimensions(first_semantic, second_semantic)
            if not differences:
                continue
            subject, comparison = sorted((first_candidate.candidate_thesis_id, second_candidate.candidate_thesis_id))
            subject_variant = (
                first_semantic.variant_id
                if subject == first_candidate.candidate_thesis_id
                else second_semantic.variant_id
            )
            comparison_variant = (
                second_semantic.variant_id
                if subject == first_candidate.candidate_thesis_id
                else first_semantic.variant_id
            )
            coarse = hashlib.sha256(
                _canonical_json(
                    {
                        "capital_kind": first_semantic.capital_kind.value,
                        "capital_reference": first_semantic.capital_reference,
                        "direction": first_semantic.direction.value,
                    }
                ).encode()
            ).hexdigest()
            review_id = (
                "hypothesis-review:"
                + hashlib.sha256(
                    _canonical_json((subject, comparison, coarse, HYPOTHESIS_REVIEW_POLICY_VERSION)).encode()
                ).hexdigest()
            )
            created_at = str(
                connection.execute(
                    "SELECT max(created_at) FROM candidate_theses WHERE candidate_thesis_id IN (?, ?)",
                    (subject, comparison),
                ).fetchone()[0]
            )
            _ = connection.execute(
                """INSERT INTO hypothesis_reviews
                (review_id, subject_candidate_id, comparison_candidate_id, subject_variant_id,
                 comparison_variant_id, coarse_fingerprint, policy_version, created_at, review_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    subject,
                    comparison,
                    subject_variant,
                    comparison_variant,
                    coarse,
                    HYPOTHESIS_REVIEW_POLICY_VERSION,
                    created_at,
                    _canonical_json({"differing_dimensions": differences}),
                ),
            )
    return memberships


def _job_task_ids(payload: object, task_by_json: dict[str, str]) -> tuple[str, ...]:
    payload = _JSON_OBJECT_ADAPTER.validate_python(payload)
    premises = payload.get("premises")
    tasks_value = premises.get("tasks") if isinstance(premises, dict) else None
    tasks: list[JsonValue] = [] if tasks_value is None else _JSON_LIST_ADAPTER.validate_python(tasks_value)
    identifiers: list[str] = []
    for task in tasks:
        identifier = task_by_json.get(_canonical_json(task))
        if identifier is None:
            raise ValueError("Research job premise refers to an unknown planned task.")
        identifiers.append(identifier)
    return tuple(sorted(set(identifiers)))


def _planner_task_ids(
    connection: sqlite3.Connection,
    job_id: str,
    task_by_json: dict[str, str],
) -> tuple[str, ...]:
    identifiers: set[str] = set()
    for row in connection.execute(
        "SELECT result_json FROM research_planner_results WHERE job_id = ? ORDER BY wave_number", (job_id,)
    ).fetchall():
        result = _JSON_OBJECT_ADAPTER.validate_json(str(row[0]))
        tasks = result.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("Research planner result tasks are not a list.")
        for task in tasks:
            identifier = task_by_json.get(_canonical_json(task))
            if identifier is None:
                raise ValueError("Research planner result refers to an unknown planned task.")
            identifiers.add(identifier)
    return tuple(sorted(identifiers))


def _semantic_premise_fingerprint(
    hypothesis_id: str,
    material_claims: object,
    task_ids: tuple[str, ...],
    task_state: dict[str, sqlite3.Row],
) -> str:
    material_claims = _JSON_LIST_ADAPTER.validate_python(material_claims)
    claims: list[dict[str, object]] = []
    for claim in material_claims:
        if isinstance(claim, dict):
            claims.append(dict(claim))
        elif isinstance(claim, str):
            claims.append({"canonical_claim_key": claim, "legacy_reconstruction": "unresolved"})
        else:
            raise ValueError("Research material claim is neither an identity nor an object.")
    tasks: list[dict[str, object]] = []
    for task_id in task_ids:
        payload = _JSON_OBJECT_ADAPTER.validate_json(str(task_state[task_id][6]))
        tasks.append(research_task_semantics_from_mapping(payload).model_dump(mode="json"))
    return research_premise_semantics_from_mappings(
        hypothesis_id=hypothesis_id,
        material_claims=tuple(claims),
        initial_tasks=tuple(tasks),
    ).fingerprint


def _backfill_research_semantics(  # noqa: C901
    connection: sqlite3.Connection,
    memberships: dict[str, str],
) -> None:
    planned_rows = connection.execute(
        """SELECT task_id, candidate_thesis_id, status, completed_at,
                  materialized_session_id, materialized_task_id, task_json
        FROM planned_research_tasks ORDER BY created_at, task_id"""
    ).fetchall()
    tasks_by_candidate: dict[str, dict[str, str]] = {}
    task_state: dict[str, sqlite3.Row] = {}
    for row in planned_rows:
        _ = tasks_by_candidate.setdefault(str(row[1]), {}).setdefault(
            _canonical_json(_JSON_OBJECT_ADAPTER.validate_json(str(row[6]))), str(row[0])
        )
        task_state[str(row[0])] = row
    jobs: dict[str, dict[str, object]] = {}
    for row in connection.execute(
        "SELECT job_id, candidate_thesis_id, premise_fingerprint, status, created_at, job_json FROM research_jobs ORDER BY created_at, job_id"
    ).fetchall():
        job_id = str(row[0])
        candidate_id = str(row[1])
        origins = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT unit_id FROM research_job_discovery_origins WHERE job_id = ? ORDER BY unit_id", (job_id,)
            ).fetchall()
        )
        source_rows = connection.execute(
            """SELECT DISTINCT unit.source_id FROM research_job_discovery_origins origin
            JOIN discovery_units unit ON unit.unit_id = origin.unit_id
            WHERE origin.job_id = ? ORDER BY unit.source_id""",
            (job_id,),
        ).fetchall()
        source_ids = tuple(str(item[0]) for item in source_rows if item[0] is not None)
        contains_global = any(item[0] is None for item in source_rows)
        mixed_scope = len(source_ids) > 1 or (contains_global and bool(source_ids))
        scope_source = source_ids[0] if len(source_ids) == 1 and not contains_global else None
        scope = (
            hashlib.sha256(
                _canonical_json(
                    {
                        "policy": "legacy-mixed-research-scope@1",
                        "source_ids": source_ids,
                        "contains_global": contains_global,
                    }
                ).encode()
            ).hexdigest()
            if mixed_scope
            else research_scope_fingerprint(scope_source)
        )
        hypothesis_id = memberships[candidate_id]
        case_id = research_case_id(hypothesis_id, scope)
        job_payload = _JSON_OBJECT_ADAPTER.validate_json(str(row[5]))
        initial_task_ids = _job_task_ids(job_payload, tasks_by_candidate.get(candidate_id, {}))
        planner_task_ids = _planner_task_ids(connection, job_id, tasks_by_candidate.get(candidate_id, {}))
        task_ids = tuple(sorted({*initial_task_ids, *planner_task_ids}))
        premises_value = job_payload.get("premises", {})
        premises = _JSON_OBJECT_ADAPTER.validate_python(premises_value)
        material_claims = premises.get("material_claims", [])
        jobs[job_id] = {
            "candidate_id": candidate_id,
            "hypothesis_id": hypothesis_id,
            "case_id": case_id,
            "scope": scope,
            "premise": _semantic_premise_fingerprint(hypothesis_id, material_claims, initial_task_ids, task_state),
            "status": str(row[3]),
            "created_at": str(row[4]),
            "task_ids": task_ids,
            "initial_task_ids": initial_task_ids,
            "origins": origins,
            "scope_available": not mixed_scope,
        }
    by_case: dict[str, list[str]] = {}
    for job_id, job in jobs.items():
        by_case.setdefault(cast("str", job["case_id"]), []).append(job_id)
    for case_id, job_ids in sorted(by_case.items()):
        first = jobs[job_ids[0]]
        _ = connection.execute(
            """INSERT INTO research_cases
            (case_id, hypothesis_id, scope_fingerprint, head_job_id, created_at)
            VALUES (?, ?, ?, NULL, ?)""",
            (
                case_id,
                first["hypothesis_id"],
                first["scope"],
                min(cast("str", jobs[item]["created_at"]) for item in job_ids),
            ),
        )
        maximal = [
            item
            for item in job_ids
            if not any(
                set(cast("tuple[str, ...]", jobs[item]["task_ids"]))
                < set(cast("tuple[str, ...]", jobs[other]["task_ids"]))
                for other in job_ids
            )
        ]
        unique_sets = {cast("tuple[str, ...]", jobs[item]["task_ids"]) for item in maximal}
        if not bool(first["scope_available"]):
            head = None
        elif len(unique_sets) == 1:
            head = sorted(maximal, key=lambda item: (jobs[item]["created_at"], item))[-1]
        else:
            union_task_ids = tuple(
                sorted({task for item in maximal for task in cast("tuple[str, ...]", jobs[item]["task_ids"])})
            )
            candidate_id = min(cast("str", jobs[item]["candidate_id"]) for item in maximal)
            created_at = max(cast("str", jobs[item]["created_at"]) for item in maximal)
            hypothesis_id = cast("str", first["hypothesis_id"])
            premise = _semantic_premise_fingerprint(hypothesis_id, [], union_task_ids, task_state)
            head = (
                "research-job:"
                + hashlib.sha256(_canonical_json((case_id, premise, union_task_ids)).encode()).hexdigest()
            )
            task_payloads = [
                _JSON_OBJECT_ADAPTER.validate_json(str(task_state[task_id][6])) for task_id in union_task_ids
            ]
            _ = connection.execute(
                """INSERT INTO research_jobs
                (job_id, parent_job_id, candidate_thesis_id, premise_fingerprint, cycle_number,
                 review_trigger_at, source_discovery_unit_id, status, created_at, claimed_run_id,
                 claimed_at, completed_at, stop_reason, wave_count, search_count,
                 accepted_fetch_count, next_review_at, job_json)
                VALUES (?, NULL, ?, ?, 0, NULL, NULL, 'pending', ?, NULL, NULL, NULL, NULL, 0, 0, 0, NULL, ?)""",
                (
                    head,
                    candidate_id,
                    premise,
                    created_at,
                    _canonical_json(
                        {
                            "candidate_id": candidate_id,
                            "semantic_union_predecessor_ids": sorted(maximal),
                            "premises": {"tasks": task_payloads, "material_claims": []},
                        }
                    ),
                ),
            )
            union_origins = tuple(
                sorted({origin for item in maximal for origin in cast("tuple[str, ...]", jobs[item]["origins"])})
            )
            for origin in union_origins:
                _ = connection.execute(
                    "INSERT INTO research_job_discovery_origins (job_id, unit_id) VALUES (?, ?)", (head, origin)
                )
            jobs[head] = {
                "candidate_id": candidate_id,
                "hypothesis_id": hypothesis_id,
                "case_id": case_id,
                "scope": first["scope"],
                "premise": premise,
                "status": "pending",
                "created_at": created_at,
                "task_ids": union_task_ids,
                "initial_task_ids": union_task_ids,
                "origins": union_origins,
                "scope_available": True,
            }
            job_ids.append(head)
        for job_id in job_ids:
            disposition = "current" if job_id == head else "superseded" if head is not None else "unavailable"
            successor = head if disposition == "superseded" else None
            _ = connection.execute(
                """INSERT INTO research_job_semantics
                (job_id, case_id, semantic_premise_fingerprint,
                 successor_job_id, disposition, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, case_id, jobs[job_id]["premise"], successor, disposition, jobs[job_id]["created_at"]),
            )
            if successor is not None:
                _ = connection.execute(
                    "INSERT INTO research_job_predecessors (successor_job_id, predecessor_job_id) VALUES (?, ?)",
                    (successor, job_id),
                )
        if head is not None:
            _ = connection.execute("UPDATE research_cases SET head_job_id = ? WHERE case_id = ?", (head, case_id))
        session_owner = {
            str(item[0]): str(item[1])
            for item in connection.execute(
                "SELECT session_id, job_id FROM research_wave_results WHERE job_id IN (SELECT value FROM json_each(?))",
                (_canonical_json(job_ids),),
            ).fetchall()
        }
        for session_id, owner_job_id in sorted(session_owner.items()):
            _ = connection.execute(
                """INSERT INTO research_job_sessions (job_id, session_id, bound_at)
                SELECT ?, session_id, started_at FROM research_sessions WHERE session_id = ?""",
                (owner_job_id, session_id),
            )
        for job_id in job_ids:
            for task_id in cast("tuple[str, ...]", jobs[job_id]["task_ids"]):
                role = (
                    "initial"
                    if task_id in cast("tuple[str, ...]", jobs[job_id]["initial_task_ids"])
                    else "planner_followup"
                )
                state = task_state[task_id]
                status = "pending"
                completed_at = None
                session_id = None if state[4] is None else str(state[4])
                materialized_task_id = None if state[5] is None else str(state[5])
                if session_id is not None and session_owner.get(session_id) == job_id:
                    status = str(state[2])
                    completed_at = None if state[3] is None else str(state[3])
                _ = connection.execute(
                    """INSERT INTO research_job_tasks
                    (job_id, task_id, role, execution_status, materialized_session_id,
                     materialized_task_id, completed_at, reused_from_job_id, reused_from_task_id, binding_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '{}')""",
                    (
                        job_id,
                        task_id,
                        role,
                        status,
                        session_id if status != "pending" else None,
                        materialized_task_id if status != "pending" else None,
                        completed_at,
                    ),
                )
                for origin in cast("tuple[str, ...]", jobs[job_id]["origins"]):
                    _ = connection.execute(
                        "INSERT INTO research_job_task_origins (job_id, task_id, unit_id) VALUES (?, ?, ?)",
                        (job_id, task_id, origin),
                    )
        if head is not None:
            existing_head_tasks = {
                str(item[0])
                for item in connection.execute(
                    "SELECT task_id FROM research_job_tasks WHERE job_id = ?", (head,)
                ).fetchall()
            }
            for predecessor in job_ids:
                if predecessor == head:
                    continue
                for task in connection.execute(
                    """SELECT task_id, role, completed_at FROM research_job_tasks
                    WHERE job_id = ? AND execution_status IN ('completed', 'reused')""",
                    (predecessor,),
                ).fetchall():
                    task_id = str(task[0])
                    if task_id in existing_head_tasks:
                        _ = connection.execute(
                            """UPDATE research_job_tasks SET execution_status = 'reused', completed_at = ?,
                            materialized_session_id = NULL, materialized_task_id = NULL,
                            reused_from_job_id = ?, reused_from_task_id = ?
                            WHERE job_id = ? AND task_id = ? AND execution_status = 'pending'""",
                            (str(task[2]), predecessor, task_id, head, task_id),
                        )


def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
    values = _JSON_LIST_ADAPTER.validate_python(value)
    if not all(isinstance(item, str) for item in values):
        raise ValueError(f"Legacy synthesis {field_name} must contain only strings.")
    return tuple(sorted(item for item in values if isinstance(item, str)))


def _legacy_synthesis_projection(
    connection: sqlite3.Connection,
    *,
    payload: object,
    hypothesis_id: str,
    prior_revision_id: str | None,
    as_of: str,
) -> tuple[dict[str, object], bool, bool, bool]:
    """Recover decision material only from the final persisted synthesis context."""
    payload = _JSON_OBJECT_ADAPTER.validate_python(payload)
    context_container = payload.get("context")
    contexts = context_container.get("contexts") if isinstance(context_container, dict) else None
    grounded_value = payload.get("origin_observation_ids", [])
    grounded_observation_ids = _string_sequence(grounded_value, "origin observations")
    if not isinstance(contexts, list) or not contexts:
        legacy_final = payload.get("final_context")
        legacy_observations = _string_sequence(payload.get("observation_ids", []), "observations")
        if isinstance(legacy_final, dict):
            legacy_evidence = _string_sequence(legacy_final.get("accepted_evidence_ids", []), "accepted evidence ids")
            evidence_passed = legacy_final.get("evidence_standard_satisfied") is True
            contradiction = legacy_final.get("decisive_contradiction") is True
            return (
                {
                    "hypothesis_id": hypothesis_id,
                    "grounded_observation_ids": legacy_observations,
                    "legacy_accepted_evidence_ids": legacy_evidence,
                    "material_claim_states": (),
                    "accepted_evidence": (),
                    "verification_states": (),
                    "supported_claim_keys": (),
                    "provisionally_covered_claim_keys": (),
                    "contradicted_claim_keys": (),
                    "unresolved_claim_keys": (),
                    "evidence_standard_satisfied": evidence_passed,
                    "decisive_contradiction": contradiction,
                    "prior_revision_id": prior_revision_id,
                    "policy_version": "synthesis-material@1",
                    "legacy_reconstruction": "incomplete",
                },
                evidence_passed or contradiction,
                False,
                True,
            )
        return (
            {
                "hypothesis_id": hypothesis_id,
                "grounded_observation_ids": grounded_observation_ids,
                "material_claim_states": (),
                "accepted_evidence": (),
                "verification_states": (),
                "supported_claim_keys": (),
                "provisionally_covered_claim_keys": (),
                "contradicted_claim_keys": (),
                "unresolved_claim_keys": (),
                "evidence_standard_satisfied": False,
                "decisive_contradiction": False,
                "prior_revision_id": prior_revision_id,
                "policy_version": "synthesis-material@1",
                "legacy_reconstruction": "incomplete",
            },
            False,
            False,
            False,
        )
    try:
        typed_contexts = tuple(ResearchCumulativeContext.model_validate(context) for context in contexts)
    except (TypeError, ValueError):
        return (
            {"hypothesis_id": hypothesis_id, "legacy_reconstruction": "malformed_final_context"},
            False,
            False,
            False,
        )
    merged_context = merge_research_contexts(typed_contexts)
    claim_keys = tuple(sorted(set(merged_context.material_anchor_assessment.material_claim_keys)))
    claims: list[CanonicalClaim] = []
    for claim_key in claim_keys:
        row = connection.execute(
            """SELECT projection_json FROM canonical_claims
            WHERE canonical_claim_key = ? AND projected_as_of <= ?
            ORDER BY projected_as_of DESC LIMIT 1""",
            (claim_key, as_of),
        ).fetchone()
        if row is None:
            continue
        claims.append(CanonicalClaim.model_validate_json(str(row[0])))
    assessment = merged_context.material_anchor_assessment
    assessment_passed = assessment.evidence_standard_satisfied or assessment.decisive_contradiction
    complete = bool(
        grounded_observation_ids
        and claim_keys
        and len(claims) == len(claim_keys)
        and (not assessment_passed or merged_context.alias_bindings)
    )
    if not complete:
        return (
            {
                "hypothesis_id": hypothesis_id,
                "grounded_observation_ids": grounded_observation_ids,
                "material_claim_keys": claim_keys,
                "evidence_standard_satisfied": assessment.evidence_standard_satisfied,
                "decisive_contradiction": assessment.decisive_contradiction,
                "prior_revision_id": prior_revision_id,
                "policy_version": "synthesis-material@1",
                "legacy_reconstruction": "incomplete",
            },
            assessment_passed,
            False,
            True,
        )
    projection = project_synthesis_material(
        hypothesis_id=hypothesis_id,
        grounded_observation_ids=grounded_observation_ids,
        material_claims=tuple(claims),
        research_context=merged_context,
        prior_revision_id=prior_revision_id,
    )
    return projection.model_dump(mode="json"), assessment_passed, True, True


def _synthesis_material_tokens(value: object) -> set[str]:
    """Return comparable decision identities without operational history."""
    value = _JSON_OBJECT_ADAPTER.validate_python(value)
    tokens: set[str] = set()
    for field_name in (
        "grounded_observation_ids",
        "supported_claim_keys",
        "provisionally_covered_claim_keys",
        "contradicted_claim_keys",
        "unresolved_claim_keys",
    ):
        items = value.get(field_name, [])
        if isinstance(items, list):
            tokens.update(f"{field_name}:{item}" for item in items if isinstance(item, str))
    for field_name in ("material_claim_states", "accepted_evidence", "verification_states"):
        items = value.get(field_name, [])
        if isinstance(items, list):
            tokens.update(f"{field_name}:{_canonical_json(item)}" for item in items if isinstance(item, dict))
    return tokens


def _reconcile_migrated_synthesis_units(
    connection: sqlite3.Connection,
    grouped: dict[tuple[str, str], list[tuple[str, str, str, str]]],
) -> None:
    priority = {"completed": 4, "checkpointed": 3, "active": 2, "pending": 1}
    for units in grouped.values():
        completed = [unit for unit in units if unit[1] == "completed"]
        if len(completed) > 1:
            raise ValueError("Equivalent synthesis material has conflicting completed outputs.")
        winner = sorted(units, key=lambda unit: (priority[unit[1]], unit[0]))[-1]
        eligibility = str(
            connection.execute(
                "SELECT eligibility FROM synthesis_material_states WHERE material_state_id = ?", (winner[2],)
            ).fetchone()[0]
        )
        pending_review = connection.execute(
            """SELECT 1 FROM synthesis_material_states material
            JOIN canonical_hypothesis_group_variants group_variant
              ON group_variant.group_id = material.hypothesis_id
            JOIN hypothesis_reviews review
              ON group_variant.variant_id IN (review.subject_variant_id, review.comparison_variant_id)
            LEFT JOIN hypothesis_review_resolutions resolution USING (review_id)
            WHERE material.material_state_id = ? AND resolution.review_id IS NULL LIMIT 1""",
            (winner[2],),
        ).fetchone()
        authoritative = eligibility == "eligible" and pending_review is None and winner[3] == "1"
        for unit_id, _status, state_id, _complete in units:
            disposition = (
                "current"
                if authoritative and unit_id == winner[0]
                else "superseded"
                if unit_id != winner[0]
                else "unavailable"
            )
            successor = winner[0] if disposition == "superseded" else None
            _ = connection.execute(
                """INSERT INTO synthesis_unit_semantics
                (unit_id, material_state_id, disposition, successor_unit_id, recorded_at)
                SELECT ?, ?, ?, ?, created_at FROM synthesis_units WHERE unit_id = ?""",
                (unit_id, state_id, disposition, successor, unit_id),
            )


def _supersede_subset_synthesis_material(connection: sqlite3.Connection) -> None:
    state_rows = connection.execute(
        "SELECT material_state_id, hypothesis_id, prior_revision_id, assessment_json FROM synthesis_material_states"
    ).fetchall()
    for state in state_rows:
        assessment = _JSON_OBJECT_ADAPTER.validate_json(str(state[3]))
        state_values = _synthesis_material_tokens(assessment)
        supersets: list[tuple[int, str]] = []
        for comparison in state_rows:
            if state[0] == comparison[0] or state[1] != comparison[1] or state[2] != comparison[2]:
                continue
            comparison_assessment = _JSON_OBJECT_ADAPTER.validate_json(str(comparison[3]))
            comparison_values = _synthesis_material_tokens(comparison_assessment)
            if state_values < comparison_values:
                supersets.append((len(comparison_values), str(comparison[0])))
        if not supersets:
            continue
        target_state = sorted(supersets, key=lambda item: (-item[0], item[1]))[0][1]
        target = connection.execute(
            """SELECT unit_id FROM synthesis_unit_semantics
            WHERE material_state_id = ? ORDER BY CASE disposition WHEN 'current' THEN 0 ELSE 1 END, unit_id LIMIT 1""",
            (target_state,),
        ).fetchone()
        if target is not None:
            _ = connection.execute(
                """UPDATE synthesis_unit_semantics SET disposition = 'superseded', successor_unit_id = ?
                WHERE material_state_id = ? AND unit_id != ?""",
                (str(target[0]), str(state[0]), str(target[0])),
            )


def _supersede_synthesis_from_research_succession(connection: sqlite3.Connection) -> None:
    predecessor_units = connection.execute(
        """SELECT unit.unit_id, research_semantic.successor_job_id
        FROM synthesis_units unit
        JOIN research_job_semantics research_semantic
          ON research_semantic.job_id = unit.research_job_id
        WHERE research_semantic.disposition = 'superseded'
          AND research_semantic.successor_job_id IS NOT NULL
        ORDER BY unit.unit_id"""
    ).fetchall()
    for predecessor_unit in predecessor_units:
        successor_unit = connection.execute(
            """SELECT unit_id FROM synthesis_units WHERE research_job_id = ?
            ORDER BY CASE status WHEN 'completed' THEN 0 WHEN 'checkpointed' THEN 1
                         WHEN 'active' THEN 2 ELSE 3 END,
                     created_at DESC, unit_id DESC LIMIT 1""",
            (str(predecessor_unit[1]),),
        ).fetchone()
        if successor_unit is None:
            _ = connection.execute(
                """UPDATE synthesis_unit_semantics SET disposition = 'superseded',
                successor_unit_id = NULL, successor_research_job_id = ? WHERE unit_id = ?""",
                (str(predecessor_unit[1]), str(predecessor_unit[0])),
            )
        else:
            _ = connection.execute(
                """UPDATE synthesis_unit_semantics SET disposition = 'superseded',
                successor_unit_id = ?, successor_research_job_id = NULL WHERE unit_id = ?""",
                (str(successor_unit[0]), str(predecessor_unit[0])),
            )


def _backfill_synthesis_semantics(connection: sqlite3.Connection) -> None:
    grouped: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
    for row in connection.execute(
        """SELECT unit.unit_id, unit.research_job_id, unit.status, unit.created_at, unit.unit_json,
                  research_case.hypothesis_id
        FROM synthesis_units unit
        JOIN research_job_semantics semantic ON semantic.job_id = unit.research_job_id
        JOIN research_cases research_case ON research_case.case_id = semantic.case_id
        ORDER BY unit.created_at, unit.unit_id"""
    ).fetchall():
        payload = _JSON_OBJECT_ADAPTER.validate_json(str(row[4]))
        prior_row = connection.execute(
            """SELECT revision.revision_id FROM thesis_revisions revision
            JOIN theses thesis ON thesis.thesis_id = revision.thesis_id
            JOIN research_jobs job ON job.candidate_thesis_id = thesis.candidate_thesis_id
            WHERE job.job_id = ? AND revision.known_at <= ?
            ORDER BY revision.known_at DESC, revision.revision_number DESC LIMIT 1""",
            (str(row[1]), str(row[3])),
        ).fetchone()
        prior_revision_id = None if prior_row is None else str(prior_row[0])
        projection, assessment_passed, reconstruction_complete, final_assessment_present = _legacy_synthesis_projection(
            connection,
            payload=payload,
            hypothesis_id=str(row[5]),
            prior_revision_id=prior_revision_id,
            as_of=str(row[3]),
        )
        fingerprint = hashlib.sha256(_canonical_json(projection).encode()).hexdigest()
        state_id = f"synthesis-material:{fingerprint}"
        eligibility = (
            "unavailable"
            if not final_assessment_present or (not reconstruction_complete and assessment_passed)
            else "eligible"
            if assessment_passed
            else "insufficient_evidence"
        )
        _ = connection.execute(
            """INSERT OR IGNORE INTO synthesis_material_states
            (material_state_id, hypothesis_id, material_fingerprint, eligibility,
             prior_revision_id, created_at, assessment_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                state_id,
                str(row[5]),
                fingerprint,
                eligibility,
                prior_revision_id,
                str(row[3]),
                _canonical_json(projection),
            ),
        )
        _ = connection.execute(
            "INSERT OR IGNORE INTO synthesis_material_origins (material_state_id, research_job_id) VALUES (?, ?)",
            (state_id, str(row[1])),
        )
        grouped.setdefault((str(row[5]), fingerprint), []).append(
            (str(row[0]), str(row[2]), state_id, "1" if reconstruction_complete else "0")
        )
    _reconcile_migrated_synthesis_units(connection, grouped)
    _supersede_synthesis_from_research_succession(connection)
    _supersede_subset_synthesis_material(connection)


def _backfill_semantic_intelligence(connection: sqlite3.Connection) -> None:
    _ = connection.execute(
        """UPDATE research_wave_results
        SET execution_completed_run_id = run_id,
            execution_completed_at = COALESCE(execution_completed_at, recorded_at)
        WHERE phase = 'execution_completed' AND execution_completed_run_id IS NULL"""
    )
    _ = connection.execute(
        """UPDATE research_wave_results SET checkpointed_run_id = run_id
        WHERE checkpointed_at IS NOT NULL AND checkpointed_run_id IS NULL"""
    )
    memberships = _backfill_canonical_hypotheses(connection)
    _backfill_research_semantics(connection, memberships)
    _backfill_synthesis_semantics(connection)


def _migrate_verified_predecessor(connection: sqlite3.Connection, *, target_fingerprint: str) -> None:
    try:
        delta = _read_schema_resource(_CURRENT_DELTA_FILENAME, release=_CURRENT_SCHEMA_RELEASE)
        for statement in _sql_statements(delta):
            _ = connection.execute(statement)
        _backfill_semantic_intelligence(connection)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BaselineApplyError("Could not migrate the verified 0.0.5 schema to 0.0.6.") from error
    if catalog_fingerprint(connection) != target_fingerprint:
        raise BaselineApplyError("The migrated 0.0.6 schema does not match the packaged baseline.")
    cursor = connection.execute(
        "UPDATE schema_metadata SET release = ?, schema_fingerprint = ? WHERE singleton = 1",
        (_CURRENT_SCHEMA_RELEASE, target_fingerprint),
    )
    if cursor.rowcount != 1:
        raise BaselineApplyError("Could not record the verified 0.0.6 schema identity.")


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
        raise BaselineApplyError("The migrated database did not retain the verified 0.0.6 identity.")
    raise UnknownDatabaseSchemaError(
        "The database schema does not exactly match money-pit 0.0.6 or its trusted 0.0.5 predecessor."
    )
