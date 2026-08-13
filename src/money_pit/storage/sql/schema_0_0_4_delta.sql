CREATE TABLE interpretation_bundles (
    bundle_id TEXT PRIMARY KEY,
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    interpreter_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    bundle_json TEXT NOT NULL CHECK (json_valid(bundle_json)),
    FOREIGN KEY (source_item_id, content_version)
        REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT,
    UNIQUE (source_item_id, content_version, interpreter_version, input_fingerprint),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE INDEX interpretation_bundles_pending_idx
ON interpretation_bundles (status, created_at, bundle_id);

CREATE TABLE interpretation_bundle_chunks (
    chunk_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL REFERENCES interpretation_bundles(bundle_id) ON DELETE RESTRICT,
    chunk_number INTEGER NOT NULL CHECK (chunk_number >= 0),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed')),
    claimed_run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    claimed_at TEXT,
    completed_at TEXT,
    output_attempt_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(output_attempt_ids_json)),
    output_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(output_json)),
    chunk_json TEXT NOT NULL CHECK (json_valid(chunk_json)),
    UNIQUE (bundle_id, chunk_number),
    CHECK (
        (status = 'pending' AND claimed_run_id IS NULL AND claimed_at IS NULL AND completed_at IS NULL)
        OR (status = 'active' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'completed' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX interpretation_bundle_chunks_pending_idx
ON interpretation_bundle_chunks (status, bundle_id, chunk_number);

CREATE TABLE legacy_interpretation_reuse (
    source_item_id TEXT NOT NULL,
    content_version TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES claim_interpretation_attempts(attempt_id) ON DELETE RESTRICT,
    admitted_at TEXT NOT NULL,
    PRIMARY KEY (source_item_id, content_version, asset_id),
    FOREIGN KEY (source_item_id, content_version)
        REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT
);

CREATE TABLE discovery_units (
    unit_id TEXT PRIMARY KEY,
    unit_kind TEXT NOT NULL CHECK (unit_kind IN ('source_bundle', 'canonical_claim', 'universe_entry')),
    subject_id TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    source_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    unit_json TEXT NOT NULL CHECK (json_valid(unit_json)),
    UNIQUE (unit_kind, subject_id, input_fingerprint),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE INDEX discovery_units_pending_idx
ON discovery_units (status, created_at, unit_id);

CREATE TABLE planned_research_task_origins (
    task_id TEXT NOT NULL REFERENCES planned_research_tasks(task_id) ON DELETE RESTRICT,
    unit_id TEXT NOT NULL REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    PRIMARY KEY (task_id, unit_id)
);
CREATE INDEX planned_research_task_origins_unit_idx
ON planned_research_task_origins (unit_id, task_id);

CREATE TABLE discovery_unit_origins (
    unit_id TEXT NOT NULL REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    origin_kind TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    PRIMARY KEY (unit_id, origin_kind, origin_id)
);

CREATE TABLE discovery_batches (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('active', 'checkpointed', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    result_fingerprint TEXT CHECK (result_fingerprint IS NULL OR length(result_fingerprint) = 64),
    output_candidate_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(output_candidate_ids_json)),
    validated_output_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(validated_output_json)),
    batch_json TEXT NOT NULL CHECK (json_valid(batch_json)),
    CHECK (
        (status = 'active' AND completed_at IS NULL AND result_fingerprint IS NULL
            AND validated_output_json = 'null')
        OR (status = 'checkpointed' AND completed_at IS NULL AND result_fingerprint IS NOT NULL
            AND validated_output_json != 'null')
        OR (status = 'completed' AND completed_at IS NOT NULL AND result_fingerprint IS NOT NULL)
    )
);

CREATE TABLE discovery_batch_units (
    batch_id TEXT NOT NULL REFERENCES discovery_batches(batch_id) ON DELETE RESTRICT,
    unit_id TEXT NOT NULL UNIQUE REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    PRIMARY KEY (batch_id, unit_id)
);

CREATE TABLE candidate_discovery_origins (
    candidate_thesis_id TEXT NOT NULL REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    unit_id TEXT NOT NULL REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    batch_id TEXT NOT NULL REFERENCES discovery_batches(batch_id) ON DELETE RESTRICT,
    PRIMARY KEY (candidate_thesis_id, unit_id),
    FOREIGN KEY (batch_id, unit_id)
        REFERENCES discovery_batch_units(batch_id, unit_id) ON DELETE RESTRICT
);
CREATE INDEX candidate_discovery_origins_unit_idx
ON candidate_discovery_origins (unit_id, candidate_thesis_id);

CREATE TABLE research_jobs (
    job_id TEXT PRIMARY KEY,
    parent_job_id TEXT REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    candidate_thesis_id TEXT NOT NULL REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    premise_fingerprint TEXT NOT NULL CHECK (length(premise_fingerprint) = 64),
    cycle_number INTEGER NOT NULL DEFAULT 0 CHECK (cycle_number >= 0),
    review_trigger_at TEXT,
    source_discovery_unit_id TEXT REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'terminal')),
    created_at TEXT NOT NULL,
    claimed_run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    claimed_at TEXT,
    completed_at TEXT,
    stop_reason TEXT,
    wave_count INTEGER NOT NULL DEFAULT 0 CHECK (wave_count BETWEEN 0 AND 3),
    search_count INTEGER NOT NULL DEFAULT 0 CHECK (search_count BETWEEN 0 AND 6),
    accepted_fetch_count INTEGER NOT NULL DEFAULT 0 CHECK (accepted_fetch_count BETWEEN 0 AND 12),
    next_review_at TEXT,
    job_json TEXT NOT NULL CHECK (json_valid(job_json)),
    UNIQUE (candidate_thesis_id, premise_fingerprint, cycle_number),
    CHECK ((cycle_number = 0 AND parent_job_id IS NULL AND review_trigger_at IS NULL)
        OR (cycle_number > 0 AND parent_job_id IS NOT NULL AND review_trigger_at IS NOT NULL)),
    CHECK (
        (status = 'pending' AND claimed_run_id IS NULL AND claimed_at IS NULL AND completed_at IS NULL AND stop_reason IS NULL)
        OR (status = 'active' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL AND completed_at IS NULL AND stop_reason IS NULL)
        OR (status = 'terminal' AND completed_at IS NOT NULL AND stop_reason IS NOT NULL)
    )
);
CREATE INDEX research_jobs_pending_idx
ON research_jobs (status, created_at, job_id);

CREATE TABLE research_job_discovery_origins (
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    unit_id TEXT NOT NULL REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    PRIMARY KEY (job_id, unit_id)
);

CREATE TABLE research_job_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    wave_number INTEGER NOT NULL CHECK (wave_number BETWEEN 0 AND 3),
    search_count INTEGER NOT NULL CHECK (search_count BETWEEN 0 AND 6),
    accepted_fetch_count INTEGER NOT NULL CHECK (accepted_fetch_count BETWEEN 0 AND 12),
    recorded_at TEXT NOT NULL,
    digest_json TEXT NOT NULL CHECK (json_valid(digest_json)),
    UNIQUE (job_id, wave_number)
);

CREATE TABLE research_planner_results (
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    wave_number INTEGER NOT NULL CHECK (wave_number BETWEEN 1 AND 3),
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    recorded_at TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    PRIMARY KEY (job_id, wave_number)
);

CREATE TABLE research_wave_results (
    wave_result_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    wave_number INTEGER NOT NULL CHECK (wave_number BETWEEN 1 AND 3),
    phase TEXT NOT NULL CHECK (phase IN ('provider_completed', 'execution_completed')),
    recorded_at TEXT NOT NULL,
    checkpointed_at TEXT,
    provider_result_json TEXT NOT NULL CHECK (json_valid(provider_result_json)),
    execution_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(execution_json)),
    UNIQUE (job_id, wave_number)
);
CREATE INDEX research_wave_results_uncheckpointed_idx
ON research_wave_results (job_id, checkpointed_at, wave_number);

CREATE TABLE incremental_research_admissions (
    admission_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES stage_artifacts(artifact_id) ON DELETE RESTRICT,
    known_at TEXT NOT NULL,
    input_job_ids_json TEXT NOT NULL CHECK (json_valid(input_job_ids_json)),
    input_wave_result_ids_json TEXT NOT NULL CHECK (json_valid(input_wave_result_ids_json)),
    input_checkpoint_ids_json TEXT NOT NULL CHECK (json_valid(input_checkpoint_ids_json)),
    output_record_ids_json TEXT NOT NULL CHECK (json_valid(output_record_ids_json)),
    admission_json TEXT NOT NULL CHECK (json_valid(admission_json))
);

CREATE TABLE research_uri_admissions (
    admission_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    canonical_uri TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted', 'rejected', 'reused')),
    provenance_group TEXT,
    consumes_fetch_capacity INTEGER NOT NULL CHECK (consumes_fetch_capacity IN (0, 1)),
    admitted_at TEXT NOT NULL,
    reason TEXT,
    source_item_id TEXT,
    asset_id TEXT REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    content_hash TEXT,
    content_version TEXT,
    source_definition_hash TEXT,
    processor_name TEXT,
    processor_version TEXT,
    interpretation_model TEXT,
    interpretation_prompt_version TEXT,
    admission_json TEXT NOT NULL CHECK (json_valid(admission_json)),
    UNIQUE (
        job_id, canonical_uri, content_hash, content_version, source_definition_hash,
        processor_name, processor_version, interpretation_model, interpretation_prompt_version
    ),
    CHECK (disposition = 'accepted' OR consumes_fetch_capacity = 0),
    CHECK (
        disposition = 'rejected'
        OR (content_hash IS NOT NULL AND length(content_hash) = 64
            AND content_version IS NOT NULL AND source_definition_hash IS NOT NULL
            AND processor_name IS NOT NULL AND processor_version IS NOT NULL
            AND interpretation_model IS NOT NULL AND interpretation_prompt_version IS NOT NULL)
    )
);
CREATE INDEX research_uri_admissions_uri_idx
ON research_uri_admissions (canonical_uri, disposition, admitted_at, admission_id);
CREATE INDEX research_uri_admissions_reuse_idx
ON research_uri_admissions (
    canonical_uri, content_hash, content_version, source_definition_hash, processor_name,
    processor_version, interpretation_model, interpretation_prompt_version,
    disposition, admitted_at, admission_id
);

CREATE TABLE synthesis_units (
    unit_id TEXT PRIMARY KEY,
    research_job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'checkpointed', 'completed')),
    created_at TEXT NOT NULL,
    claimed_run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    claimed_at TEXT,
    completed_at TEXT,
    checkpoint_fingerprint TEXT CHECK (checkpoint_fingerprint IS NULL OR length(checkpoint_fingerprint) = 64),
    validated_output_json TEXT NOT NULL DEFAULT 'null' CHECK (json_valid(validated_output_json)),
    unit_payload_hash TEXT NOT NULL CHECK (length(unit_payload_hash) = 64),
    unit_json TEXT NOT NULL CHECK (json_valid(unit_json)),
    UNIQUE (research_job_id, input_fingerprint),
    CHECK (
        (status = 'pending' AND claimed_run_id IS NULL AND claimed_at IS NULL AND completed_at IS NULL
            AND checkpoint_fingerprint IS NULL AND validated_output_json = 'null')
        OR (status = 'active' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL AND completed_at IS NULL
            AND checkpoint_fingerprint IS NULL AND validated_output_json = 'null')
        OR (status = 'checkpointed' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL
            AND completed_at IS NULL AND checkpoint_fingerprint IS NOT NULL AND validated_output_json != 'null')
        OR (status = 'completed' AND claimed_run_id IS NOT NULL AND claimed_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);
CREATE INDEX synthesis_units_pending_idx ON synthesis_units (status, created_at, unit_id);

CREATE TABLE synthesis_outputs (
    output_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL UNIQUE REFERENCES synthesis_units(unit_id) ON DELETE RESTRICT,
    output_fingerprint TEXT NOT NULL CHECK (length(output_fingerprint) = 64),
    created_at TEXT NOT NULL,
    output_record_ids_json TEXT NOT NULL CHECK (json_valid(output_record_ids_json)),
    output_json TEXT NOT NULL CHECK (json_valid(output_json))
);

CREATE TABLE inference_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    work_unit_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('A1', 'A2', 'A3', 'A4')),
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    usage_available INTEGER NOT NULL CHECK (usage_available IN (0, 1)),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    cache_read_tokens INTEGER CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
    cache_write_tokens INTEGER CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    request_count INTEGER CHECK (request_count IS NULL OR request_count > 0),
    elapsed_milliseconds INTEGER NOT NULL CHECK (elapsed_milliseconds >= 0),
    failure_kind TEXT,
    CHECK (
        (status = 'succeeded' AND completed_at IS NOT NULL AND usage_available = 1
            AND input_tokens IS NOT NULL
            AND cache_read_tokens IS NOT NULL AND cache_write_tokens IS NOT NULL
            AND output_tokens IS NOT NULL AND request_count IS NOT NULL AND failure_kind IS NULL)
        OR (status = 'failed' AND completed_at IS NOT NULL AND failure_kind IS NOT NULL
            AND ((usage_available = 1 AND input_tokens IS NOT NULL AND cache_read_tokens IS NOT NULL
                  AND cache_write_tokens IS NOT NULL AND output_tokens IS NOT NULL AND request_count IS NOT NULL)
              OR (usage_available = 0 AND input_tokens IS NULL AND cache_read_tokens IS NULL
                  AND cache_write_tokens IS NULL AND output_tokens IS NULL AND request_count IS NULL)))
    )
);
CREATE INDEX inference_calls_run_idx ON inference_calls (run_id, stage, started_at, call_id);
