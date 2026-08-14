from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.evidence.work import EvidenceWorkStore
from money_pit.schemas.evidence import EvidenceAsset
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import EvidenceProcessingAttempt
from money_pit.schemas.evidence import EvidenceProcessingStatus
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.runs import RunFailureDetail
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources.service import EvidenceRepository
from money_pit.storage.database import Database
from money_pit.storage.database import TransactionMode
from money_pit.storage.runs import RunRepository
from money_pit.storage.sources import SourceRepository


if TYPE_CHECKING:
    import sqlite3


NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
ASSET_ID = "a" * 64


@dataclass(frozen=True)
class _EvidenceWorkScenario:
    database: Database
    work_store: EvidenceWorkStore
    attempt_repository: EvidenceProcessingAttemptRepository
    source_repository: SourceRepository
    evidence_repository: EvidenceRepository
    source_definition_hash: str
    document: EvidenceDocument

    def persist_acquisition(self, content_version: str) -> SourceItem:
        item = SourceItem(
            source_item_id="source:item",
            source_id="source",
            source_definition_hash=self.source_definition_hash,
            canonical_uri="https://example.test/item",
            discovered_at=NOW,
            content_version=content_version,
        )
        _ = self.source_repository.persist_discovery(
            item.source_id,
            (item,),
            next_cursor=None,
            updated_at=NOW,
        )
        _ = self.evidence_repository.persist(self.document, source_item=item)
        return item

    def persist_processing_attempt(
        self,
        item: SourceItem,
        status: EvidenceProcessingStatus,
    ) -> None:
        _ = self.attempt_repository.persist(
            EvidenceProcessingAttempt(
                attempt_id=f"attempt:{item.content_version}:{status.value}",
                source_item_id=item.source_item_id,
                content_version=item.content_version,
                asset_id=ASSET_ID,
                processor_name="text-utf8",
                processor_version="1",
                started_at=NOW,
                completed_at=NOW + timedelta(seconds=1),
                status=status,
                failure_kind="extraction" if status is EvidenceProcessingStatus.FAILED else None,
                document_id="document" if status is EvidenceProcessingStatus.SUCCEEDED else None,
                fragment_ids=("fragment",) if status is EvidenceProcessingStatus.SUCCEEDED else (),
            ),
        )


@pytest.fixture
def evidence_work_scenario(tmp_path: Path) -> _EvidenceWorkScenario:
    database = Database(tmp_path / "evidence-work.sqlite3")
    database.initialize()
    source_repository = SourceRepository(database)
    definition = SourceDefinition(
        source_id="source",
        adapter_name="test",
        locator="https://example.test",
        provenance_group="publisher",
        allowed_uses=(AllowedUse.INTERPRETATION, AllowedUse.FACTUAL_VERIFICATION),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
    )
    _ = source_repository.register_definition(
        definition,
        registry_version="1",
        registered_at=NOW,
    )
    document = EvidenceDocument(
        asset=EvidenceAsset(
            asset_id=ASSET_ID,
            content_hash=ASSET_ID,
            media_type="text/plain",
            source_item_id="source:item",
            local_path=Path("aa") / ASSET_ID,
            retrieved_at=NOW,
        ),
        fragments=(
            EvidenceFragment(
                fragment_id="fragment",
                asset_id=ASSET_ID,
                kind="web_span",
                locator=TextLocator(start_offset=0, end_offset=4),
                extracted_text="text",
                extraction_method="text-utf8",
            ),
        ),
    )
    with database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            """
            INSERT INTO runs (
                run_id, requested_as_of, started_at, known_at, through_stage,
                source_config_hash, intelligence_config_hash, portfolio_config_hash,
                execution_config_hash, manifest_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "4fa85f64-5717-4562-b3fc-2c963f66afa6",
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                "A1",
                "a" * 64,
                None,
                None,
                None,
                "{}",
            ),
        )
    return _EvidenceWorkScenario(
        database=database,
        work_store=EvidenceWorkStore(database),
        attempt_repository=EvidenceProcessingAttemptRepository(database),
        source_repository=source_repository,
        evidence_repository=EvidenceRepository(database),
        source_definition_hash=source_repository.definition_hash(definition),
        document=document,
    )


def test_begin_interpretation_preserves_the_selected_content_version_for_a_reused_asset(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    first = evidence_work_scenario.persist_acquisition("version-1")
    second = evidence_work_scenario.persist_acquisition("version-2")
    evidence_work_scenario.persist_processing_attempt(first, EvidenceProcessingStatus.SUCCEEDED)
    evidence_work_scenario.persist_processing_attempt(second, EvidenceProcessingStatus.SUCCEEDED)
    pending = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id="source",
        limit=10,
        interpreter_version="interpreter-1",
    )
    selected = next(work for work in pending if work.content_version == "version-2")

    attempt_id = evidence_work_scenario.work_store.begin_interpretation(
        selected,
        run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
        interpreter_version="interpreter-1",
        started_at=NOW + timedelta(minutes=1),
    )

    with evidence_work_scenario.database.transaction() as connection:
        row = cast(
            "tuple[str, str, str] | None",
            connection.execute(
                """
                SELECT source_item_id, content_version, asset_id
                FROM claim_interpretation_attempts WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone(),
        )
    assert row is not None
    assert tuple(row) == ("source:item", "version-2", ASSET_ID)


def test_begin_interpretation_replaces_pending_attempt_owned_by_failed_run(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    item = evidence_work_scenario.persist_acquisition("version-recovered")
    evidence_work_scenario.persist_processing_attempt(item, EvidenceProcessingStatus.SUCCEEDED)
    work = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id="source",
        limit=1,
        interpreter_version="interpreter-recovered",
    )[0]
    failed_run_id = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
    first_attempt_id = evidence_work_scenario.work_store.begin_interpretation(
        work,
        run_id=failed_run_id,
        interpreter_version="interpreter-recovered",
        started_at=NOW + timedelta(minutes=1),
    )
    runs = RunRepository(evidence_work_scenario.database)
    failed_run = RunRecord(
        run_id=failed_run_id,
        requested_as_of=NOW,
        started_at=NOW,
        known_at=NOW,
        through_stage="A1",
        source_config_hash="a" * 64,
    )
    with evidence_work_scenario.database.transaction(TransactionMode.WRITE) as connection:
        _ = connection.execute(
            "UPDATE runs SET manifest_json = ? WHERE run_id = ?",
            (failed_run.model_dump_json(), failed_run_id),
        )
    runs.append_terminal_event(
        RunTerminalEvent(
            run_id=failed_run_id,
            status=RunTerminalStatus.FAILED,
            completed_at=NOW + timedelta(minutes=2),
            known_at=NOW + timedelta(minutes=2),
            failure_kind="ArtifactValidationError",
            failure_detail=RunFailureDetail(retryable=False),
        )
    )
    recovery_run_id = "d9428888-122b-4df8-b24f-f10e8a9bd799"
    runs.append_run(
        RunRecord(
            run_id=recovery_run_id,
            requested_as_of=NOW + timedelta(minutes=3),
            started_at=NOW + timedelta(minutes=3),
            known_at=NOW + timedelta(minutes=3),
            through_stage="A1",
            source_config_hash="a" * 64,
        )
    )

    replacement_attempt_id = evidence_work_scenario.work_store.begin_interpretation(
        work,
        run_id=recovery_run_id,
        interpreter_version="interpreter-recovered",
        started_at=NOW + timedelta(minutes=3),
    )

    with evidence_work_scenario.database.read_only_transaction() as connection:
        rows = cast(
            "list[sqlite3.Row]",
            connection.execute(
                """SELECT attempt_id, run_id, outcome, failure_kind
                FROM claim_interpretation_attempts WHERE interpreter_version = 'interpreter-recovered'
                ORDER BY started_at"""
            ).fetchall(),
        )
    records = tuple(cast("tuple[str, str, str, str | None]", tuple(row)) for row in rows)
    assert records == (
        (first_attempt_id, failed_run_id, "failed", "OwningRunFailed"),
        (replacement_attempt_id, recovery_run_id, "pending", None),
    )


def test_list_pending_documents_requires_succeeded_processing_for_the_exact_acquisition(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    _ = evidence_work_scenario.persist_acquisition("version-unprocessed")
    failed = evidence_work_scenario.persist_acquisition("version-failed")
    succeeded = evidence_work_scenario.persist_acquisition("version-succeeded")
    evidence_work_scenario.persist_processing_attempt(failed, EvidenceProcessingStatus.FAILED)
    evidence_work_scenario.persist_processing_attempt(succeeded, EvidenceProcessingStatus.SUCCEEDED)

    pending = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id=None,
        limit=10,
        interpreter_version="interpreter-1",
    )

    assert tuple(
        (work.document.asset.source_item_id, work.content_version, work.document.asset.asset_id) for work in pending
    ) == (("source:item", "version-succeeded", ASSET_ID),)


def test_list_pending_documents_excludes_sources_that_forbid_interpretation(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    excluded_definition = SourceDefinition(
        source_id="source",
        adapter_name="test",
        locator="https://example.test",
        provenance_group="publisher",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
    )
    _ = evidence_work_scenario.source_repository.register_definition(
        excluded_definition,
        registry_version="2",
        registered_at=NOW,
    )
    excluded_hash = evidence_work_scenario.source_repository.definition_hash(excluded_definition)
    item = SourceItem(
        source_item_id="source:item",
        source_id="source",
        source_definition_hash=excluded_hash,
        canonical_uri="https://example.test/item",
        discovered_at=NOW,
        content_version="version-excluded",
    )
    _ = evidence_work_scenario.source_repository.persist_discovery(
        item.source_id,
        (item,),
        next_cursor=None,
        updated_at=NOW,
    )
    _ = evidence_work_scenario.evidence_repository.persist(evidence_work_scenario.document, source_item=item)
    evidence_work_scenario.persist_processing_attempt(item, EvidenceProcessingStatus.SUCCEEDED)

    pending = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id="source",
        limit=10,
        interpreter_version="interpreter-1",
    )

    assert pending == ()


def test_interpretation_work_rejects_a_source_without_interpretation_use(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    definition = SourceDefinition(
        source_id="verification-only",
        adapter_name="test",
        locator="https://verification.example.test",
        provenance_group="verification-publisher",
        allowed_uses=(AllowedUse.FACTUAL_VERIFICATION,),
        trust_settings=(
            SourceTrustSetting(
                category=TrustCategory.FACTUAL,
                level=TrustLevel.INDEPENDENT_SECONDARY,
            ),
        ),
    )
    _ = evidence_work_scenario.source_repository.register_definition(
        definition,
        registry_version="1",
        registered_at=NOW,
    )
    item = SourceItem(
        source_item_id="verification-only:item",
        source_id=definition.source_id,
        source_definition_hash=evidence_work_scenario.source_repository.definition_hash(definition),
        canonical_uri="https://verification.example.test/item",
        discovered_at=NOW,
        content_version="version-1",
    )
    document = evidence_work_scenario.document.model_copy(
        update={
            "asset": evidence_work_scenario.document.asset.model_copy(
                update={"source_item_id": item.source_item_id},
            ),
        },
    )
    _ = evidence_work_scenario.source_repository.persist_discovery(
        definition.source_id,
        (item,),
        next_cursor=None,
        updated_at=NOW,
    )
    _ = evidence_work_scenario.evidence_repository.persist(document, source_item=item)
    _ = evidence_work_scenario.attempt_repository.persist(
        EvidenceProcessingAttempt(
            attempt_id="verification-only-processing",
            source_item_id=item.source_item_id,
            content_version=item.content_version,
            asset_id=ASSET_ID,
            processor_name="text-utf8",
            processor_version="1",
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            status=EvidenceProcessingStatus.SUCCEEDED,
            document_id="document",
            fragment_ids=("fragment",),
        ),
    )

    pending = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id=definition.source_id,
        limit=10,
        interpreter_version="interpreter-1",
    )

    assert pending == ()
    with pytest.raises(KeyError):
        _ = evidence_work_scenario.work_store.begin_interpretation(
            EvidenceInterpretationWork(document=document, content_version=item.content_version),
            run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
            interpreter_version="interpreter-1",
            started_at=NOW + timedelta(minutes=1),
        )


def test_interpretation_failures_back_off_twice_then_quarantine(
    evidence_work_scenario: _EvidenceWorkScenario,
) -> None:
    item = evidence_work_scenario.persist_acquisition("version-retry")
    evidence_work_scenario.persist_processing_attempt(item, EvidenceProcessingStatus.SUCCEEDED)
    work = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(minutes=1),
        source_id="source",
        limit=1,
        interpreter_version="interpreter-retry",
    )[0]
    attempt_times = (
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=6),
        NOW + timedelta(minutes=26),
    )
    for index, started_at in enumerate(attempt_times):
        attempt_id = evidence_work_scenario.work_store.begin_interpretation(
            work,
            run_id="4fa85f64-5717-4562-b3fc-2c963f66afa6",
            interpreter_version="interpreter-retry",
            started_at=started_at,
        )
        evidence_work_scenario.work_store.fail_interpretation(
            attempt_id,
            failure_kind="ModelFailure",
            completed_at=started_at,
        )
        if index < 2:
            not_due = evidence_work_scenario.work_store.list_pending_documents(
                as_of=started_at + timedelta(minutes=(4 if index == 0 else 19)),
                source_id="source",
                limit=10,
                interpreter_version="interpreter-retry",
            )
            assert "version-retry" not in {item.content_version for item in not_due}
        if index == 0:
            new_item = evidence_work_scenario.persist_acquisition("version-never-attempted")
            evidence_work_scenario.persist_processing_attempt(new_item, EvidenceProcessingStatus.SUCCEEDED)
            with evidence_work_scenario.database.transaction(TransactionMode.WRITE) as connection:
                _ = connection.execute(
                    """UPDATE evidence_asset_acquisitions SET retrieved_at = ?
                       WHERE source_item_id = ? AND content_version = ?""",
                    (
                        (started_at + timedelta(minutes=6)).isoformat(),
                        new_item.source_item_id,
                        new_item.content_version,
                    ),
                )
            first_due = evidence_work_scenario.work_store.list_pending_documents(
                as_of=started_at + timedelta(minutes=6),
                source_id="source",
                limit=1,
                interpreter_version="interpreter-retry",
            )
            assert tuple(item.content_version for item in first_due) == ("version-retry",)

    next_work = evidence_work_scenario.work_store.list_pending_documents(
        as_of=NOW + timedelta(days=30),
        source_id="source",
        limit=1,
        interpreter_version="interpreter-retry",
    )
    assert tuple(item.content_version for item in next_work) == ("version-never-attempted",)
    with evidence_work_scenario.database.transaction() as connection:
        rows = cast(
            "list[tuple[str]]",
            connection.execute(
                """
                    SELECT outcome FROM claim_interpretation_attempts
                    WHERE interpreter_version = 'interpreter-retry'
                      AND content_version = 'version-retry'
                ORDER BY started_at
                """
            ).fetchall(),
        )
        outcomes = tuple(row[0] for row in rows)
    assert outcomes == ("failed", "failed", "quarantined")
