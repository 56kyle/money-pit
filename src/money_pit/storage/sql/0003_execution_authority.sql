INSERT INTO execution_control (
    control_id,
    execution_disabled,
    changed_at,
    changed_by,
    reason
)
VALUES (
    1,
    1,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
    'migration',
    'Execution remains disabled until explicitly enabled by the operator.'
);

CREATE TABLE execution_claims (
    execution_claim_id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL,
    trade_identity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('claimed', 'submitted', 'partially_filled', 'filled', 'failed', 'cancelled')
    ),
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    broker_order_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    UNIQUE (plan_hash, trade_identity)
);

CREATE INDEX execution_claims_status_idx
ON execution_claims (status, updated_at);
