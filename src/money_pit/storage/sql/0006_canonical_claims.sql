CREATE TABLE canonical_claims (
    canonical_claim_key TEXT PRIMARY KEY,
    current_status TEXT NOT NULL CHECK (
        current_status IN ('active', 'superseded', 'expired', 'disputed')
    ),
    active_observation_ids_json TEXT NOT NULL CHECK (json_valid(active_observation_ids_json)),
    last_material_change_at TEXT NOT NULL,
    next_refresh_at TEXT
);

CREATE INDEX canonical_claims_refresh_idx
ON canonical_claims (next_refresh_at, current_status);
