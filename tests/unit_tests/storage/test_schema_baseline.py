import hashlib
import importlib.resources
import json
import sqlite3
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from money_pit.agents.inference import InferenceInvocationContext
from money_pit.agents.inference import InferenceResult
from money_pit.agents.inference import InferenceUsage
from money_pit.claims.repository import ClaimRepository
from money_pit.config import ConfigurationScope
from money_pit.config import claim_refresh_policy
from money_pit.config import load_application_config
from money_pit.constants import APP_NAME
from money_pit.constants import APP_VERSION
from money_pit.contracts import InterpretationDraft
from money_pit.contracts import InterpretationRequest
from money_pit.evidence.work import EvidenceWorkStore
from money_pit.pipeline import research as research_module
from money_pit.pipeline.identity import interpretation_policy_version
from money_pit.pipeline.interpretation import make_interpretation_node
from money_pit.pipeline.research import _ensure_semantic_research_heads  # pyright: ignore[reportPrivateUsage]
from money_pit.portfolio.theses import ThesisRepository
from money_pit.research.memory import PlannedResearchTaskStore
from money_pit.schemas.runs import RunRecord
from money_pit.storage import migrations as migrations_module
from money_pit.storage.admission import IntelligenceAdmissionRepository
from money_pit.storage.database import Database
from money_pit.storage.errors import BaselineApplyError
from money_pit.storage.errors import BaselineDiscoveryError
from money_pit.storage.errors import UnknownDatabaseSchemaError
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.migrations import catalog_fingerprint
from money_pit.storage.migrations import expected_schema_fingerprint
from money_pit.storage.runs import RunRepository
from money_pit.storage.semantic_intelligence import SemanticIntelligenceRepository


_PREDECESSOR_RELEASE = "0.0.5"
_INITIALIZED_AT = "2026-08-01T12:00:00+00:00"
_INCREMENTAL_RUN_ID = "12d1838e-caae-4f45-9c06-91ef7f2b9135"


def _create_exact_predecessor(path: Path) -> str:
    baseline = (
        importlib.resources.files("money_pit.storage.sql").joinpath("schema_0_0_3.sql").read_text(encoding="utf-8")
    )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        _ = connection.executescript(baseline)
        fingerprint = catalog_fingerprint(connection)
        _ = connection.execute(
            "INSERT INTO schema_metadata VALUES (1, ?, ?, ?, ?)",
            (APP_NAME, _PREDECESSOR_RELEASE, fingerprint, _INITIALIZED_AT),
        )
        _ = connection.execute(
            "INSERT INTO source_cursors VALUES ('source', 'sync', '{\"page\":2}', ?)",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            "INSERT INTO execution_control VALUES (1, 1, ?, 'tester', 'preserve', 'policy-1')",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            """INSERT INTO runs (
                run_id, requested_as_of, started_at, known_at, through_stage,
                source_config_hash, intelligence_config_hash, manifest_json
            ) VALUES ('legacy-run', ?, ?, ?, 'A4', 'sources', 'intelligence', '{}')""",
            (_INITIALIZED_AT, _INITIALIZED_AT, _INITIALIZED_AT),
        )
        _ = connection.execute(
            """INSERT INTO stage_artifacts (
                artifact_id, run_id, stage, requested_as_of, started_at, decision_at,
                known_at, input_ids_json, output_ids_json, implementation_version,
                payload_hash, payload_json
            ) VALUES ('legacy-a2', 'legacy-run', 'A2', ?, ?, ?, ?,
                '["canonical_claim:one"]', '["candidate_thesis:legacy-candidate"]',
                'legacy', ?, '{}')""",
            (_INITIALIZED_AT, _INITIALIZED_AT, _INITIALIZED_AT, _INITIALIZED_AT, "d" * 64),
        )
        _ = connection.execute(
            """INSERT INTO candidate_theses (
                candidate_thesis_id, status, created_at, known_at, candidate_json
            ) VALUES ('legacy-candidate', 'researching', ?, ?, ?)""",
            (
                _INITIALIZED_AT,
                _INITIALIZED_AT,
                json.dumps(
                    {
                        "candidate_thesis_id": "legacy-candidate",
                        "subject": "Gold miners",
                        "direction": "long",
                        "instrument_reference": "VanEck Gold Miners ETF",
                        "instrument": "GDX",
                        "theme": "Gold miners",
                        "horizon_class": "medium_term",
                        "discovery_basis": {"source_claim_keys": ["claim:gold"]},
                        "causal_mechanisms": ["Operating leverage"],
                        "regime_assumptions": ["Stable funding"],
                        "created_at": "2026-08-01T12:00:00Z",
                        "known_at": "2026-08-01T12:00:00Z",
                    }
                ),
            ),
        )
        _ = connection.execute(
            """INSERT INTO planned_research_tasks (
                task_id, candidate_thesis_id, status, created_at, known_at, run_id, task_json
            ) VALUES ('legacy-planned-task', 'legacy-candidate', 'pending', ?, ?, 'legacy-run', '{}')""",
            (_INITIALIZED_AT, _INITIALIZED_AT),
        )
        for session_id, status, stop_reason, query_count, fetch_count in (
            ("legacy-active", "active", None, 8, 15),
            ("legacy-terminal", "stopped", "evidence_standard_met", 12, 24),
        ):
            _ = connection.execute(
                """INSERT INTO research_sessions (
                    session_id, run_id, scope_kind, scope_subject_id, started_at,
                    deadline_at, maximum_rounds, maximum_queries, maximum_fetches,
                    query_count, fetch_count, status, stop_reason, session_json
                ) VALUES (?, 'legacy-run', 'candidate_thesis', 'legacy-candidate', ?, ?,
                    3, 12, 24, ?, ?, ?, ?, '{}')""",
                (
                    session_id,
                    _INITIALIZED_AT,
                    "2026-08-01T12:10:00+00:00",
                    query_count,
                    fetch_count,
                    status,
                    stop_reason,
                ),
            )
        _ = connection.execute(
            """INSERT INTO research_tasks VALUES (
                'legacy-searched-task', 'legacy-active', 1, 'PRIMARY',
                '  Issuer   Backlog  ', 'searched', ?, '{}'
            )""",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            """INSERT INTO research_tasks VALUES (
                'legacy-pending-task', 'legacy-active', 2, 'primary',
                'Issuer cash flow', 'pending', ?, '{}'
            )""",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            """INSERT INTO research_candidate_summaries (
                summary_id, session_id, run_id, known_at, summary_hash, summary_json
            ) VALUES ('legacy-summary', 'legacy-terminal', 'legacy-run', ?, ?, '{}')""",
            (_INITIALIZED_AT, "e" * 64),
        )
        _ = connection.execute(
            """INSERT INTO source_definition_revisions (
                definition_hash, source_id, registry_version, provenance_group,
                definition_json, registered_at
            ) VALUES ('fx-definition', 'fx-evolution-youtube', 'legacy',
                'fx-evolution', '{}', ?)""",
            (_INITIALIZED_AT,),
        )
        for index in range(6):
            video_id = "video-one" if index < 4 else "video-two"
            _ = connection.execute(
                """INSERT INTO source_items (
                    source_item_id, content_version, source_id, source_definition_hash,
                    canonical_uri, discovered_at
                ) VALUES (?, ?, 'fx-evolution-youtube', 'fx-definition', ?, ?)""",
                (video_id, f"version-{index}", f"https://video.test/{video_id}", _INITIALIZED_AT),
            )
        delta = (
            importlib.resources.files("money_pit.storage.sql")
            .joinpath("schema_0_0_4_delta.sql")
            .read_text(encoding="utf-8")
        )
        _ = connection.executescript(delta)
        migrations_module._backfill_interpretation_work(connection)  # pyright: ignore[reportPrivateUsage]
        migrations_module._backfill_discovery_work(connection)  # pyright: ignore[reportPrivateUsage]
        migrations_module._backfill_research_jobs(connection)  # pyright: ignore[reportPrivateUsage]
        predecessor_delta = (
            importlib.resources.files("money_pit.storage.sql")
            .joinpath("schema_0_0_5_delta.sql")
            .read_text(encoding="utf-8")
        )
        _ = connection.executescript(predecessor_delta)
        fingerprint = catalog_fingerprint(connection)
        _ = connection.execute(
            """UPDATE schema_metadata SET release = ?, schema_fingerprint = ? WHERE singleton = 1""",
            (_PREDECESSOR_RELEASE, fingerprint),
        )
        connection.commit()
    finally:
        connection.close()
    return fingerprint


def _create_live_shape_predecessor(path: Path) -> tuple[tuple[str, str], ...]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        predecessor = migrations_module._load_predecessor_baseline_sql()  # pyright: ignore[reportPrivateUsage]
        _ = connection.executescript(predecessor)
        fingerprint = catalog_fingerprint(connection)
        _ = connection.execute(
            "INSERT INTO schema_metadata VALUES (1, ?, '0.0.5', ?, ?)",
            (APP_NAME, fingerprint, _INITIALIZED_AT),
        )
        _ = connection.execute(
            "INSERT INTO execution_control VALUES (1, 1, ?, 'tester', 'preserve', 'policy-1')",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            """INSERT INTO runs
            (run_id, requested_as_of, started_at, known_at, through_stage,
             source_config_hash, intelligence_config_hash, manifest_json)
            VALUES ('live-run', ?, ?, ?, 'A4', 'sources', 'intelligence', '{}')""",
            (_INITIALIZED_AT, _INITIALIZED_AT, _INITIALIZED_AT),
        )
        candidates = (
            ("gdx-tactical", "GDX tactical", "GDX", "Gold miners", "tactical"),
            ("gdx-medium", "GDX medium", "GDX", "Gold miners", "medium_term"),
            ("bonds", "Long bonds", "TLT", "Duration", "medium_term"),
            ("coreweave", "CoreWeave", "CRWV", "AI infrastructure", "medium_term"),
            ("ice", "Intercontinental Exchange", "ICE", "Market infrastructure", "medium_term"),
            ("other-one", "Copper", "COPX", "Copper miners", "structural"),
            ("other-two", "Japan banks", "MUFG", "Japan banks", "medium_term"),
            ("other-three", "Natural gas", "UNG", "Natural gas", "tactical"),
        )
        for candidate_id, subject, instrument, theme, horizon in candidates:
            payload = json.dumps(
                {
                    "candidate_thesis_id": candidate_id,
                    "subject": subject,
                    "direction": "long",
                    "instrument_reference": instrument,
                    "instrument": instrument,
                    "theme": theme,
                    "horizon_class": horizon,
                    "discovery_basis": {"source_claim_keys": [f"claim:{candidate_id}"]},
                    "causal_mechanisms": [f"mechanism:{candidate_id}"],
                    "regime_assumptions": ["stable funding"],
                    "status": "open",
                    "created_at": _INITIALIZED_AT,
                    "known_at": _INITIALIZED_AT,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            _ = connection.execute(
                """INSERT INTO candidate_theses
                (candidate_thesis_id, status, created_at, known_at, candidate_json)
                VALUES (?, 'open', ?, ?, ?)""",
                (candidate_id, _INITIALIZED_AT, _INITIALIZED_AT, payload),
            )
        task_payloads: dict[str, dict[str, object]] = {}
        meaningful = {
            "gdx-tactical": 3,
            "bonds": 2,
            "coreweave": 2,
            "ice": 2,
        }
        task_number = 0
        for candidate_id, count in meaningful.items():
            for index in range(count):
                task_id = f"task:{candidate_id}:{index}"
                task_payloads[task_id] = {
                    "candidate_thesis_id": candidate_id,
                    "provider": "sec",
                    "query": f"{candidate_id} material question {index}",
                    "purpose": "Verify a material premise",
                    "material_claim_keys": [f"claim:{candidate_id}"],
                    "maximum_results": 5,
                }
                task_number += 1
        while task_number < 65:
            candidate_id = "other-three"
            task_id = f"task:backlog:{task_number}"
            task_payloads[task_id] = {
                "candidate_thesis_id": candidate_id,
                "provider": "sec",
                "query": f"backlog material question {task_number}",
                "purpose": "Verify a material premise",
                "material_claim_keys": [f"claim:{candidate_id}"],
                "maximum_results": 5,
            }
            task_number += 1
        for task_id, payload in task_payloads.items():
            _ = connection.execute(
                """INSERT INTO planned_research_tasks
                (task_id, candidate_thesis_id, status, created_at, known_at, task_json)
                VALUES (?, ?, 'pending', ?, ?, ?)""",
                (
                    task_id,
                    str(payload["candidate_thesis_id"]),
                    _INITIALIZED_AT,
                    _INITIALIZED_AT,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                ),
            )
        job_tasks = (
            ("job:gdx-tactical:1", "gdx-tactical", ("task:gdx-tactical:0",)),
            ("job:gdx-tactical:0", "gdx-tactical", ()),
            (
                "job:gdx-tactical:3",
                "gdx-tactical",
                ("task:gdx-tactical:0", "task:gdx-tactical:1", "task:gdx-tactical:2"),
            ),
            ("job:gdx-medium:0", "gdx-medium", ()),
            ("job:bonds:0", "bonds", ()),
            ("job:bonds:2", "bonds", ("task:bonds:0", "task:bonds:1")),
            ("job:coreweave:0", "coreweave", ()),
            ("job:coreweave:2", "coreweave", ("task:coreweave:0", "task:coreweave:1")),
            ("job:ice:0", "ice", ()),
            ("job:ice:2", "ice", ("task:ice:0", "task:ice:1")),
            ("job:other-one", "other-one", ()),
            ("job:other-two", "other-two", ()),
            ("job:other-three", "other-three", ()),
        )
        for index, (job_id, candidate_id, task_ids) in enumerate(job_tasks):
            tasks = [task_payloads[task_id] for task_id in task_ids]
            payload = json.dumps(
                {
                    "candidate_id": candidate_id,
                    "premises": {"tasks": tasks, "material_claims": []},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            _ = connection.execute(
                """INSERT INTO research_jobs
                (job_id, candidate_thesis_id, premise_fingerprint, status, created_at, job_json)
                VALUES (?, ?, ?, 'pending', ?, ?)""",
                (job_id, candidate_id, f"{index + 1:064x}", _INITIALIZED_AT, payload),
            )
        for index, (job_id, _candidate_id, _task_ids) in enumerate(job_tasks[:11]):
            final_evidence_satisfied = index in {2, 3}
            observation_id = "observation:operational" if index in {0, 1} else f"observation:{index}"
            unit_payload = json.dumps(
                {
                    "final_context": {
                        "evidence_standard_satisfied": final_evidence_satisfied,
                        "accepted_evidence_ids": ([f"evidence:{index}"] if final_evidence_satisfied else []),
                    },
                    "planner_history": (
                        [{"evidence_standard_satisfied": True, "query": "stale exploratory signal"}]
                        if index == 1
                        else []
                    ),
                    "observation_ids": [observation_id],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            completed = final_evidence_satisfied
            _ = connection.execute(
                """INSERT INTO synthesis_units
                (unit_id, research_job_id, input_fingerprint, status, created_at,
                 claimed_run_id, claimed_at, completed_at, checkpoint_fingerprint,
                 validated_output_json, unit_payload_hash, unit_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'null', ?, ?)""",
                (
                    f"synthesis-unit:{index}",
                    job_id,
                    f"{index + 101:064x}",
                    "completed" if completed else "pending",
                    _INITIALIZED_AT,
                    "live-run" if completed else None,
                    _INITIALIZED_AT if completed else None,
                    _INITIALIZED_AT if completed else None,
                    hashlib.sha256(unit_payload.encode()).hexdigest(),
                    unit_payload,
                ),
            )
            if completed:
                _ = connection.execute(
                    """INSERT INTO synthesis_outputs
                    (output_id, unit_id, output_fingerprint, created_at,
                     output_record_ids_json, output_json)
                    VALUES (?, ?, ?, ?, '[]', '{}')""",
                    (f"synthesis-output:{index}", f"synthesis-unit:{index}", f"{index + 201:064x}", _INITIALIZED_AT),
                )
        immutable_rows = cast(
            "list[tuple[object, object]]",
            connection.execute(
                "SELECT candidate_thesis_id, candidate_json FROM candidate_theses ORDER BY candidate_thesis_id"
            ).fetchall(),
        )
        immutable_payloads = tuple((str(row[0]), str(row[1])) for row in immutable_rows)
        connection.commit()
    finally:
        connection.close()
    return immutable_payloads


def _predecessor_data_snapshot(
    path: Path,
    *,
    table_names: tuple[str, ...] | None = None,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    connection = sqlite3.connect(path)
    try:
        name_rows = cast(
            "list[tuple[object]]",
            connection.execute(
                """SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                  AND name != 'schema_metadata'
                ORDER BY name"""
            ).fetchall(),
        )
        names = table_names or tuple(str(row[0]) for row in name_rows)
        return tuple(
            (
                name,
                tuple(
                    sorted(
                        cast(
                            "list[tuple[object, ...]]",
                            connection.execute(f'SELECT * FROM "{name}"').fetchall(),  # noqa: S608
                        ),
                        key=repr,
                    )
                ),
            )
            for name in names
        )
    finally:
        connection.close()


def _logical_snapshot(path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute(
                "SELECT application_id, release, schema_fingerprint, initialized_at FROM schema_metadata"
            ).fetchall(),
            connection.execute("SELECT * FROM source_cursors").fetchall(),
            connection.execute("SELECT * FROM execution_control").fetchall(),
            connection.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
                ORDER BY type, name, tbl_name, sql"""
            ).fetchall(),
        )
    finally:
        connection.close()


def _seed_partially_interpreted_media_bundle(
    path: Path,
    *,
    successful_asset_count: int = 24,
) -> tuple[str, ...]:
    asset_ids = tuple(f"{index:064x}" for index in range(1, 26))
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute(
            """UPDATE source_definition_revisions
            SET definition_json = '{"allowed_uses":["interpretation"]}'
            WHERE definition_hash = 'fx-definition'"""
        )
        for index, asset_id in enumerate(asset_ids, start=1):
            _ = connection.execute(
                "INSERT INTO evidence_assets VALUES (?, ?, ?, '{}')",
                (asset_id, asset_id, f"assets/{asset_id}"),
            )
            _ = connection.execute(
                """INSERT INTO evidence_asset_acquisitions VALUES (?, ?, 'video-two',
                'version-5', 'fx-definition', ?, 'text/plain')""",
                (f"acquisition-{index}", asset_id, _INITIALIZED_AT),
            )
            fragment_id = f"fragment-{index}"
            extracted_text = f"Evidence from media asset {index}"
            _ = connection.execute(
                """INSERT INTO evidence_fragments (
                    fragment_id, asset_id, fragment_kind, locator_json,
                    extracted_text, extraction_method
                ) VALUES (?, ?, 'web_span', ?, ?, 'legacy-media')""",
                (
                    fragment_id,
                    asset_id,
                    f'{{"kind":"text","start_offset":0,"end_offset":{len(extracted_text)}}}',
                    extracted_text,
                ),
            )
            _ = connection.execute(
                """INSERT INTO evidence_processing_attempts VALUES (?, 'video-two',
                'version-5', ?, 'media', 'legacy', ?, ?, 'succeeded', NULL, NULL, ?)""",
                (
                    f"processing-{index}",
                    asset_id,
                    _INITIALIZED_AT,
                    _INITIALIZED_AT,
                    f'["{fragment_id}"]',
                ),
            )
            if index <= successful_asset_count:
                _ = connection.execute(
                    """INSERT INTO claim_interpretation_attempts VALUES (?, 'video-two',
                    'version-5', ?, 'legacy-run', 'legacy-interpreter', ?, ?, ?, 'succeeded',
                    NULL, NULL, '[]')""",
                    (
                        f"interpretation-{index}",
                        asset_id,
                        _INITIALIZED_AT,
                        _INITIALIZED_AT,
                        _INITIALIZED_AT,
                    ),
                )
        migrations_module._backfill_interpretation_work(connection)  # pyright: ignore[reportPrivateUsage]
        connection.commit()
    finally:
        connection.close()
    return asset_ids


def _index_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = cast(
        "list[sqlite3.Row]",
        connection.execute("PRAGMA index_info(claim_interpretation_success_idx)").fetchall(),
    )
    return tuple(str(cast("object", row[2])) for row in rows)


def _row_values(row: sqlite3.Row | None) -> tuple[object, ...] | None:
    return None if row is None else tuple(row)


def test_load_baseline_sql_rejects_application_schema_release_mismatch_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor_loaded = False

    def observe_predecessor_load() -> str:
        nonlocal predecessor_loaded
        predecessor_loaded = True
        return ""

    monkeypatch.setattr(migrations_module, "APP_VERSION", "9.9.9")
    monkeypatch.setattr(migrations_module, "_load_predecessor_baseline_sql", observe_predecessor_load)

    with pytest.raises(BaselineDiscoveryError):
        _ = migrations_module.load_baseline_sql()

    assert predecessor_loaded is False


def test_initialize_installs_the_exact_release_baseline(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")

    database.initialize()

    with database.transaction() as connection:
        row = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT application_id, release, schema_fingerprint FROM schema_metadata",
            ).fetchone(),
        )
        assert row is not None
        assert tuple(row) == (APP_NAME, APP_VERSION, expected_schema_fingerprint())
        assert catalog_fingerprint(connection) == expected_schema_fingerprint()


def test_initialize_indexes_successful_interpretations_by_exact_asset(tmp_path: Path) -> None:
    database = Database(tmp_path / "intelligence.sqlite3")
    database.initialize()

    with database.transaction() as connection:
        index_columns = _index_columns(connection)

    assert index_columns == (
        "source_item_id",
        "content_version",
        "asset_id",
        "interpreter_version",
    )


def test_initialize_atomically_migrates_exact_0_0_5_and_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    predecessor_snapshot = _predecessor_data_snapshot(path)

    Database(path).initialize()
    migrated_predecessor_snapshot = _predecessor_data_snapshot(
        path,
        table_names=tuple(name for name, _rows in predecessor_snapshot),
    )

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = _row_values(
            cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT application_id, release, schema_fingerprint, initialized_at FROM schema_metadata"
                ).fetchone(),
            )
        )
        source_cursor = _row_values(
            cast("sqlite3.Row | None", connection.execute("SELECT * FROM source_cursors").fetchone())
        )
        execution_control = _row_values(
            cast("sqlite3.Row | None", connection.execute("SELECT * FROM execution_control").fetchone())
        )
        index_columns = _index_columns(connection)
        migrated_fingerprint = catalog_fingerprint(connection)
        discovery_rows = cast(
            "list[sqlite3.Row]",
            connection.execute("SELECT status, count(*) FROM discovery_units GROUP BY status").fetchall(),
        )
        discovery_backfill = [tuple(row) for row in discovery_rows]
        research_rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT status, search_count, accepted_fetch_count
                FROM research_jobs ORDER BY status"""
            ).fetchall(),
        )
        research_backfill = [tuple(row) for row in research_rows]
        source_shape_row = cast(
            "sqlite3.Row | None",
            connection.execute("SELECT count(*), count(DISTINCT source_item_id) FROM source_items").fetchone(),
        )
        source_shape = None if source_shape_row is None else tuple(source_shape_row)
        lineage_shape = _row_values(
            cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT
                        (SELECT count(*) FROM candidate_discovery_origins),
                        (SELECT count(*) FROM planned_research_task_origins),
                        (SELECT count(*) FROM synthesis_units WHERE status = 'pending')"""
                ).fetchone(),
            )
        )
    finally:
        connection.close()
    assert (
        metadata,
        migrated_predecessor_snapshot,
        source_cursor,
        execution_control,
        index_columns,
        migrated_fingerprint,
        discovery_backfill,
        research_backfill,
        source_shape,
        lineage_shape,
    ) == (
        (APP_NAME, APP_VERSION, expected_schema_fingerprint(), _INITIALIZED_AT),
        predecessor_snapshot,
        ("source", "sync", '{"page":2}', _INITIALIZED_AT),
        (1, 1, _INITIALIZED_AT, "tester", "preserve", "policy-1"),
        ("source_item_id", "content_version", "asset_id", "interpreter_version"),
        expected_schema_fingerprint(),
        [("completed", 1)],
        [("active", 6, 12), ("terminal", 6, 12)],
        (6, 2),
        (1, 1, 1),
    )


def test_initialize_reconciles_live_semantic_histories_without_losing_paid_work(tmp_path: Path) -> None:
    path = tmp_path / "live-shape.sqlite3"
    immutable_candidates = _create_live_shape_predecessor(path)
    predecessor_snapshot = _predecessor_data_snapshot(path)

    Database(path).initialize()
    migrated_predecessor_snapshot = _predecessor_data_snapshot(
        path,
        table_names=tuple(name for name, _rows in predecessor_snapshot),
    )

    with Database(path).read_only_transaction() as connection:
        preserved_counts = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM candidate_theses),
                    (SELECT count(*) FROM research_jobs),
                    (SELECT count(*) FROM planned_research_tasks),
                    (SELECT count(*) FROM synthesis_units),
                    (SELECT count(*) FROM synthesis_outputs)"""
                ).fetchone(),
            )
        )
        candidate_rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                "SELECT candidate_thesis_id, candidate_json FROM candidate_theses ORDER BY candidate_thesis_id"
            ).fetchall(),
        )
        migrated_candidates = tuple((cast("str", row[0]), cast("str", row[1])) for row in candidate_rows)
        research_dispositions = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    sum(disposition = 'current'), sum(disposition = 'superseded'),
                    count(DISTINCT case_id)
                    FROM research_job_semantics"""
                ).fetchone(),
            )
        )
        expanded_head_rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT semantic.job_id, count(task.task_id)
                FROM research_job_semantics semantic
                LEFT JOIN research_job_tasks task ON task.job_id = semantic.job_id
                WHERE semantic.disposition = 'current'
                  AND semantic.job_id IN ('job:gdx-tactical:3', 'job:bonds:2',
                    'job:coreweave:2', 'job:ice:2')
                GROUP BY semantic.job_id ORDER BY semantic.job_id"""
            ).fetchall(),
        )
        expanded_heads = tuple((cast("str", row[0]), cast("int", row[1])) for row in expanded_head_rows)
        gdx_review_row = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(*) FROM hypothesis_reviews
                WHERE (subject_candidate_id = 'gdx-medium' AND comparison_candidate_id = 'gdx-tactical')
                   OR (subject_candidate_id = 'gdx-tactical' AND comparison_candidate_id = 'gdx-medium')"""
            ).fetchone(),
        )
        insufficient_row = cast(
            "sqlite3.Row",
            connection.execute(
                "SELECT count(*) FROM synthesis_material_states WHERE eligibility = 'insufficient_evidence'"
            ).fetchone(),
        )
        synthesis_rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT unit.unit_id, unit.status, material.eligibility, semantic.disposition
                FROM synthesis_units unit
                JOIN synthesis_unit_semantics semantic USING (unit_id)
                JOIN synthesis_material_states material USING (material_state_id)
                ORDER BY unit.unit_id"""
            ).fetchall(),
        )
        synthesis_eligibility = tuple(
            (
                cast("str", row[0]),
                cast("str", row[1]),
                cast("str", row[2]),
                cast("str", row[3]),
            )
            for row in synthesis_rows
        )
        operational_row = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(DISTINCT material_state_id) FROM synthesis_unit_semantics
                WHERE unit_id IN ('synthesis-unit:0', 'synthesis-unit:1')"""
            ).fetchone(),
        )
        gdx_review = cast("int", gdx_review_row[0])
        insufficient_states = cast("int", insufficient_row[0])
        operational_material_states = cast("int", operational_row[0])

    assert (
        preserved_counts,
        migrated_predecessor_snapshot,
        migrated_candidates,
        research_dispositions,
        expanded_heads,
        gdx_review,
        insufficient_states,
        synthesis_eligibility,
        operational_material_states,
    ) == (
        (8, 13, 65, 11, 2),
        predecessor_snapshot,
        immutable_candidates,
        (8, 5, 8),
        (
            ("job:bonds:2", 2),
            ("job:coreweave:2", 2),
            ("job:gdx-tactical:3", 3),
            ("job:ice:2", 2),
        ),
        1,
        8,
        (
            ("synthesis-unit:0", "pending", "insufficient_evidence", "superseded"),
            ("synthesis-unit:1", "pending", "insufficient_evidence", "superseded"),
            ("synthesis-unit:10", "pending", "insufficient_evidence", "unavailable"),
            ("synthesis-unit:2", "completed", "unavailable", "unavailable"),
            ("synthesis-unit:3", "completed", "unavailable", "unavailable"),
            ("synthesis-unit:4", "pending", "insufficient_evidence", "superseded"),
            ("synthesis-unit:5", "pending", "insufficient_evidence", "unavailable"),
            ("synthesis-unit:6", "pending", "insufficient_evidence", "superseded"),
            ("synthesis-unit:7", "pending", "insufficient_evidence", "unavailable"),
            ("synthesis-unit:8", "pending", "insufficient_evidence", "superseded"),
            ("synthesis-unit:9", "pending", "insufficient_evidence", "unavailable"),
        ),
        1,
    )


def test_migrated_unreconstructable_paid_output_cannot_enter_portfolio_review(tmp_path: Path) -> None:
    path = tmp_path / "unreconstructable-output.sqlite3"
    _ = _create_live_shape_predecessor(path)
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute(
            "INSERT INTO theses (thesis_id, candidate_thesis_id, created_at) VALUES ('thesis:1', 'gdx-tactical', ?)",
            (_INITIALIZED_AT,),
        )
        _ = connection.execute(
            """INSERT INTO thesis_revisions
            (revision_id, thesis_id, revision_number, status, created_at, known_at,
             review_at, valid_until, revision_json)
            VALUES ('revision:legacy', 'thesis:1', 1, 'candidate', ?, ?, ?, NULL, '{}')""",
            (_INITIALIZED_AT, _INITIALIZED_AT, _INITIALIZED_AT),
        )
        _ = connection.execute(
            """UPDATE synthesis_outputs SET output_record_ids_json = '["revision:legacy"]'
            WHERE output_id = 'synthesis-output:2'"""
        )
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    database.initialize()
    with database.read_only_transaction() as migrated:
        migrated_state = tuple(
            cast(
                "sqlite3.Row",
                migrated.execute(
                    """SELECT material.eligibility, semantic.disposition
                    FROM synthesis_unit_semantics semantic
                    JOIN synthesis_material_states material USING (material_state_id)
                    WHERE semantic.unit_id = 'synthesis-unit:2'"""
                ).fetchone(),
            )
        )
    portfolio = SemanticIntelligenceRepository(database).portfolio_eligibility_for_revision("revision:legacy")

    assert (migrated_state, portfolio.model_dump()) == (
        ("unavailable", "unavailable"),
        {
            "revision_id": "revision:legacy",
            "intelligence_available": False,
            "synthesis_evidence_sufficient": False,
        },
    )


def test_migrated_exact_semantics_reconcile_without_creating_duplicate_work(tmp_path: Path) -> None:
    path = tmp_path / "provider-free-reconciliation.sqlite3"
    _ = _create_live_shape_predecessor(path)
    database = Database(path)
    database.initialize()
    semantic = SemanticIntelligenceRepository(database)
    work = IntelligenceWorkRepository(database)
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    with database.read_only_transaction() as connection:
        before = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM research_jobs),
                    (SELECT count(*) FROM research_cases),
                    (SELECT count(*) FROM research_job_tasks),
                    (SELECT count(*) FROM synthesis_units)"""
                ).fetchone(),
            )
        )

    _ensure_semantic_research_heads(
        candidate_ids=("gdx-tactical",),
        source_id=None,
        requested_as_of=datetime.fromisoformat(_INITIALIZED_AT),
        state={},
        claims=ClaimRepository(database, refresh_policy=claim_refresh_policy(config.intelligence)),
        theses=ThesisRepository(database),
        tasks=PlannedResearchTaskStore(database, semantic),
        work=work,
        semantic=semantic,
        run_id="live-run",
        recorded_at=datetime.fromisoformat(_INITIALIZED_AT),
    )

    with database.read_only_transaction() as connection:
        after = tuple(
            cast(
                "sqlite3.Row",
                connection.execute(
                    """SELECT
                    (SELECT count(*) FROM research_jobs),
                    (SELECT count(*) FROM research_cases),
                    (SELECT count(*) FROM research_job_tasks),
                    (SELECT count(*) FROM synthesis_units)"""
                ).fetchone(),
            )
        )

    assert (before, after) == ((13, 8, 10, 11), (13, 8, 10, 11))


def test_initialize_preserves_provider_and_execution_wave_provenance_from_0_0_5(tmp_path: Path) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute(
            """INSERT INTO candidate_theses
            (candidate_thesis_id, status, created_at, known_at, candidate_json)
            VALUES ('wave-candidate', 'researching', ?, ?, ?)""",
            (
                _INITIALIZED_AT,
                _INITIALIZED_AT,
                json.dumps(
                    {
                        "candidate_thesis_id": "wave-candidate",
                        "subject": "Gold miners",
                        "direction": "long",
                        "instrument": "GDX",
                        "theme": "Gold miners",
                        "horizon_class": "medium_term",
                        "discovery_basis": {"source_claim_keys": ["claim:gold"]},
                        "causal_mechanisms": [],
                        "regime_assumptions": [],
                        "created_at": "2026-08-01T12:00:00Z",
                        "known_at": "2026-08-01T12:00:00Z",
                    }
                ),
            ),
        )
        _ = connection.execute(
            """INSERT INTO research_jobs
            (job_id, candidate_thesis_id, premise_fingerprint, status, created_at,
             claimed_run_id, claimed_at, wave_count, search_count, accepted_fetch_count, job_json)
            VALUES ('wave-job', 'wave-candidate', ?, 'active', ?, 'legacy-run', ?, 0, 0, 0, '{}')""",
            ("a" * 64, _INITIALIZED_AT, _INITIALIZED_AT),
        )
        _ = connection.execute(
            """INSERT INTO research_sessions
            (session_id, run_id, scope_kind, scope_subject_id, started_at, deadline_at,
             maximum_rounds, maximum_queries, maximum_fetches, query_count, fetch_count,
             status, session_json)
            VALUES ('wave-session', 'legacy-run', 'candidate_thesis', 'wave-candidate', ?, ?,
            3, 6, 12, 1, 0, 'active', '{}')""",
            (_INITIALIZED_AT, "2026-08-01T12:10:00+00:00"),
        )
        for wave_id, wave_number, phase, execution_json in (
            ("provider-wave", 1, "provider_completed", '{"search_count_delta":1,"accepted_fetch_count_delta":0}'),
            (
                "execution-wave",
                2,
                "execution_completed",
                '{"search_count_delta":0,"accepted_fetch_count_delta":0,"execution":{},"context":null}',
            ),
        ):
            _ = connection.execute(
                """INSERT INTO research_wave_results
                (wave_result_id, job_id, session_id, run_id, wave_number, phase, recorded_at,
                 provider_result_json, execution_json)
                VALUES (?, 'wave-job', 'wave-session', 'legacy-run', ?, ?, ?, '{}', ?)""",
                (wave_id, wave_number, phase, _INITIALIZED_AT, execution_json),
            )
        connection.commit()
    finally:
        connection.close()

    Database(path).initialize()

    with Database(path).read_only_transaction() as migrated:
        rows = cast(
            "list[sqlite3.Row]",
            migrated.execute(
                """SELECT wave_result_id, run_id, execution_completed_run_id, checkpointed_run_id
                FROM research_wave_results WHERE wave_result_id IN ('provider-wave', 'execution-wave')
                ORDER BY wave_result_id"""
            ).fetchall(),
        )
    assert [tuple(row) for row in rows] == [
        ("execution-wave", "legacy-run", "legacy-run", None),
        ("provider-wave", "legacy-run", None, None),
    ]


def test_initialize_backfills_only_exact_interpretation_bundle_coverage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    asset_ids = _seed_partially_interpreted_media_bundle(path)
    database = Database(path)

    database.initialize()

    with database.transaction() as connection:
        bundle_shape = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(DISTINCT bundle.bundle_id),
                    sum(chunk.status = 'completed'), sum(chunk.status = 'pending')
                FROM interpretation_bundles AS bundle
                JOIN interpretation_bundle_chunks AS chunk USING (bundle_id)"""
            ).fetchone(),
        )
    pending = EvidenceWorkStore(database).list_pending_documents(
        as_of=datetime.fromisoformat(_INITIALIZED_AT),
        source_id="fx-evolution-youtube",
        limit=100,
        interpreter_version="legacy-interpreter",
    )

    assert (
        tuple(bundle_shape),
        tuple(
            (item.document.asset.source_item_id, item.content_version, item.document.asset.asset_id) for item in pending
        ),
    ) == (
        (1, 24, 1),
        (("video-two", "version-5", asset_ids[-1]),),
    )


def test_initialize_parses_real_namespaced_a2_artifact_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    database = Database(path)

    database.initialize()

    with database.transaction() as connection:
        migrated = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT unit.subject_id,
                    json_extract(unit.unit_json, '$.input_binding') AS input_binding,
                    batch.output_candidate_ids_json
                FROM discovery_units AS unit
                JOIN discovery_batch_units AS membership USING (unit_id)
                JOIN discovery_batches AS batch USING (batch_id)
                WHERE unit.unit_kind = 'canonical_claim'"""
            ).fetchone(),
        )
    assert (
        migrated["subject_id"],
        migrated["input_binding"],
        migrated["output_candidate_ids_json"],
    ) == (
        "one",
        "canonical_claim:one",
        '["legacy-candidate"]',
    )


def test_migrated_partial_interpretation_bundle_infers_only_missing_asset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    asset_ids = _seed_partially_interpreted_media_bundle(path)
    database = Database(path)
    database.initialize()
    as_of = datetime(2026, 8, 2, tzinfo=UTC)
    config = load_application_config(scope=ConfigurationScope.INTELLIGENCE)
    configured_interpreter_version = interpretation_policy_version(
        prompt_version=APP_VERSION,
        model=config.intelligence.llm_model,
    )
    RunRepository(database).append_run(
        RunRecord(
            run_id=_INCREMENTAL_RUN_ID,
            requested_as_of=as_of,
            started_at=as_of,
            known_at=as_of,
            through_stage="A1",
            source_config_hash="a" * 64,
        )
    )
    calls: list[tuple[str, ...]] = []

    def interpret(
        request: InterpretationRequest, *, context: InferenceInvocationContext
    ) -> InferenceResult[InterpretationDraft]:
        del context
        calls.append(tuple(item.alias for item in request.evidence))
        return InferenceResult(
            output=InterpretationDraft(observations=()),
            usage=InferenceUsage(),
            request_hash="0" * 64,
        )

    node = make_interpretation_node(
        evidence=EvidenceWorkStore(database),
        admission=IntelligenceAdmissionRepository(database),
        agent=interpret,
        implementation_version=configured_interpreter_version,
        clock=lambda: as_of,
        work_repository=IntelligenceWorkRepository(database),
    )

    _ = node(
        {
            "run_id": _INCREMENTAL_RUN_ID,
            "run_dir": str(tmp_path / "runs" / _INCREMENTAL_RUN_ID),
            "requested_as_of": as_of,
            "run_started_at": as_of,
            "source_id": "fx-evolution-youtube",
        }
    )

    with database.transaction() as connection:
        bundle_shape = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT bundle.status, count(chunk.chunk_id),
                    sum(chunk.status = 'completed')
                FROM interpretation_bundles AS bundle
                JOIN interpretation_bundle_chunks AS chunk USING (bundle_id)
                GROUP BY bundle.bundle_id"""
            ).fetchone(),
        )
        new_attempts = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(*), min(asset_id) FROM claim_interpretation_attempts
                WHERE run_id = ? AND interpreter_version = ?""",
                (_INCREMENTAL_RUN_ID, configured_interpreter_version),
            ).fetchone(),
        )
        legacy_policy_reuse = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(*) FROM claim_interpretation_attempts
                WHERE interpreter_version = ? AND asset_id != ?""",
                (configured_interpreter_version, asset_ids[-1]),
            ).fetchone(),
        )

    assert (len(calls), tuple(bundle_shape), tuple(new_attempts), legacy_policy_reuse[0]) == (
        1,
        ("completed", 25, 25),
        (1, asset_ids[-1]),
        0,
    )


def test_initialize_backfills_complete_interpretation_bundle_with_one_chunk_per_asset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    asset_ids = _seed_partially_interpreted_media_bundle(path, successful_asset_count=25)
    database = Database(path)

    database.initialize()

    with database.transaction() as connection:
        completed_shape = cast(
            "sqlite3.Row",
            connection.execute(
                """SELECT count(DISTINCT bundle.bundle_id), count(chunk.chunk_id)
                FROM interpretation_bundles AS bundle
                JOIN interpretation_bundle_chunks AS chunk USING (bundle_id)
                WHERE bundle.status = 'completed' AND chunk.status = 'completed'"""
            ).fetchone(),
        )
    pending = EvidenceWorkStore(database).list_pending_documents(
        as_of=datetime.fromisoformat(_INITIALIZED_AT),
        source_id="fx-evolution-youtube",
        limit=100,
        interpreter_version="legacy-interpreter",
    )

    assert (tuple(completed_shape), len(asset_ids), pending) == ((1, 25), 25, ())


def test_initialize_restores_interrupted_legacy_research_before_pending_round(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    database = Database(path)

    database.initialize()

    with database.transaction() as connection:
        active_job_row = cast(
            "sqlite3.Row | None",
            connection.execute("SELECT job_id FROM research_jobs WHERE status = 'active'").fetchone(),
        )
        assert active_job_row is not None
        active_job_id = cast("str", active_job_row[0])
    checkpoint = IntelligenceWorkRepository(database).latest_research_checkpoint(active_job_id)
    assert checkpoint is not None
    summary, contexts = research_module._restore_research_checkpoint(checkpoint)  # pyright: ignore[reportPrivateUsage]
    assert summary is not None
    assert (
        checkpoint.wave_number,
        summary.normalized_queries,
        contexts,
    ) == (
        1,
        ("primary:issuer backlog",),
        (),
    )


def test_initialize_is_idempotent_after_0_0_5_migration(tmp_path: Path) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    database = Database(path)
    database.initialize()
    migrated = _logical_snapshot(path)

    database.initialize()

    assert _logical_snapshot(path) == migrated


def test_initialize_rolls_back_predecessor_when_post_index_migration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "intelligence.sqlite3"
    _ = _create_exact_predecessor(path)
    predecessor = _logical_snapshot(path)
    catalog_fingerprint = migrations_module.catalog_fingerprint

    def fail_after_target_index_creation(connection: sqlite3.Connection) -> str:
        fingerprint = catalog_fingerprint(connection)
        database_row = cast(
            "sqlite3.Row | None",
            connection.execute("PRAGMA database_list").fetchone(),
        )
        database_path = None if database_row is None else Path(str(cast("object", database_row[2])))
        incremental_table_row = cast(
            "sqlite3.Row | None",
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'inference_calls'"
            ).fetchone(),
        )
        incremental_table_exists = (
            incremental_table_row is not None and cast("int", cast("object", incremental_table_row[0])) == 1
        )
        if database_path is not None and database_path.resolve() == path.resolve() and incremental_table_exists:
            raise BaselineApplyError("Injected post-index migration failure.")
        return fingerprint

    monkeypatch.setattr(migrations_module, "catalog_fingerprint", fail_after_target_index_creation)

    with pytest.raises(BaselineApplyError):
        Database(path).initialize()

    assert _logical_snapshot(path) == predecessor


def test_initialize_refuses_tampered_0_0_5_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "tampered-predecessor.sqlite3"
    _ = _create_exact_predecessor(path)
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE tampered_history (value TEXT NOT NULL)")
    _ = connection.execute("INSERT INTO tampered_history VALUES ('preserve-me')")
    connection.commit()
    connection.close()
    before = _logical_snapshot(path)

    with pytest.raises(UnknownDatabaseSchemaError):
        Database(path).initialize()

    assert _logical_snapshot(path) == before


def test_initialize_refuses_an_unknown_nonempty_database_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite3"
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE user_history (value TEXT NOT NULL)")
    _ = connection.execute("INSERT INTO user_history VALUES ('preserve-me')")
    connection.commit()
    connection.close()

    with pytest.raises(UnknownDatabaseSchemaError):
        Database(path).initialize()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT value FROM user_history").fetchone() == ("preserve-me",)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name = 'schema_metadata'",
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_initialize_refuses_catalog_drift_from_the_recorded_baseline(tmp_path: Path) -> None:
    path = tmp_path / "drifted.sqlite3"
    database = Database(path)
    database.initialize()
    connection = sqlite3.connect(path)
    _ = connection.execute("CREATE TABLE unexpected_runtime_table (id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(UnknownDatabaseSchemaError):
        database.initialize()
