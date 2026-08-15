CREATE TABLE hypothesis_variants (
    variant_id TEXT PRIMARY KEY,
    semantic_fingerprint TEXT NOT NULL UNIQUE CHECK (length(semantic_fingerprint) = 64),
    capital_kind TEXT NOT NULL CHECK (capital_kind IN (
        'resolved_instrument', 'instrument_reference', 'universe_reference', 'unclassified_candidate'
    )),
    capital_reference TEXT NOT NULL,
    availability TEXT NOT NULL CHECK (availability IN ('available', 'unavailable')),
    direction TEXT NOT NULL CHECK (direction IN ('long', 'bearish', 'neutral')),
    horizon_class TEXT NOT NULL CHECK (horizon_class IN ('event', 'tactical', 'medium_term', 'structural')),
    theme_key TEXT,
    causal_mechanisms_json TEXT NOT NULL CHECK (json_valid(causal_mechanisms_json)),
    regime_assumptions_json TEXT NOT NULL CHECK (json_valid(regime_assumptions_json)),
    created_at TEXT NOT NULL,
    variant_json TEXT NOT NULL CHECK (json_valid(variant_json))
);
CREATE INDEX hypothesis_variants_coarse_idx
ON hypothesis_variants (capital_kind, capital_reference, direction, variant_id);

CREATE TABLE canonical_hypothesis_groups (
    group_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('current', 'superseded')),
    created_at TEXT NOT NULL
);

CREATE TABLE canonical_hypothesis_group_variants (
    group_id TEXT NOT NULL REFERENCES canonical_hypothesis_groups(group_id) ON DELETE RESTRICT,
    variant_id TEXT NOT NULL REFERENCES hypothesis_variants(variant_id) ON DELETE RESTRICT,
    PRIMARY KEY (group_id, variant_id)
);
CREATE INDEX canonical_hypothesis_group_variants_variant_idx
ON canonical_hypothesis_group_variants (variant_id, group_id);

CREATE TABLE candidate_hypothesis_memberships (
    candidate_thesis_id TEXT PRIMARY KEY REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    variant_id TEXT NOT NULL REFERENCES hypothesis_variants(variant_id) ON DELETE RESTRICT,
    membership_kind TEXT NOT NULL CHECK (membership_kind = 'exact'),
    recorded_at TEXT NOT NULL
);
CREATE INDEX candidate_hypothesis_memberships_variant_idx
ON candidate_hypothesis_memberships (variant_id, candidate_thesis_id);

CREATE TABLE hypothesis_reviews (
    review_id TEXT PRIMARY KEY,
    subject_candidate_id TEXT NOT NULL REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    comparison_candidate_id TEXT NOT NULL REFERENCES candidate_theses(candidate_thesis_id) ON DELETE RESTRICT,
    subject_variant_id TEXT NOT NULL REFERENCES hypothesis_variants(variant_id) ON DELETE RESTRICT,
    comparison_variant_id TEXT NOT NULL REFERENCES hypothesis_variants(variant_id) ON DELETE RESTRICT,
    coarse_fingerprint TEXT NOT NULL CHECK (length(coarse_fingerprint) = 64),
    policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    review_json TEXT NOT NULL CHECK (json_valid(review_json)),
    UNIQUE (subject_candidate_id, comparison_candidate_id, policy_version),
    CHECK (subject_candidate_id < comparison_candidate_id),
    CHECK (subject_variant_id != comparison_variant_id)
);
CREATE INDEX hypothesis_reviews_created_idx ON hypothesis_reviews (created_at, review_id);

CREATE TABLE hypothesis_review_resolutions (
    review_id TEXT PRIMARY KEY REFERENCES hypothesis_reviews(review_id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK (decision IN ('same', 'distinct')),
    resolved_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    resolution_json TEXT NOT NULL CHECK (json_valid(resolution_json))
);

CREATE TABLE canonical_hypothesis_group_supersessions (
    successor_group_id TEXT NOT NULL REFERENCES canonical_hypothesis_groups(group_id) ON DELETE RESTRICT,
    predecessor_group_id TEXT NOT NULL REFERENCES canonical_hypothesis_groups(group_id) ON DELETE RESTRICT,
    review_id TEXT NOT NULL REFERENCES hypothesis_review_resolutions(review_id) ON DELETE RESTRICT,
    PRIMARY KEY (successor_group_id, predecessor_group_id),
    CHECK (successor_group_id != predecessor_group_id)
);
CREATE UNIQUE INDEX canonical_hypothesis_group_supersessions_predecessor_idx
ON canonical_hypothesis_group_supersessions (predecessor_group_id);

CREATE TABLE research_cases (
    case_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES canonical_hypothesis_groups(group_id) ON DELETE RESTRICT,
    scope_fingerprint TEXT NOT NULL CHECK (length(scope_fingerprint) = 64),
    head_job_id TEXT REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (hypothesis_id, scope_fingerprint)
);

CREATE TABLE research_job_semantics (
    job_id TEXT PRIMARY KEY REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    case_id TEXT NOT NULL REFERENCES research_cases(case_id) ON DELETE RESTRICT,
    semantic_premise_fingerprint TEXT NOT NULL CHECK (length(semantic_premise_fingerprint) = 64),
    successor_job_id TEXT REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    disposition TEXT NOT NULL CHECK (disposition IN ('current', 'superseded', 'unavailable')),
    recorded_at TEXT NOT NULL,
    CHECK (job_id != successor_job_id),
    CHECK ((disposition = 'superseded') = (successor_job_id IS NOT NULL))
);
CREATE UNIQUE INDEX research_job_semantics_one_current_idx
ON research_job_semantics (case_id) WHERE disposition = 'current';

CREATE TABLE research_job_predecessors (
    successor_job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    predecessor_job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    PRIMARY KEY (successor_job_id, predecessor_job_id),
    CHECK (successor_job_id != predecessor_job_id)
);
CREATE INDEX research_job_predecessors_predecessor_idx
ON research_job_predecessors (predecessor_job_id, successor_job_id);

CREATE TABLE research_job_sessions (
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL UNIQUE REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (job_id, session_id)
);

CREATE TABLE research_job_tasks (
    job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES planned_research_tasks(task_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('initial', 'planner_followup')),
    execution_status TEXT NOT NULL CHECK (execution_status IN ('pending', 'materialized', 'completed', 'cancelled', 'reused')),
    materialized_session_id TEXT REFERENCES research_sessions(session_id) ON DELETE RESTRICT,
    materialized_task_id TEXT,
    completed_at TEXT,
    reused_from_job_id TEXT,
    reused_from_task_id TEXT,
    binding_json TEXT NOT NULL CHECK (json_valid(binding_json)),
    PRIMARY KEY (job_id, task_id),
    CHECK (
        (execution_status = 'pending' AND materialized_session_id IS NULL AND materialized_task_id IS NULL AND completed_at IS NULL)
        OR (execution_status = 'materialized' AND materialized_session_id IS NOT NULL AND materialized_task_id IS NOT NULL AND completed_at IS NULL)
        OR (execution_status = 'completed' AND completed_at IS NOT NULL
            AND reused_from_job_id IS NULL AND reused_from_task_id IS NULL)
        OR (execution_status = 'reused' AND completed_at IS NOT NULL
            AND reused_from_job_id IS NOT NULL AND reused_from_task_id IS NOT NULL)
        OR (execution_status = 'cancelled' AND completed_at IS NOT NULL)
    ),
    FOREIGN KEY (reused_from_job_id, reused_from_task_id)
        REFERENCES research_job_tasks(job_id, task_id) ON DELETE RESTRICT
);
CREATE INDEX research_job_tasks_due_idx
ON research_job_tasks (job_id, execution_status, role, task_id);

CREATE TABLE research_job_task_origins (
    job_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    unit_id TEXT NOT NULL REFERENCES discovery_units(unit_id) ON DELETE RESTRICT,
    PRIMARY KEY (job_id, task_id, unit_id),
    FOREIGN KEY (job_id, task_id) REFERENCES research_job_tasks(job_id, task_id) ON DELETE RESTRICT
);
CREATE INDEX research_job_task_origins_unit_idx
ON research_job_task_origins (unit_id, job_id, task_id);

CREATE TABLE synthesis_material_states (
    material_state_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES canonical_hypothesis_groups(group_id) ON DELETE RESTRICT,
    material_fingerprint TEXT NOT NULL CHECK (length(material_fingerprint) = 64),
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible', 'insufficient_evidence', 'unavailable')),
    prior_revision_id TEXT REFERENCES thesis_revisions(revision_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    assessment_json TEXT NOT NULL CHECK (json_valid(assessment_json)),
    UNIQUE (hypothesis_id, material_fingerprint)
);
CREATE INDEX synthesis_material_states_eligibility_idx
ON synthesis_material_states (eligibility, created_at, material_state_id);

CREATE TABLE synthesis_material_origins (
    material_state_id TEXT NOT NULL REFERENCES synthesis_material_states(material_state_id) ON DELETE RESTRICT,
    research_job_id TEXT NOT NULL REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    PRIMARY KEY (material_state_id, research_job_id)
);

CREATE TABLE synthesis_unit_semantics (
    unit_id TEXT PRIMARY KEY REFERENCES synthesis_units(unit_id) ON DELETE RESTRICT,
    material_state_id TEXT NOT NULL REFERENCES synthesis_material_states(material_state_id) ON DELETE RESTRICT,
    disposition TEXT NOT NULL CHECK (disposition IN ('current', 'superseded', 'unavailable')),
    successor_unit_id TEXT REFERENCES synthesis_units(unit_id) ON DELETE RESTRICT,
    successor_research_job_id TEXT REFERENCES research_jobs(job_id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL,
    CHECK (unit_id != successor_unit_id),
    CHECK ((disposition = 'superseded') =
           ((successor_unit_id IS NOT NULL) != (successor_research_job_id IS NOT NULL)))
);
CREATE UNIQUE INDEX synthesis_unit_semantics_one_current_idx
ON synthesis_unit_semantics (material_state_id) WHERE disposition = 'current';
