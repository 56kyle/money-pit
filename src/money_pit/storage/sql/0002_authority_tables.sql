CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    run_path TEXT NOT NULL UNIQUE,
    manifest_path TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL CHECK (origin IN ('native', 'legacy'))
);

CREATE TABLE legacy_runs (
    legacy_run_id TEXT PRIMARY KEY,
    legacy_path TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    inferred_created_at TEXT,
    indexed_at TEXT NOT NULL,
    artifact_names_json TEXT NOT NULL CHECK (json_valid(artifact_names_json))
);

CREATE TABLE legacy_artifacts (
    legacy_artifact_id TEXT PRIMARY KEY,
    legacy_run_id TEXT NOT NULL REFERENCES legacy_runs(legacy_run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    indexed_at TEXT NOT NULL,
    UNIQUE (legacy_run_id, relative_path)
);

CREATE TABLE source_cursors (
    source_id TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL CHECK (json_valid(cursor_json)),
    updated_at TEXT NOT NULL
);

CREATE TABLE evidence_assets (
    asset_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    source_item_id TEXT,
    local_path TEXT NOT NULL UNIQUE,
    retrieved_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json))
);

CREATE TABLE evidence_fragments (
    fragment_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    fragment_kind TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
    extracted_text TEXT,
    cited_source_text TEXT,
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);

CREATE VIRTUAL TABLE evidence_fragment_search USING fts5(
    fragment_id UNINDEXED,
    extracted_text,
    cited_source_text
);

CREATE TABLE claim_observations (
    observation_id TEXT PRIMARY KEY,
    canonical_claim_key TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind IN ('factual', 'forecast', 'opinion', 'strategy')),
    source_item_id TEXT NOT NULL,
    asserted_at TEXT NOT NULL,
    valid_from TEXT,
    horizon TEXT,
    expires_at TEXT,
    supersedes_observation_id TEXT REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    subjects_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(subjects_json)),
    instruments_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(instruments_json)),
    evidence_fragment_ids_json TEXT NOT NULL CHECK (json_valid(evidence_fragment_ids_json))
);

CREATE TABLE verification_results (
    verification_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES claim_observations(observation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('supported', 'contradicted', 'mixed', 'unresolved')),
    supporting_evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(supporting_evidence_ids_json)),
    contradicting_evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(contradicting_evidence_ids_json)),
    checked_at TEXT NOT NULL,
    valid_until TEXT,
    verifier_version TEXT NOT NULL,
    limitations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(limitations_json))
);

CREATE TABLE theses (
    thesis_id TEXT PRIMARY KEY,
    instrument_or_theme TEXT NOT NULL,
    direction TEXT NOT NULL,
    horizon TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'weakened', 'invalidated', 'closed')),
    supporting_claim_keys_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(supporting_claim_keys_json)),
    contradicting_claim_keys_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(contradicting_claim_keys_json)),
    scenario_distribution_json TEXT NOT NULL CHECK (json_valid(scenario_distribution_json)),
    invalidation_rules_json TEXT NOT NULL CHECK (json_valid(invalidation_rules_json)),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE portfolio_plans (
    plan_id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    portfolio_snapshot_id TEXT NOT NULL,
    market_snapshot_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    model_versions_json TEXT NOT NULL CHECK (json_valid(model_versions_json)),
    prompt_versions_json TEXT NOT NULL CHECK (json_valid(prompt_versions_json)),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json))
);

CREATE TABLE plan_decisions (
    decision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES portfolio_plans(plan_id) ON DELETE RESTRICT,
    plan_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE execution_control (
    control_id INTEGER PRIMARY KEY CHECK (control_id = 1),
    execution_disabled INTEGER NOT NULL CHECK (execution_disabled IN (0, 1)),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT
);

CREATE INDEX legacy_artifacts_run_idx ON legacy_artifacts (legacy_run_id);
CREATE INDEX evidence_fragments_asset_idx ON evidence_fragments (asset_id);
CREATE INDEX claim_observations_key_idx ON claim_observations (canonical_claim_key, asserted_at);
CREATE INDEX verification_results_observation_idx ON verification_results (observation_id, checked_at);
CREATE INDEX theses_status_idx ON theses (status, reviewed_at);
CREATE INDEX plan_decisions_plan_idx ON plan_decisions (plan_id, decided_at);
