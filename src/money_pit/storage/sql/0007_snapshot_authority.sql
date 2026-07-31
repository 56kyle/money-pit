CREATE TABLE portfolio_state_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK (
        length(snapshot_id) = 64
        AND snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE TABLE market_state_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK (
        length(snapshot_id) = 64
        AND snapshot_id NOT GLOB '*[^0-9a-f]*'
    ),
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);

CREATE TABLE thesis_status_history (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id TEXT NOT NULL REFERENCES theses(thesis_id) ON DELETE RESTRICT,
    prior_status TEXT,
    next_status TEXT NOT NULL CHECK (
        next_status IN ('candidate', 'active', 'weakened', 'invalidated', 'closed')
    ),
    transitioned_at TEXT NOT NULL,
    thesis_json TEXT NOT NULL CHECK (json_valid(thesis_json))
);

CREATE INDEX portfolio_state_snapshots_captured_idx
ON portfolio_state_snapshots (captured_at);

CREATE INDEX market_state_snapshots_captured_idx
ON market_state_snapshots (captured_at);

CREATE INDEX thesis_status_history_thesis_idx
ON thesis_status_history (thesis_id, transitioned_at, transition_id);
