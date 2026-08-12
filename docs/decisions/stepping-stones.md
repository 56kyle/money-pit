# Stepping-Stone Ledger

## Definition-scoped source-item version compatibility

- Introduced by: ADR 0065.
- Stepping stone: encode the definition hash after the content digest in new YouTube
  `content_version` values.
- Terminus: migrate the `source_items` identity and acquisition foreign key so
  `source_definition_hash` is part of the durable source-item key, then return YouTube content
  versions to the plain content digest.
- Done condition: existing databases migrate without provenance loss; two definition revisions can
  persist the same source item and digest as distinct rows; cache lookup and processing tests pass
  without the definition-scoped suffix path.
