CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application_id TEXT NOT NULL,
    release TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL,
    initialized_at TEXT NOT NULL
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    requested_as_of TEXT NOT NULL,
    started_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    through_stage TEXT NOT NULL CHECK (through_stage IN ('A1', 'A2', 'A3', 'A4', 'A5', 'A6')),
    source_config_hash TEXT NOT NULL,
    intelligence_config_hash TEXT,
    portfolio_config_hash TEXT,
    execution_config_hash TEXT,
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    CHECK (
        (through_stage = 'A1' AND intelligence_config_hash IS NULL)
        OR
        (through_stage IN ('A2', 'A3', 'A4', 'A5', 'A6') AND intelligence_config_hash IS NOT NULL)
    ),
    CHECK (
        (through_stage IN ('A1', 'A2', 'A3', 'A4') AND portfolio_config_hash IS NULL)
        OR
        (through_stage IN ('A5', 'A6') AND portfolio_config_hash IS NOT NULL)
    ),
    CHECK (
        (through_stage IN ('A1', 'A2', 'A3', 'A4', 'A5') AND execution_config_hash IS NULL)
        OR
        (through_stage = 'A6' AND execution_config_hash IS NOT NULL)
    )
);

CREATE TABLE run_terminal_events (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    completed_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    failure_kind TEXT,
    failure_detail_json TEXT CHECK (
        failure_detail_json IS NULL OR json_valid(failure_detail_json)
    ),
    event_json TEXT NOT NULL CHECK (json_valid(event_json)),
    CHECK (known_at >= completed_at),
    CHECK (
        (status = 'completed' AND failure_kind IS NULL AND failure_detail_json IS NULL)
        OR
        (status = 'failed' AND length(failure_kind) BETWEEN 1 AND 128
            AND failure_detail_json IS NOT NULL)
    )
);
CREATE INDEX run_terminal_events_known_idx
ON run_terminal_events (known_at, run_id);

CREATE TABLE stage_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK (stage IN ('A1', 'A2', 'A3', 'A4', 'A5', 'A6')),
    requested_as_of TEXT NOT NULL,
    started_at TEXT NOT NULL,
    decision_at TEXT,
    known_at TEXT NOT NULL,
    input_ids_json TEXT NOT NULL CHECK (json_valid(input_ids_json)),
    output_ids_json TEXT NOT NULL CHECK (json_valid(output_ids_json)),
    implementation_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    CHECK (known_at >= started_at),
    CHECK (decision_at IS NULL OR (decision_at >= started_at AND decision_at <= known_at)),
    UNIQUE (run_id, stage)
);
CREATE INDEX stage_artifacts_run_delta_idx
ON stage_artifacts (run_id, stage, known_at, artifact_id);

CREATE TABLE source_definition_revisions (
    definition_hash TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    registry_version TEXT NOT NULL,
    provenance_group TEXT NOT NULL,
    definition_json TEXT NOT NULL CHECK (json_valid(definition_json)),
    registered_at TEXT NOT NULL
);
CREATE INDEX source_definition_revisions_source_idx
ON source_definition_revisions (source_id, registered_at, definition_hash);
CREATE VIEW current_source_definitions AS
SELECT revision.*
FROM source_definition_revisions AS revision
WHERE NOT EXISTS (
    SELECT 1
    FROM source_definition_revisions AS later
    WHERE later.source_id = revision.source_id
      AND (later.registered_at, later.definition_hash) >
          (revision.registered_at, revision.definition_hash)
);

CREATE TABLE source_items (
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_definition_hash TEXT NOT NULL REFERENCES source_definition_revisions(definition_hash) ON DELETE RESTRICT,
    canonical_uri TEXT NOT NULL,
    published_at TEXT,
    updated_at TEXT,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (source_item_id, content_version)
);
CREATE INDEX source_items_source_idx ON source_items (source_id, discovered_at);

CREATE TABLE source_cursors (
    source_id TEXT NOT NULL,
    cursor_purpose TEXT NOT NULL CHECK (cursor_purpose IN ('sync', 'backfill')),
    cursor_json TEXT NOT NULL CHECK (json_valid(cursor_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, cursor_purpose)
);

CREATE TABLE evidence_assets (
    asset_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    local_path TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    CHECK (asset_id = content_hash)
);

CREATE TABLE evidence_asset_acquisitions (
    acquisition_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    source_definition_hash TEXT NOT NULL REFERENCES source_definition_revisions(definition_hash) ON DELETE RESTRICT,
    retrieved_at TEXT NOT NULL,
    media_type TEXT NOT NULL,
    FOREIGN KEY (source_item_id, content_version) REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT,
    UNIQUE (asset_id, source_item_id, content_version, retrieved_at)
);
CREATE INDEX evidence_asset_acquisitions_source_idx
ON evidence_asset_acquisitions (source_item_id, content_version, retrieved_at);

CREATE TABLE evidence_fragments (
    fragment_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    fragment_kind TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
    extracted_text TEXT,
    cited_source_text TEXT,
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX evidence_fragments_asset_idx ON evidence_fragments (asset_id);
CREATE VIRTUAL TABLE evidence_fragment_search USING fts5(fragment_id UNINDEXED, content);

CREATE TABLE evidence_processing_attempts (
    attempt_id TEXT PRIMARY KEY,
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    processor_name TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    failure_kind TEXT,
    document_id TEXT,
    fragment_ids_json TEXT NOT NULL CHECK (json_valid(fragment_ids_json)),
    FOREIGN KEY (source_item_id, content_version)
        REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT
);
CREATE INDEX evidence_processing_attempts_acquisition_idx
ON evidence_processing_attempts (
    source_item_id, content_version, asset_id, status, completed_at
);

CREATE TABLE claim_interpretation_attempts (
    attempt_id TEXT PRIMARY KEY,
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    interpreter_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    known_at TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'succeeded', 'failed', 'quarantined')),
    failure_kind TEXT,
    retry_after TEXT,
    observation_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(observation_ids_json)),
    FOREIGN KEY (source_item_id, content_version)
        REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT,
    CHECK (
        (outcome = 'pending' AND completed_at IS NULL AND known_at IS NULL
            AND failure_kind IS NULL AND retry_after IS NULL)
        OR
        (outcome = 'succeeded' AND completed_at IS NOT NULL AND known_at IS NOT NULL
            AND failure_kind IS NULL AND retry_after IS NULL)
        OR
        (outcome = 'failed' AND completed_at IS NOT NULL AND known_at IS NOT NULL
            AND failure_kind IS NOT NULL AND retry_after IS NOT NULL
            AND retry_after > completed_at)
        OR
        (outcome = 'quarantined' AND completed_at IS NOT NULL AND known_at IS NOT NULL
            AND failure_kind IS NOT NULL AND retry_after IS NULL)
    )
);
CREATE UNIQUE INDEX claim_interpretation_success_idx
ON claim_interpretation_attempts (
    source_item_id, content_version, asset_id, interpreter_version
)
WHERE outcome = 'succeeded';
CREATE INDEX claim_interpretation_pending_idx
ON claim_interpretation_attempts (outcome, started_at, attempt_id);
CREATE INDEX claim_interpretation_retry_idx
ON claim_interpretation_attempts (
    source_item_id, content_version, asset_id, interpreter_version,
    outcome, retry_after, completed_at, attempt_id
);

CREATE TABLE claim_observations (
    observation_id TEXT PRIMARY KEY,
    claim_text TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    asserted_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    effective_from TEXT,
    event_at TEXT,
    review_at TEXT,
    valid_until TEXT,
    horizon_class TEXT NOT NULL CHECK (horizon_class IN ('event', 'tactical', 'medium_term', 'structural')),
    observation_json TEXT NOT NULL CHECK (json_valid(observation_json))
);
CREATE INDEX claim_observations_as_of_idx ON claim_observations (known_at, asserted_at, observation_id);
CREATE VIRTUAL TABLE claim_observation_search USING fts5(
    observation_id UNINDEXED,
    claim_text,
    tokenize = 'unicode61'
);

CREATE TABLE claim_resolution_decisions (
    decision_id TEXT PRIMARY KEY,
    subject_observation_id TEXT NOT NULL REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    object_observation_id TEXT REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    relation TEXT NOT NULL CHECK (relation IN ('same', 'contradicts', 'distinct', 'updates')),
    resolved_canonical_claim_key TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    decision_json TEXT NOT NULL CHECK (json_valid(decision_json)),
    CHECK (
        (relation = 'distinct' AND object_observation_id IS NULL)
        OR
        (relation != 'distinct' AND object_observation_id IS NOT NULL
            AND object_observation_id != subject_observation_id)
    )
);

CREATE TABLE verification_results (
    verification_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('supported', 'contradicted', 'mixed', 'unresolved')),
    checked_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    valid_until TEXT,
    verification_json TEXT NOT NULL CHECK (json_valid(verification_json))
);
CREATE INDEX verification_results_as_of_idx ON verification_results (observation_id, known_at, checked_at);

CREATE TABLE canonical_claims (
    canonical_claim_key TEXT NOT NULL,
    projected_as_of TEXT NOT NULL,
    current_status TEXT NOT NULL CHECK (current_status IN ('active', 'superseded', 'expired', 'disputed')),
    projection_json TEXT NOT NULL CHECK (json_valid(projection_json)),
    PRIMARY KEY (canonical_claim_key, projected_as_of)
);

CREATE TABLE candidate_theses (
    candidate_thesis_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('open', 'researching', 'promoted', 'rejected', 'unresolved')),
    created_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    candidate_json TEXT NOT NULL CHECK (json_valid(candidate_json))
);

CREATE TABLE planned_research_tasks (
    task_id TEXT PRIMARY KEY,
    candidate_thesis_id TEXT NOT NULL REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'materialized', 'completed', 'cancelled')),
    created_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    materialized_session_id TEXT REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    materialized_task_id TEXT,
    completed_at TEXT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    task_json TEXT NOT NULL CHECK (json_valid(task_json)),
    CHECK (
        (status = 'pending' AND materialized_session_id IS NULL AND materialized_task_id IS NULL AND completed_at IS NULL)
        OR
        (status = 'materialized' AND materialized_session_id IS NOT NULL AND materialized_task_id IS NOT NULL AND completed_at IS NULL)
        OR
        (status = 'completed' AND materialized_session_id IS NOT NULL AND materialized_task_id IS NOT NULL AND completed_at IS NOT NULL)
        OR
        (status = 'cancelled' AND completed_at IS NOT NULL)
    )
);
CREATE INDEX planned_research_tasks_pending_idx
ON planned_research_tasks (candidate_thesis_id, status, known_at, created_at, task_id);

CREATE TABLE research_sessions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    scope_kind TEXT NOT NULL CHECK (
        scope_kind IN ('candidate_thesis', 'canonical_claim', 'claim_observation')
    ),
    scope_subject_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    maximum_rounds INTEGER NOT NULL CHECK (maximum_rounds > 0),
    maximum_queries INTEGER NOT NULL CHECK (maximum_queries > 0),
    maximum_fetches INTEGER NOT NULL CHECK (maximum_fetches > 0),
    query_count INTEGER NOT NULL DEFAULT 0 CHECK (query_count >= 0),
    fetch_count INTEGER NOT NULL DEFAULT 0 CHECK (fetch_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'stopped')),
    stop_reason TEXT,
    session_json TEXT NOT NULL CHECK (json_valid(session_json))
);
CREATE INDEX research_sessions_run_idx ON research_sessions (run_id, started_at, session_id);
CREATE INDEX research_sessions_scope_idx
ON research_sessions (scope_kind, scope_subject_id, status, started_at, session_id);

CREATE TABLE research_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    provider TEXT NOT NULL,
    query_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'searched', 'fetching', 'completed', 'failed')),
    created_at TEXT NOT NULL,
    task_json TEXT NOT NULL CHECK (json_valid(task_json)),
    UNIQUE (session_id, provider, query_text)
);

CREATE TABLE research_results (
    result_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    canonical_uri TEXT NOT NULL,
    provenance_group TEXT,
    discovered_at TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    UNIQUE (task_id, canonical_uri)
);

CREATE TABLE research_fetches (
    fetch_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES research_tasks(task_id) ON DELETE RESTRICT,
    result_id TEXT NOT NULL REFERENCES research_results(result_id) ON DELETE RESTRICT,
    source_item_id TEXT,
    asset_id TEXT REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'skipped')),
    failure_kind TEXT,
    attempted_at TEXT NOT NULL,
    fetch_json TEXT NOT NULL CHECK (json_valid(fetch_json)),
    UNIQUE (task_id, result_id)
);

CREATE TABLE research_stop_events (
    stop_event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    stopped_at TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json))
);

CREATE TABLE research_candidate_summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    known_at TEXT NOT NULL,
    summary_hash TEXT NOT NULL CHECK (length(summary_hash) = 64),
    summary_json TEXT NOT NULL CHECK (json_valid(summary_json))
);
CREATE INDEX research_candidate_summaries_run_idx
ON research_candidate_summaries (run_id, known_at, session_id, summary_id);

CREATE TABLE research_failures (
    failure_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES research_tasks(task_id) ON DELETE RESTRICT,
    fetch_id TEXT REFERENCES research_fetches(fetch_id) ON DELETE RESTRICT,
    failure_kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json))
);

CREATE TABLE research_stage_admissions (
    admission_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES stage_artifacts(artifact_id) ON DELETE RESTRICT,
    known_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    semantic_context_hash TEXT NOT NULL CHECK (length(semantic_context_hash) = 64),
    admission_json TEXT NOT NULL CHECK (json_valid(admission_json))
);

CREATE TABLE theses (
    thesis_id TEXT PRIMARY KEY,
    candidate_thesis_id TEXT NOT NULL UNIQUE REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE thesis_revisions (
    revision_id TEXT PRIMARY KEY,
    thesis_id TEXT NOT NULL REFERENCES theses(thesis_id) ON DELETE RESTRICT,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'weakened', 'invalidated', 'closed')),
    created_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    review_at TEXT NOT NULL,
    valid_until TEXT,
    revision_json TEXT NOT NULL CHECK (json_valid(revision_json)),
    UNIQUE (thesis_id, revision_number)
);
CREATE INDEX thesis_revisions_as_of_idx ON thesis_revisions (thesis_id, known_at, revision_number);

CREATE TABLE signal_contributions (
    contribution_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    thesis_revision_id TEXT NOT NULL REFERENCES thesis_revisions(revision_id) ON DELETE RESTRICT,
    relation TEXT NOT NULL CHECK (relation IN ('reinforces', 'contradicts', 'updates', 'independent', 'not_comparable')),
    temporal_compatible INTEGER NOT NULL CHECK (temporal_compatible IN (0, 1)),
    judged_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    contribution_json TEXT NOT NULL CHECK (json_valid(contribution_json)),
    UNIQUE (observation_id, thesis_revision_id)
);

CREATE TABLE portfolio_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE TABLE market_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE TABLE risk_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE TABLE liquidity_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
CREATE TABLE tax_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE TABLE decision_snapshots (
    decision_snapshot_id TEXT PRIMARY KEY,
    decision_hash TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    requested_as_of TEXT NOT NULL,
    decision_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    portfolio_snapshot_id TEXT NOT NULL REFERENCES portfolio_state_snapshots(snapshot_id) ON DELETE RESTRICT,
    market_snapshot_id TEXT NOT NULL REFERENCES market_state_snapshots(snapshot_id) ON DELETE RESTRICT,
    risk_snapshot_id TEXT NOT NULL REFERENCES risk_state_snapshots(snapshot_id) ON DELETE RESTRICT,
    liquidity_snapshot_id TEXT NOT NULL REFERENCES liquidity_state_snapshots(snapshot_id) ON DELETE RESTRICT,
    tax_snapshot_id TEXT NOT NULL REFERENCES tax_state_snapshots(snapshot_id) ON DELETE RESTRICT,
    source_config_hash TEXT NOT NULL,
    strategy_config_hash TEXT NOT NULL,
    execution_config_hash TEXT,
    policy_version TEXT NOT NULL,
    claim_freshness_policy_version TEXT NOT NULL,
    verification_result_ids_json TEXT NOT NULL CHECK (json_valid(verification_result_ids_json)),
    canonical_projection_hashes_json TEXT NOT NULL CHECK (json_valid(canonical_projection_hashes_json)),
    universe_fingerprint TEXT NOT NULL,
    processor_versions_json TEXT NOT NULL CHECK (json_valid(processor_versions_json)),
    calibration_version TEXT NOT NULL,
    optimizer_version TEXT NOT NULL,
    trade_generation_version TEXT NOT NULL,
    execution_eligible INTEGER NOT NULL CHECK (execution_eligible IN (0, 1)),
    model_versions_json TEXT NOT NULL CHECK (json_valid(model_versions_json)),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    CHECK (known_at >= decision_at)
);

CREATE TABLE portfolio_plans (
    plan_id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL UNIQUE,
    decision_snapshot_id TEXT NOT NULL REFERENCES decision_snapshots(decision_snapshot_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE TABLE plan_decisions (
    decision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES portfolio_plans(plan_id) ON DELETE RESTRICT,
    plan_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    expires_at TEXT,
    execution_config_hash TEXT,
    execution_policy_hash TEXT,
    execution_policy_version TEXT,
    broker_environment TEXT CHECK (broker_environment IN ('paper', 'live')),
    account_id TEXT,
    committed_turnover_at_approval REAL,
    CHECK (
        (decision = 'approved'
         AND execution_config_hash IS NOT NULL
         AND execution_policy_hash IS NOT NULL
         AND execution_policy_version IS NOT NULL
         AND broker_environment IS NOT NULL
         AND account_id IS NOT NULL
         AND committed_turnover_at_approval BETWEEN 0 AND 1)
        OR
        (decision = 'rejected'
         AND execution_config_hash IS NULL
         AND execution_policy_hash IS NULL
         AND execution_policy_version IS NULL
         AND broker_environment IS NULL
         AND account_id IS NULL
         AND committed_turnover_at_approval IS NULL)
    )
);
CREATE INDEX plan_decisions_plan_idx ON plan_decisions (plan_hash, decided_at, decision_id);

CREATE TABLE execution_control (
    control_id INTEGER PRIMARY KEY CHECK (control_id = 1),
    execution_disabled INTEGER NOT NULL CHECK (execution_disabled IN (0, 1)),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT,
    policy_version TEXT NOT NULL
);

CREATE TABLE turnover_reservations (
    reservation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES portfolio_plans(plan_id) ON DELETE RESTRICT,
    plan_hash TEXT NOT NULL,
    amount_fraction REAL NOT NULL CHECK (amount_fraction > 0 AND amount_fraction <= 1),
    maximum_fraction REAL NOT NULL CHECK (maximum_fraction > 0 AND maximum_fraction <= 1),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
    reserved_at TEXT NOT NULL,
    terminal_at TEXT,
    reservation_json TEXT NOT NULL CHECK (json_valid(reservation_json)),
    UNIQUE (account_id, trading_date, plan_hash),
    CHECK (amount_fraction <= maximum_fraction),
    CHECK (
        (status = 'reserved' AND terminal_at IS NULL)
        OR (status IN ('settled', 'released') AND terminal_at IS NOT NULL)
    )
);
CREATE INDEX turnover_reservations_cap_idx
ON turnover_reservations (account_id, trading_date, status, reserved_at, reservation_id);

CREATE TABLE execution_claims (
    execution_claim_id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL,
    trade_identity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'submitted', 'partially_filled', 'filled', 'failed', 'cancelled')),
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    broker_order_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    UNIQUE (plan_hash, trade_identity)
);
CREATE INDEX execution_claims_status_idx ON execution_claims (status, updated_at);

CREATE TABLE execution_events (
    event_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    trade_identity TEXT,
    client_order_id TEXT,
    phase TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    broker_order_id TEXT,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json))
);
CREATE INDEX execution_events_plan_idx ON execution_events (plan_hash, occurred_at, event_id);

CREATE TABLE outcome_schedules (
    schedule_id TEXT PRIMARY KEY,
    thesis_revision_id TEXT NOT NULL REFERENCES thesis_revisions(revision_id) ON DELETE RESTRICT,
    plan_id TEXT,
    plan_hash TEXT,
    benchmark_snapshot_id TEXT NOT NULL,
    boundary TEXT NOT NULL CHECK (boundary IN ('event', 'review', 'horizon')),
    observe_at TEXT NOT NULL,
    schedule_json TEXT NOT NULL CHECK (json_valid(schedule_json))
);
CREATE INDEX outcome_schedules_due_idx ON outcome_schedules (observe_at, schedule_id);

CREATE TABLE outcome_metrics (
    metric_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL UNIQUE REFERENCES outcome_schedules(schedule_id) ON DELETE RESTRICT,
    thesis_revision_id TEXT NOT NULL REFERENCES thesis_revisions(revision_id) ON DELETE RESTRICT,
    plan_id TEXT,
    plan_hash TEXT,
    benchmark_snapshot_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL CHECK (json_valid(metrics_json))
);
