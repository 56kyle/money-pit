---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Process Media and PDF as Derived Evidence

## Context and Problem Statement

Video, audio, and PDF sources carry evidence that a plain-text extractor cannot preserve. Video claims need timestamped transcript cues and image-linked frame provenance. PDF claims need page and table locators. Every acquisition path must persist the same result shape so that normal source synchronization does not lose derived assets that research fetching retains.

## Decision Drivers

- Keep transcripts, frame images, source labels, bounding boxes, pages, and tables traceable to immutable bytes.
- Apply one processor contract to source synchronization and iterative research.
- Keep large and untrusted media processing bounded.
- Avoid runtime state or downloader caches outside the content-addressed asset store.
- Retain exact model-to-fragment resolution after compacting rolling captions.

## Considered Options

- Store frame descriptions on the parent video asset and discard frame images.
- Give media processors a separate persistence path.
- Return one common processing bundle with a primary document and content-addressed derived documents.

For PDFs, the considered extractors were `pypdf` text extraction and `pdfplumber` page and table extraction.

## Decision Outcome

Every evidence processor returns an `EvidenceProcessingBundle`. The bundle contains the primary acquisition document and zero or more derived immutable assets with their own evidence documents. Source synchronization and research fetching persist and validate the same bundle contract. A video frame becomes one PNG asset containing all text and source-label fragments detected in that frame. Its fragments keep the parent-video timestamp and bounding box.

YouTube discovery continues through the configured uploads playlist, while fetch acquires the actual bounded media through a read-only `yt-dlp` seam. Media processing uses timestamped transcription and bounded scene sampling. The alias projection removes the longest normalized rolling-caption overlap, groups transcript spans by time and size, groups frame fragments by frame asset and timestamp, and maps each alias back to every contributing durable fragment.

Use `pdfplumber==0.11.10` for PDF page text and table extraction. Require `Pillow>=12.3.0`. Processing enforces byte, page, and elapsed-time bounds and converts parser failures into explicit extraction failures. This choice accepts `pypdfium2` native binaries and a larger untrusted-document attack surface in exchange for table and bounding-box provenance that `pypdf` alone does not supply.

### Consequences

- Good, because reports can embed the exact local frame thumbnail cited by a fragment.
- Good, because normal source sync and research fetch cannot diverge on derived evidence.
- Good, because PDF tables remain distinct from surrounding page prose.
- Bad, because video processing needs optional native dependencies and may be expensive.
- Bad, because PDF processing has a larger dependency and parser attack surface.

## Confirmation

Tests must prove that source sync persists derived frame bytes and grouped fragments, that aliases resolve every contributing fragment, that a 1,652-cue rolling-caption input remains bounded, that YouTube fetch returns media bytes, and that PDF page and table fragments retain exact locators. Ruff, basedpyright, and offline tests must pass without reading or writing the configured runtime data directory.
