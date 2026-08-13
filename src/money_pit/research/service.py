"""Module orchestrating bounded research into provenance-safe evidence."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.constants import APP_VERSION
from money_pit.evidence.admission import materialize_processing_bundle
from money_pit.evidence.errors import EvidenceProcessingError
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.pipeline.identity import interpretation_policy_version
from money_pit.research.errors import HistoricalResearchUnavailableError
from money_pit.research.errors import ResearchBudgetExceededError
from money_pit.research.errors import ResearchError
from money_pit.research.errors import ResearchEvidenceCutoffError
from money_pit.research.errors import ResearchUriReuseMismatchError
from money_pit.schemas.evidence import EvidenceDocument
from money_pit.schemas.evidence import EvidenceProcessingAttempt
from money_pit.schemas.evidence import EvidenceProcessingStatus
from money_pit.schemas.research import ResearchDiscoveryBatch
from money_pit.schemas.research import ResearchFetch
from money_pit.schemas.research import ResearchFetchStatus
from money_pit.schemas.research import ResearchSession
from money_pit.schemas.research import ResearchTask
from money_pit.schemas.research import ResearchTaskStatus
from money_pit.schemas.sources import SourceDefinition  # noqa: TC001 - Pydantic resolves this field at runtime.
from money_pit.sources._shared import evidence_asset
from money_pit.sources._shared import source_definition_hash
from money_pit.sources._shared import utc_now
from money_pit.sources.errors import SourceError
from money_pit.sources.service import SourceIngestionRecord


if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from money_pit.evidence.processors import EvidenceProcessor
    from money_pit.evidence.processors import EvidenceProcessorRegistry
    from money_pit.evidence.repository import EvidenceProcessingAttemptRepository
    from money_pit.evidence.work import EvidenceWorkStore
    from money_pit.research.protocol import ResearchProvider
    from money_pit.research.registry import ResearchProviderRegistry
    from money_pit.research.repository import ResearchBudgetState
    from money_pit.research.repository import ResearchRepository
    from money_pit.schemas.research import ResearchDiscoveryResult
    from money_pit.schemas.research import ResearchScope
    from money_pit.schemas.sources import RawArtifact
    from money_pit.sources.service import EvidenceRepository
    from money_pit.sources.service import SourceStateRepository
    from money_pit.storage.assets import AssetStore
from money_pit.storage.intelligence_work import IntelligenceWorkRepository
from money_pit.storage.intelligence_work import ResearchUriAdmissionRecord
from money_pit.storage.intelligence_work import UriDisposition


class ResearchSourceAssociation(BaseModel):
    """Exact durable source item to immutable publisher-policy association."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_item_id: str = Field(min_length=1)
    source_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_definition: SourceDefinition

    @model_validator(mode="after")
    def validate_definition_hash(self) -> ResearchSourceAssociation:
        """Reject associations that do not bind the exact source-definition revision."""
        if source_definition_hash(self.source_definition) != self.source_definition_hash:
            raise ValueError("Research source association definition hash does not match")
        return self


class ResearchRoundResult(BaseModel):
    """Durable outcome of executing one bounded research round."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    round_number: int = Field(ge=1)
    task_count: int = Field(ge=0)
    query_count: int = Field(ge=0)
    fetch_count: int = Field(ge=0)
    source_item_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    independent_provenance_groups: tuple[str, ...]
    failure_kinds: tuple[str, ...]
    document_ids: tuple[str, ...] = ()
    fragment_ids: tuple[str, ...] = ()
    interpretation_work: tuple[EvidenceInterpretationWork, ...] = ()
    interpretation_targets: tuple[ResearchInterpretationTarget, ...] = ()
    source_associations: tuple[ResearchSourceAssociation, ...] = ()


class ResearchInterpretationTarget(BaseModel):
    """One exact fetched document and the durable material anchors its task targets."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    work: EvidenceInterpretationWork
    source: ResearchSourceAssociation
    material_claim_keys: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_material_claim_keys(self) -> ResearchInterpretationTarget:
        """Reject duplicate anchor identities at the task-to-evidence boundary."""
        if len(self.material_claim_keys) != len(set(self.material_claim_keys)):
            raise ValueError("Research interpretation target claim keys must be unique")
        return self


class ResearchService:
    """Execute allowed research while enforcing durable budgets and evidence admission."""

    def __init__(
        self,
        providers: ResearchProviderRegistry,
        repository: ResearchRepository,
        source_repository: SourceStateRepository,
        evidence_repository: EvidenceRepository,
        processor_registry: EvidenceProcessorRegistry,
        attempt_repository: EvidenceProcessingAttemptRepository,
        asset_store: AssetStore,
        work_repository: IntelligenceWorkRepository | None = None,
        *,
        evidence_work: EvidenceWorkStore | None = None,
        interpretation_model: str = APP_VERSION,
        interpretation_prompt_version: str = APP_VERSION,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind read-only providers to durable source and research boundaries."""
        self._providers: ResearchProviderRegistry = providers
        self._repository: ResearchRepository = repository
        self._source_repository: SourceStateRepository = source_repository
        self._evidence_repository: EvidenceRepository = evidence_repository
        self._processor_registry: EvidenceProcessorRegistry = processor_registry
        self._attempt_repository: EvidenceProcessingAttemptRepository = attempt_repository
        self._asset_store: AssetStore = asset_store
        self._work_repository: IntelligenceWorkRepository | None = work_repository
        self._evidence_work: EvidenceWorkStore | None = evidence_work
        self._interpretation_model: str = interpretation_model
        self._interpretation_prompt_version: str = interpretation_prompt_version
        self._clock: Callable[[], datetime] = clock

    def start(self, session: ResearchSession) -> bool:
        """Persist a new bounded session before any provider I/O."""
        return self._repository.create_session(session)

    def enqueue(self, session: ResearchSession, tasks: tuple[ResearchTask, ...]) -> int:
        """Persist A2 research work so A3 or replay can resume it later."""
        _validate_round_inputs(session, tasks)
        _ = self.start(session)
        return sum(self._repository.add_task(task) for task in tasks)

    def run_pending_for_scope(
        self,
        scope: ResearchScope,
        *,
        selected_result_ids: frozenset[str] | None = None,
    ) -> ResearchRoundResult:
        """Resume the next durable pending round for one exact subject."""
        session: ResearchSession | None = self._repository.active_session_for_scope(scope)
        if session is None:
            raise KeyError(scope)
        pending: tuple[ResearchTask, ...] = self._repository.pending_tasks(session.session_id)
        if not pending:
            raise KeyError(f"No pending research tasks for {scope.kind}")
        next_round: int = pending[0].round_number
        tasks: tuple[ResearchTask, ...] = tuple(task for task in pending if task.round_number == next_round)
        return self.run_round(session, tasks, selected_result_ids=selected_result_ids)

    def run_round(  # noqa: C901 - explicit durable provider phase recovery
        self,
        session: ResearchSession,
        tasks: tuple[ResearchTask, ...],
        *,
        selected_result_ids: frozenset[str] | None = None,
        requested_as_of: datetime | None = None,
        decision_at: datetime | None = None,
        historical_explicit: bool = False,
        job_id: str | None = None,
    ) -> ResearchRoundResult:
        """Search tasks and fetch selected results into durable evidence."""
        _validate_round_inputs(session, tasks)
        cutoff = requested_as_of or session.started_at
        actual_decision_at = decision_at or self._clock()
        if cutoff > actual_decision_at:
            raise ValueError("requested_as_of cannot be later than decision_at")
        budget: ResearchBudgetState = self._repository.budget_state(session.session_id)
        _require_round_capacity(budget, tasks, now=actual_decision_at)
        source_item_ids: list[str] = []
        asset_ids: list[str] = []
        provenance_groups: set[str] = set()
        failure_kinds: list[str] = []
        interpretation_work: list[EvidenceInterpretationWork] = []
        interpretation_targets: list[ResearchInterpretationTarget] = []
        source_associations: dict[str, ResearchSourceAssociation] = {}
        observed_result_ids: set[str] = set()
        for task in tasks:
            inserted: bool = self._repository.add_task(task)
            durable_status = self._repository.task_status(task.task_id)
            if not inserted and durable_status is ResearchTaskStatus.FAILED:
                continue
            provider = self._providers.get(task.query.provider)
            if historical_explicit and not (
                provider.capabilities.point_in_time_certified and provider.capabilities.availability_is_verifiable
            ):
                failure_kinds.append(HistoricalResearchUnavailableError.__name__)
                self._repository.record_search_failure(task)
                continue
            if durable_status in {
                ResearchTaskStatus.SEARCHED,
                ResearchTaskStatus.FETCHING,
                ResearchTaskStatus.COMPLETED,
            }:
                recovered_results = self._repository.results_for_task(task.task_id)
                batch = ResearchDiscoveryBatch(
                    query=task.query,
                    results=recovered_results,
                    searched_at=max(
                        (result.discovered_at for result in recovered_results),
                        default=actual_decision_at,
                    ),
                )
            else:
                self._repository.reserve_query(task, attempted_at=self._clock())
                try:
                    batch = (
                        provider.search_as_of(task.query, requested_as_of=cutoff)
                        if historical_explicit
                        else provider.search(task.query)
                    )
                    batch = batch.model_copy(
                        update={
                            "results": tuple(
                                result.model_copy(
                                    update={
                                        "result_id": hashlib.sha256(
                                            f"{task.task_id}\0{result.result_id}".encode("utf-8"),
                                        ).hexdigest(),
                                    },
                                )
                                for result in batch.results
                            ),
                        },
                    )
                    _ = self._repository.record_search(task, batch)
                except (ResearchError, SourceError) as error:
                    failure_kinds.append(type(error).__name__)
                    self._repository.record_search_failure(task)
                    continue

            fetched = self._fetch_batch_results(
                session.session_id,
                task,
                provider,
                batch,
                selected_result_ids=selected_result_ids,
                cutoff=cutoff,
                enforce_cutoff=historical_explicit,
                job_id=job_id,
            )
            observed_result_ids.update(fetched[0])
            source_item_ids.extend(fetched[1])
            asset_ids.extend(fetched[2])
            failure_kinds.extend(fetched[3])
            interpretation_work.extend(fetched[4])
            interpretation_targets.extend(fetched[6])
            for association in fetched[5]:
                definition = association.source_definition
                provenance_groups.add(definition.provenance_group)
                existing_association = source_associations.get(association.source_item_id)
                if existing_association is not None and existing_association != association:
                    raise ResearchError("Research source item identity has conflicting publisher policies")
                source_associations[association.source_item_id] = association
            task_failed = bool(fetched[3])
            self._repository.complete_task(task.task_id, failed=task_failed)

        if selected_result_ids is not None:
            unknown: frozenset[str] = selected_result_ids.difference(observed_result_ids)
            if unknown:
                raise ValueError("Selected research result IDs were not returned by this round")
        final_budget: ResearchBudgetState = self._repository.budget_state(session.session_id)
        round_number: int = tasks[0].round_number if tasks else 1
        return ResearchRoundResult(
            session_id=session.session_id,
            round_number=round_number,
            task_count=len(tasks),
            query_count=final_budget.query_count - budget.query_count,
            fetch_count=final_budget.fetch_count - budget.fetch_count,
            source_item_ids=tuple(source_item_ids),
            asset_ids=tuple(asset_ids),
            independent_provenance_groups=tuple(sorted(provenance_groups)),
            failure_kinds=tuple(failure_kinds),
            document_ids=tuple(work.document.asset.asset_id for work in interpretation_work),
            fragment_ids=tuple(
                fragment.fragment_id for work in interpretation_work for fragment in work.document.fragments
            ),
            interpretation_work=tuple(interpretation_work),
            interpretation_targets=tuple(interpretation_targets),
            source_associations=tuple(source_associations[key] for key in sorted(source_associations)),
        )

    def _fetch_batch_results(
        self,
        session_id: str,
        task: ResearchTask,
        provider: ResearchProvider,
        batch: ResearchDiscoveryBatch,
        *,
        selected_result_ids: frozenset[str] | None,
        cutoff: datetime,
        enforce_cutoff: bool,
        job_id: str | None,
    ) -> tuple[
        set[str],
        list[str],
        list[str],
        list[str],
        list[EvidenceInterpretationWork],
        list[ResearchSourceAssociation],
        list[ResearchInterpretationTarget],
    ]:
        observed: set[str] = set()
        source_ids: list[str] = []
        asset_ids: list[str] = []
        failures: list[str] = []
        work_items: list[EvidenceInterpretationWork] = []
        source_associations: list[ResearchSourceAssociation] = []
        interpretation_targets: list[ResearchInterpretationTarget] = []
        for result in batch.results:
            observed.add(result.result_id)
            if selected_result_ids is not None and result.result_id not in selected_result_ids:
                continue
            if enforce_cutoff and not _available_by(result, cutoff):
                failures.append(ResearchEvidenceCutoffError.__name__)
                continue
            source_item_id, asset_id, failure_kind, fetched_work, source_definition = self._fetch_result(
                session_id,
                task,
                provider,
                result,
                requested_as_of=cutoff,
                historical_explicit=enforce_cutoff,
                job_id=job_id,
            )
            if failure_kind is not None:
                failures.append(failure_kind)
                continue
            if source_item_id is None or asset_id is None:
                raise ResearchError("Successful research fetch omitted durable identities")
            if source_definition is None:
                raise ResearchError("Successful research fetch omitted its source policy")
            association = ResearchSourceAssociation(
                source_item_id=source_item_id,
                source_definition_hash=source_definition_hash(source_definition),
                source_definition=source_definition,
            )
            source_ids.append(source_item_id)
            if fetched_work:
                asset_ids.extend(work.document.asset.asset_id for work in fetched_work)
            else:
                asset_ids.append(asset_id)
            work_items.extend(fetched_work)
            if task.query.material_claim_keys:
                interpretation_targets.extend(
                    ResearchInterpretationTarget(
                        work=work,
                        source=association,
                        material_claim_keys=task.query.material_claim_keys,
                    )
                    for work in fetched_work
                )
            source_associations.append(association)
        return (
            observed,
            source_ids,
            asset_ids,
            failures,
            work_items,
            source_associations,
            interpretation_targets,
        )

    def _fetch_result(  # noqa: C901 - ordered URI policy, reuse, and fetch gates
        self,
        session_id: str,
        task: ResearchTask,
        provider: ResearchProvider,
        result: ResearchDiscoveryResult,
        *,
        requested_as_of: datetime,
        historical_explicit: bool,
        job_id: str | None,
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        tuple[EvidenceInterpretationWork, ...],
        SourceDefinition | None,
    ]:
        """Fetch and persist one result, returning durable IDs or a failure kind."""
        durable_fetch = self._repository.fetch_for_result(task.task_id, result.result_id)
        if durable_fetch is not None:
            if durable_fetch.status is ResearchFetchStatus.FAILED:
                return None, None, durable_fetch.failure_kind, (), None
            if durable_fetch.status is ResearchFetchStatus.SKIPPED:
                return None, None, "PreviouslySkippedUri", (), None
            source_definition = provider.source_definition_for(result)
            recovered_work: tuple[EvidenceInterpretationWork, ...] = ()
            if (
                self._evidence_work is not None
                and durable_fetch.source_item_id is not None
                and durable_fetch.asset_id is not None
            ):
                recovered_work = (
                    self._evidence_work.document_for_asset(
                        source_item_id=durable_fetch.source_item_id,
                        asset_id=durable_fetch.asset_id,
                    ),
                )
            return (
                durable_fetch.source_item_id,
                durable_fetch.asset_id,
                None,
                recovered_work,
                source_definition,
            )
        prior = (
            None
            if job_id is None or self._work_repository is None
            else self._work_repository.uri_admission_for_job(job_id, result.canonical_uri)
        )
        if prior is not None and prior.disposition is UriDisposition.REJECTED:
            return None, None, prior.reason or "PreviouslyRejectedUri", (), None
        try:
            source_definition: SourceDefinition = provider.source_definition_for(result)
        except (ResearchError, SourceError) as error:
            self._record_uri_admission(
                job_id,
                result.canonical_uri,
                disposition=UriDisposition.REJECTED,
                reason=type(error).__name__,
            )
            return None, None, type(error).__name__, (), None
        if prior is not None:
            try:
                work_items = self._job_owned_reusable_work(source_definition, prior)
            except (ResearchError, EvidenceProcessingError):
                pass
            else:
                if self._interpretations_are_reusable(work_items):
                    document = work_items[0].document
                    return (
                        document.asset.source_item_id,
                        document.asset.asset_id,
                        None,
                        work_items,
                        source_definition,
                    )
        self._repository.reserve_fetch(session_id, attempted_at=self._clock())
        try:
            _ = self._source_repository.register_definition(
                source_definition,
                registry_version=APP_VERSION,
                registered_at=self._clock(),
            )
            artifact: RawArtifact = (
                provider.fetch_as_of(result, requested_as_of=requested_as_of)
                if historical_explicit
                else provider.fetch(result)
            )
            if artifact.source_item.source_definition_hash != source_definition_hash(
                source_definition,
            ):
                raise ResearchError("Research provider artifact does not match its publisher policy")
            processor = self._processor_registry.select(artifact.media_type)
            reusable = (
                None
                if self._work_repository is None or self._evidence_work is None
                else self._work_repository.reusable_uri_admission(
                    result.canonical_uri,
                    content_hash=artifact.content_hash,
                    content_version=artifact.source_item.content_version,
                    source_definition_hash=artifact.source_item.source_definition_hash,
                    processor_name=processor.name,
                    processor_version=processor.version,
                    interpretation_model=self._interpretation_model,
                    interpretation_prompt_version=self._interpretation_prompt_version,
                )
            )
            reusable_work = self._reusable_work_or_empty(
                reusable,
                content_version=artifact.source_item.content_version,
            )
            exact_reuse = bool(reusable_work) and self._interpretations_are_reusable(reusable_work)
            work_items = reusable_work if exact_reuse else self._persist_evidence(artifact, processor=processor)
            document = next(iter(work_items)).document
        except (ResearchError, EvidenceProcessingError, SourceError, OSError) as error:
            failure_kind: str = type(error).__name__
            _ = self._repository.record_fetch(
                ResearchFetch(
                    fetch_id=f"{task.task_id}:{result.result_id}",
                    task_id=task.task_id,
                    result_id=result.result_id,
                    status=ResearchFetchStatus.FAILED,
                    failure_kind=failure_kind,
                    attempted_at=self._clock(),
                ),
            )
            return None, None, failure_kind, (), None
        _ = self._repository.record_fetch(
            ResearchFetch(
                fetch_id=f"{task.task_id}:{result.result_id}",
                task_id=task.task_id,
                result_id=result.result_id,
                source_item_id=document.asset.source_item_id,
                asset_id=document.asset.asset_id,
                status=ResearchFetchStatus.SUCCEEDED,
                attempted_at=self._clock(),
            ),
        )
        if prior is None or not _admission_matches_identity(
            prior,
            content_hash=artifact.content_hash,
            content_version=artifact.source_item.content_version,
            source_definition_hash=artifact.source_item.source_definition_hash,
            processor_name=processor.name,
            processor_version=processor.version,
            interpretation_model=self._interpretation_model,
            interpretation_prompt_version=self._interpretation_prompt_version,
        ):
            self._record_uri_admission(
                job_id,
                result.canonical_uri,
                disposition=(UriDisposition.REUSED if exact_reuse else UriDisposition.ACCEPTED),
                provenance_group=source_definition.provenance_group,
                source_item_id=document.asset.source_item_id,
                asset_id=document.asset.asset_id,
                content_hash=artifact.content_hash,
                content_version=artifact.source_item.content_version,
                source_definition_hash=artifact.source_item.source_definition_hash,
                processor_name=processor.name,
                processor_version=processor.version,
            )
        return (
            document.asset.source_item_id,
            document.asset.asset_id,
            None,
            work_items,
            source_definition,
        )

    def _record_uri_admission(
        self,
        job_id: str | None,
        canonical_uri: str,
        *,
        disposition: UriDisposition,
        reason: str | None = None,
        provenance_group: str | None = None,
        source_item_id: str | None = None,
        asset_id: str | None = None,
        content_hash: str | None = None,
        content_version: str | None = None,
        source_definition_hash: str | None = None,
        processor_name: str | None = None,
        processor_version: str | None = None,
    ) -> None:
        """Persist one job-owned URI decision when incremental work is active."""
        if job_id is None or job_id.startswith("legacy:") or self._work_repository is None:
            return
        admission_identity = "\0".join(
            (
                job_id,
                canonical_uri,
                content_hash or "",
                content_version or "",
                source_definition_hash or "",
                processor_name or "",
                processor_version or "",
                self._interpretation_model if disposition is not UriDisposition.REJECTED else "",
                (self._interpretation_prompt_version if disposition is not UriDisposition.REJECTED else ""),
            )
        )
        admission_id = hashlib.sha256(admission_identity.encode()).hexdigest()
        self._work_repository.append_uri_admission(
            ResearchUriAdmissionRecord(
                admission_id=admission_id,
                job_id=job_id,
                canonical_uri=canonical_uri,
                disposition=disposition,
                provenance_group=provenance_group,
                consumes_fetch_capacity=disposition is UriDisposition.ACCEPTED,
                admitted_at=self._clock(),
                reason=reason,
                source_item_id=source_item_id,
                asset_id=asset_id,
                content_hash=content_hash,
                content_version=content_version,
                source_definition_hash=source_definition_hash,
                processor_name=processor_name,
                processor_version=processor_version,
                interpretation_model=(
                    self._interpretation_model if disposition is not UriDisposition.REJECTED else None
                ),
                interpretation_prompt_version=(
                    self._interpretation_prompt_version if disposition is not UriDisposition.REJECTED else None
                ),
                payload={},
            )
        )

    def _persist_evidence(
        self,
        artifact: RawArtifact,
        *,
        processor: EvidenceProcessor | None = None,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Persist fetched bytes and processed evidence before returning citation IDs."""
        stored = self._asset_store.put_bytes(artifact.content)
        if stored.digest != artifact.content_hash:
            raise ResearchError("Research artifact hash changed during persistence")
        unprocessed = EvidenceDocument(
            asset=evidence_asset(artifact).model_copy(update={"local_path": stored.path}),
            fragments=(),
        )
        _ = self._evidence_repository.persist_ingestion_batch(
            artifact.source_item.source_id,
            (SourceIngestionRecord(source_item=artifact.source_item, evidence_document=unprocessed),),
            next_cursor=None,
            updated_at=self._clock(),
        )
        processor = processor or self._processor_registry.select(artifact.media_type)
        started_at = self._clock()
        attempt_id: str = str(uuid4())
        try:
            extracted, derived_records = self._process_bundle(artifact, processor)
        except (EvidenceProcessingError, ResearchError, SourceError) as error:
            _ = self._attempt_repository.persist(
                EvidenceProcessingAttempt(
                    attempt_id=attempt_id,
                    source_item_id=artifact.source_item.source_item_id,
                    content_version=artifact.source_item.content_version,
                    asset_id=artifact.content_hash,
                    processor_name=processor.name,
                    processor_version=processor.version,
                    started_at=started_at,
                    completed_at=self._clock(),
                    status=EvidenceProcessingStatus.FAILED,
                    failure_kind=type(error).__name__,
                ),
            )
            raise
        durable: EvidenceDocument = extracted.model_copy(
            update={"asset": extracted.asset.model_copy(update={"local_path": stored.path})},
        )
        _ = self._evidence_repository.persist_ingestion_batch(
            artifact.source_item.source_id,
            (
                SourceIngestionRecord(source_item=artifact.source_item, evidence_document=durable),
                *derived_records,
            ),
            next_cursor=None,
            updated_at=self._clock(),
        )
        _ = self._attempt_repository.persist(
            EvidenceProcessingAttempt(
                attempt_id=attempt_id,
                source_item_id=artifact.source_item.source_item_id,
                content_version=artifact.source_item.content_version,
                asset_id=artifact.content_hash,
                processor_name=processor.name,
                processor_version=processor.version,
                started_at=started_at,
                completed_at=self._clock(),
                status=EvidenceProcessingStatus.SUCCEEDED,
                document_id=artifact.content_hash,
                fragment_ids=tuple(fragment.fragment_id for fragment in durable.fragments),
            ),
        )
        for record in derived_records:
            _ = self._attempt_repository.persist(
                EvidenceProcessingAttempt(
                    attempt_id=str(uuid4()),
                    source_item_id=artifact.source_item.source_item_id,
                    content_version=artifact.source_item.content_version,
                    asset_id=record.evidence_document.asset.asset_id,
                    processor_name=processor.name,
                    processor_version=processor.version,
                    started_at=started_at,
                    completed_at=self._clock(),
                    status=EvidenceProcessingStatus.SUCCEEDED,
                    document_id=record.evidence_document.asset.asset_id,
                    fragment_ids=tuple(fragment.fragment_id for fragment in record.evidence_document.fragments),
                ),
            )
        return tuple(
            EvidenceInterpretationWork(
                document=record.evidence_document,
                content_version=artifact.source_item.content_version,
            )
            for record in (
                SourceIngestionRecord(source_item=artifact.source_item, evidence_document=durable),
                *derived_records,
            )
        )

    def _job_owned_reusable_work(
        self,
        source_definition: SourceDefinition,
        admission: ResearchUriAdmissionRecord,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Reconstruct an immutable job URI decision without provider I/O."""
        expected_definition_hash = source_definition_hash(source_definition)
        if admission.source_definition_hash != expected_definition_hash:
            raise ResearchUriReuseMismatchError(
                "Job URI admission source policy differs from the current provider policy",
            )
        if (
            admission.interpretation_model != self._interpretation_model
            or admission.interpretation_prompt_version != self._interpretation_prompt_version
        ):
            raise ResearchUriReuseMismatchError(
                "Job URI admission interpretation policy differs from the current policy",
            )
        work_items = self._reusable_work(
            source_item_id=admission.source_item_id,
            content_version=admission.content_version,
            asset_id=admission.asset_id,
            processor_name=admission.processor_name,
            processor_version=admission.processor_version,
        )
        admitted_document = next(
            work.document for work in work_items if work.document.asset.asset_id == admission.asset_id
        )
        processor = self._processor_registry.select(admitted_document.asset.media_type)
        if processor.name != admission.processor_name or processor.version != admission.processor_version:
            raise ResearchUriReuseMismatchError(
                "Job URI admission processor differs from the current processor",
            )
        return work_items

    def _interpretations_are_reusable(
        self,
        work_items: tuple[EvidenceInterpretationWork, ...],
    ) -> bool:
        """Return whether every document has an exact completed interpretation."""
        evidence_work = self._evidence_work
        if evidence_work is None:
            return False
        interpreter_version = interpretation_policy_version(
            prompt_version=self._interpretation_prompt_version,
            model=self._interpretation_model,
        )
        return all(
            evidence_work.reusable_interpretation(
                work,
                interpreter_version=interpreter_version,
            )
            is not None
            for work in work_items
        )

    def _reusable_work_or_empty(
        self,
        admission: ResearchUriAdmissionRecord | None,
        *,
        content_version: str,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Reconstruct a reuse candidate or reject incomplete durable state."""
        if admission is None:
            return ()
        try:
            return self._reusable_work(
                source_item_id=admission.source_item_id,
                content_version=content_version,
                asset_id=admission.asset_id,
                processor_name=admission.processor_name,
                processor_version=admission.processor_version,
            )
        except (ResearchError, EvidenceProcessingError):
            return ()

    def _reusable_work(
        self,
        *,
        source_item_id: str | None,
        content_version: str | None,
        asset_id: str | None,
        processor_name: str | None,
        processor_version: str | None,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Reconstruct exact processed work or reject an incomplete reuse candidate."""
        evidence_work = self._evidence_work
        if (
            evidence_work is None
            or source_item_id is None
            or content_version is None
            or asset_id is None
            or processor_name is None
            or processor_version is None
        ):
            raise ResearchError("Reusable URI admission omitted durable evidence identity")
        work_items = evidence_work.documents_for_bundle(
            source_item_id=source_item_id,
            content_version=content_version,
            processor_name=processor_name,
            processor_version=processor_version,
        )
        asset_ids: set[str] = {work.document.asset.asset_id for work in work_items}
        if not work_items or asset_id not in asset_ids:
            raise ResearchError("Reusable URI admission has no complete processed evidence bundle")
        return work_items

    def _process_bundle(
        self,
        artifact: RawArtifact,
        processor: EvidenceProcessor,
    ) -> tuple[EvidenceDocument, list[SourceIngestionRecord]]:
        documents = materialize_processing_bundle(
            artifact,
            processor.process_bundle(artifact),
            self._asset_store,
        )
        return documents[0], [
            SourceIngestionRecord(source_item=artifact.source_item, evidence_document=document)
            for document in documents[1:]
        ]


def _admission_matches_identity(
    admission: ResearchUriAdmissionRecord,
    *,
    content_hash: str,
    content_version: str,
    source_definition_hash: str,
    processor_name: str,
    processor_version: str,
    interpretation_model: str,
    interpretation_prompt_version: str,
) -> bool:
    """Return whether an admission binds one exact reusable policy identity."""
    return (
        admission.content_hash == content_hash
        and admission.content_version == content_version
        and admission.source_definition_hash == source_definition_hash
        and admission.processor_name == processor_name
        and admission.processor_version == processor_version
        and admission.interpretation_model == interpretation_model
        and admission.interpretation_prompt_version == interpretation_prompt_version
    )


def _validate_round_inputs(session: ResearchSession, tasks: tuple[ResearchTask, ...]) -> None:
    session_ids: set[str] = {task.session_id for task in tasks}
    if session_ids.difference({session.session_id}):
        raise ValueError("Every research task must belong to the supplied session")
    round_numbers: set[int] = {task.round_number for task in tasks}
    if len(round_numbers) > 1:
        raise ValueError("Research tasks in one call must share a round number")


def _available_by(result: ResearchDiscoveryResult, cutoff: datetime) -> bool:
    """Require verifiable availability and reject later economic timestamps."""
    if result.available_at is None or result.available_at > cutoff:
        return False
    return all(value is None or value <= cutoff for value in (result.published_at, result.updated_at))


def _require_round_capacity(
    budget: ResearchBudgetState,
    tasks: tuple[ResearchTask, ...],
    *,
    now: datetime,
) -> None:
    if budget.session.status.value != "active":
        raise ResearchBudgetExceededError("Research session is not active")
    if now >= budget.session.deadline_at:
        raise ResearchBudgetExceededError("Research elapsed-time budget expired")
    if tasks and tasks[0].round_number > budget.session.maximum_rounds:
        raise ResearchBudgetExceededError("Research round budget exceeded")
    if budget.query_count + len(tasks) > budget.session.maximum_queries:
        raise ResearchBudgetExceededError("Research query budget exceeded")
