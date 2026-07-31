CREATE TEMP TABLE claim_recorded_at_backfill (
    boundary TEXT NOT NULL
);

INSERT INTO claim_recorded_at_backfill (boundary)
VALUES (strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));

CREATE TABLE claim_observations_with_recorded_at (
    observation_id TEXT PRIMARY KEY,
    canonical_claim_key TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    claim_kind TEXT NOT NULL CHECK (claim_kind IN ('factual', 'forecast', 'opinion', 'strategy')),
    source_item_id TEXT NOT NULL,
    asserted_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT,
    horizon TEXT,
    expires_at TEXT,
    supersedes_observation_id TEXT REFERENCES claim_observations_with_recorded_at(observation_id) ON DELETE RESTRICT,
    subjects_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(subjects_json)),
    instruments_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(instruments_json)),
    evidence_fragment_ids_json TEXT NOT NULL CHECK (json_valid(evidence_fragment_ids_json))
);

INSERT INTO claim_observations_with_recorded_at (
    observation_id, canonical_claim_key, claim_text, claim_kind, source_item_id,
    asserted_at, recorded_at, valid_from, horizon, expires_at,
    supersedes_observation_id, subjects_json, instruments_json,
    evidence_fragment_ids_json
)
SELECT
    observation_id, canonical_claim_key, claim_text, claim_kind, source_item_id,
    asserted_at, (SELECT boundary FROM claim_recorded_at_backfill),
    valid_from, horizon, expires_at,
    supersedes_observation_id, subjects_json, instruments_json,
    evidence_fragment_ids_json
FROM claim_observations;

CREATE TABLE verification_results_with_recorded_at (
    verification_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES claim_observations_with_recorded_at(observation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('supported', 'contradicted', 'mixed', 'unresolved')),
    supporting_evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(supporting_evidence_ids_json)),
    contradicting_evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(contradicting_evidence_ids_json)),
    checked_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_until TEXT,
    verifier_version TEXT NOT NULL,
    limitations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(limitations_json))
);

INSERT INTO verification_results_with_recorded_at (
    verification_id, observation_id, status, supporting_evidence_ids_json,
    contradicting_evidence_ids_json, checked_at, recorded_at, valid_until,
    verifier_version, limitations_json
)
SELECT
    verification_id, observation_id, status, supporting_evidence_ids_json,
    contradicting_evidence_ids_json, checked_at,
    (SELECT boundary FROM claim_recorded_at_backfill), valid_until,
    verifier_version, limitations_json
FROM verification_results;

DROP TABLE verification_results;
DROP TABLE claim_observations;
ALTER TABLE claim_observations_with_recorded_at RENAME TO claim_observations;
ALTER TABLE verification_results_with_recorded_at RENAME TO verification_results;
DROP TABLE claim_recorded_at_backfill;

CREATE INDEX claim_observations_key_idx
ON claim_observations (canonical_claim_key, recorded_at, asserted_at);

CREATE INDEX verification_results_observation_idx
ON verification_results (observation_id, recorded_at, checked_at);
