ALTER TABLE evidence_asset_acquisitions RENAME TO legacy_evidence_asset_acquisitions;

CREATE TABLE evidence_asset_acquisitions (
    acquisition_id INTEGER PRIMARY KEY,
    asset_id TEXT NOT NULL
        REFERENCES evidence_assets(asset_id) ON DELETE RESTRICT,
    source_item_id TEXT,
    content_version TEXT,
    legacy_source_item_id TEXT,
    retrieved_at TEXT NOT NULL,
    media_type TEXT NOT NULL,
    provenance_status TEXT NOT NULL CHECK (
        provenance_status IN ('resolved', 'legacy_unresolved')
    ),
    FOREIGN KEY (source_item_id, content_version)
        REFERENCES source_items(source_item_id, content_version) ON DELETE RESTRICT,
    CHECK (
        (
            provenance_status = 'resolved'
            AND source_item_id IS NOT NULL
            AND content_version IS NOT NULL
            AND legacy_source_item_id IS NULL
        )
        OR
        (
            provenance_status = 'legacy_unresolved'
            AND source_item_id IS NULL
            AND content_version IS NULL
            AND legacy_source_item_id IS NOT NULL
        )
    )
);

INSERT INTO evidence_asset_acquisitions (
    asset_id,
    legacy_source_item_id,
    retrieved_at,
    media_type,
    provenance_status
)
SELECT
    asset_id,
    source_item_id,
    retrieved_at,
    media_type,
    'legacy_unresolved'
FROM legacy_evidence_asset_acquisitions;

DROP TABLE legacy_evidence_asset_acquisitions;

CREATE UNIQUE INDEX evidence_asset_acquisitions_resolved_identity_idx
ON evidence_asset_acquisitions (
    asset_id,
    source_item_id,
    content_version,
    retrieved_at
)
WHERE provenance_status = 'resolved';

CREATE UNIQUE INDEX evidence_asset_acquisitions_legacy_identity_idx
ON evidence_asset_acquisitions (
    asset_id,
    legacy_source_item_id,
    retrieved_at
)
WHERE provenance_status = 'legacy_unresolved';

CREATE INDEX evidence_asset_acquisitions_source_item_idx
ON evidence_asset_acquisitions (
    source_item_id,
    content_version,
    retrieved_at
)
WHERE provenance_status = 'resolved';
