ALTER TABLE source_cursors RENAME TO legacy_source_cursors;

CREATE TABLE source_cursors (
    source_id TEXT NOT NULL,
    cursor_purpose TEXT NOT NULL DEFAULT 'sync' CHECK (cursor_purpose IN ('sync', 'backfill')),
    cursor_json TEXT NOT NULL CHECK (json_valid(cursor_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, cursor_purpose)
);

INSERT INTO source_cursors (
    source_id, cursor_purpose, cursor_json, updated_at
)
SELECT source_id, 'sync', cursor_json, updated_at
FROM legacy_source_cursors;

DROP TABLE legacy_source_cursors;

CREATE TABLE canonical_evidence_assets (
    asset_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    local_path TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    CHECK (asset_id = content_hash)
);

CREATE TABLE canonical_evidence_fragments (
    fragment_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES canonical_evidence_assets(asset_id) ON DELETE RESTRICT,
    fragment_kind TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
    extracted_text TEXT,
    cited_source_text TEXT,
    extraction_method TEXT NOT NULL,
    extraction_model TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);

CREATE TABLE evidence_asset_acquisitions (
    asset_id TEXT NOT NULL REFERENCES canonical_evidence_assets(asset_id) ON DELETE RESTRICT,
    source_item_id TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY (asset_id, source_item_id, retrieved_at)
);

INSERT INTO canonical_evidence_assets (
    asset_id, content_hash, local_path, metadata_json
)
SELECT asset_id, content_hash, local_path, metadata_json
FROM evidence_assets;

INSERT INTO evidence_asset_acquisitions (
    asset_id, source_item_id, retrieved_at, media_type
)
SELECT asset_id, source_item_id, retrieved_at, media_type
FROM evidence_assets
WHERE source_item_id IS NOT NULL;

INSERT INTO canonical_evidence_fragments (
    fragment_id, asset_id, fragment_kind, locator_json, extracted_text,
    cited_source_text, extraction_method, extraction_model, confidence
)
SELECT fragment_id, asset_id, fragment_kind, locator_json, extracted_text,
       cited_source_text, extraction_method, extraction_model, confidence
FROM evidence_fragments;

DROP TABLE evidence_fragments;

DROP TABLE evidence_assets;

ALTER TABLE canonical_evidence_assets RENAME TO evidence_assets;

ALTER TABLE canonical_evidence_fragments RENAME TO evidence_fragments;

CREATE INDEX evidence_fragments_asset_idx
ON evidence_fragments (asset_id);

CREATE INDEX evidence_asset_acquisitions_source_item_idx
ON evidence_asset_acquisitions (source_item_id, retrieved_at);