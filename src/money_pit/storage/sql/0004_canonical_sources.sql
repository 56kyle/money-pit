DROP TABLE repository_runs;

DROP TABLE legacy_daily_show_runs;

CREATE TABLE source_definitions (
    source_id TEXT PRIMARY KEY,
    adapter_name TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    locator TEXT NOT NULL,
    cadence TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    trust_profile TEXT,
    allowed_uses_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(allowed_uses_json)),
    adapter_config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(adapter_config_json)),
    registry_version INTEGER NOT NULL CHECK (registry_version >= 1),
    indexed_at TEXT NOT NULL
);

CREATE TABLE source_items (
    source_item_id TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES source_definitions(source_id) ON DELETE RESTRICT,
    canonical_uri TEXT NOT NULL,
    published_at TEXT,
    updated_at TEXT,
    discovered_at TEXT NOT NULL,
    content_version TEXT NOT NULL,
    PRIMARY KEY (source_item_id, content_version)
);

CREATE INDEX source_items_source_idx
ON source_items (source_id, discovered_at);
