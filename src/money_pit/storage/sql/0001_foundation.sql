CREATE TABLE repository_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    run_path TEXT NOT NULL UNIQUE,
    manifest_path TEXT NOT NULL UNIQUE,
    origin TEXT NOT NULL CHECK (origin IN ('native', 'legacy'))
);

CREATE TABLE legacy_daily_show_runs (
    legacy_run_id TEXT PRIMARY KEY,
    legacy_path TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    inferred_created_at TEXT,
    indexed_at TEXT NOT NULL,
    artifact_names_json TEXT NOT NULL CHECK (json_valid(artifact_names_json))
);

CREATE INDEX legacy_daily_show_runs_slug_idx
ON legacy_daily_show_runs (slug);
