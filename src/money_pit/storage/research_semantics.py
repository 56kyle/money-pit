"""Module serializing durable A3 semantic context without research-package imports."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import cast


if TYPE_CHECKING:
    import sqlite3

    from pydantic import JsonValue

    from money_pit.schemas.research import ResearchStageAdmission


_EMPTY_JSON_FIELDS: frozenset[str] = frozenset()


class ResearchSemanticPayloadError(ValueError):
    """Raised when durable A3 summaries cannot form the canonical artifact payload."""


def canonical_research_payload(summary_json_values: tuple[str, ...]) -> dict[str, JsonValue]:
    """Build the sole ResearchArtifactPayload shape from canonical summary wrappers."""
    candidates: list[JsonValue] = []
    contexts: list[JsonValue] = []
    alias_bindings: list[JsonValue] = []
    alias_counters: dict[str, int] = {"E": 0, "F": 0, "T": 0}
    for encoded in summary_json_values:
        candidate, row_contexts, row_aliases = _parse_summary(encoded)
        candidates.append(cast("JsonValue", candidate))
        row_context_values, row_binding_values = _reindex_summary_contexts(
            row_contexts,
            row_aliases,
            alias_counters,
        )
        contexts.extend(row_context_values)
        alias_bindings.extend(row_binding_values)
    return {
        "candidates": candidates,
        "contexts": contexts,
        "alias_bindings": alias_bindings,
    }


def _parse_summary(encoded: str) -> tuple[object, list[object], list[object]]:
    try:
        parsed = cast("object", json.loads(encoded))
    except json.JSONDecodeError as error:
        raise ResearchSemanticPayloadError("Research summary JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise ResearchSemanticPayloadError("Research summary is not an object")
    summary = cast("dict[str, object]", parsed)
    candidate = summary.get("candidate")
    row_contexts = summary.get("contexts")
    row_aliases = summary.get("alias_bindings")
    if candidate is None or not isinstance(row_contexts, list) or not isinstance(row_aliases, list):
        raise ResearchSemanticPayloadError("Research summary has an invalid semantic shape")
    return candidate, cast("list[object]", row_contexts), cast("list[object]", row_aliases)


def _reindex_summary_contexts(
    row_contexts: list[object],
    row_aliases: list[object],
    alias_counters: dict[str, int],
) -> tuple[list[JsonValue], list[JsonValue]]:
    binding_by_alias = _bindings_by_alias(row_aliases)
    used_aliases: set[str] = set()
    contexts: list[JsonValue] = []
    bindings: list[JsonValue] = []
    for value in row_contexts:
        context, context_bindings = _reindex_context(
            value,
            binding_by_alias,
            used_aliases,
            alias_counters,
        )
        contexts.append(context)
        bindings.extend(context_bindings)
    if used_aliases != binding_by_alias.keys():
        raise ResearchSemanticPayloadError("Research summary alias bindings must have exact context ownership")
    return contexts, bindings


def _reindex_context(
    value: object,
    binding_by_alias: dict[str, dict[str, object]],
    used_aliases: set[str],
    alias_counters: dict[str, int],
) -> tuple[JsonValue, list[JsonValue]]:
    if not isinstance(value, dict):
        raise ResearchSemanticPayloadError("Research context summary is not an object")
    context = dict(cast("dict[str, object]", value))
    _ = context.pop("alias_bindings", None)
    evidence = context.get("evidence")
    if not isinstance(evidence, list):
        raise ResearchSemanticPayloadError("Research context evidence is not a list")
    records: list[JsonValue] = []
    bindings: list[JsonValue] = []
    for record_value in cast("list[object]", evidence):
        record, binding = _reindex_evidence(
            record_value,
            binding_by_alias,
            used_aliases,
            alias_counters,
        )
        records.append(record)
        bindings.append(binding)
    context["evidence"] = records
    return cast("JsonValue", context), bindings


def _reindex_evidence(
    value: object,
    binding_by_alias: dict[str, dict[str, object]],
    used_aliases: set[str],
    alias_counters: dict[str, int],
) -> tuple[JsonValue, JsonValue]:
    if not isinstance(value, dict):
        raise ResearchSemanticPayloadError("Research evidence record is not an object")
    record = dict(cast("dict[str, object]", value))
    original_alias = record.get("alias")
    if not isinstance(original_alias, str) or original_alias in used_aliases:
        raise ResearchSemanticPayloadError("Research context aliases must be unique within one summary")
    binding = binding_by_alias.get(original_alias)
    if binding is None:
        raise ResearchSemanticPayloadError("Research context evidence has no exact alias binding")
    prefix = original_alias[:1]
    if prefix not in alias_counters:
        raise ResearchSemanticPayloadError("Research evidence alias prefix is invalid")
    alias_counters[prefix] += 1
    canonical_alias = f"{prefix}{alias_counters[prefix]:06d}"
    record["alias"] = canonical_alias
    canonical_binding = dict(binding)
    canonical_binding["alias"] = canonical_alias
    used_aliases.add(original_alias)
    return cast("JsonValue", record), cast("JsonValue", canonical_binding)


def _bindings_by_alias(values: list[object]) -> dict[str, dict[str, object]]:
    bindings: dict[str, dict[str, object]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ResearchSemanticPayloadError("Research alias binding is not an object")
        binding = dict(cast("dict[str, object]", value))
        alias = binding.get("alias")
        if not isinstance(alias, str) or alias in bindings:
            raise ResearchSemanticPayloadError("Research summary alias bindings must be unique")
        bindings[alias] = binding
    return bindings


def canonical_research_context(
    connection: sqlite3.Connection,
    admission: ResearchStageAdmission,
) -> str:
    """Serialize exact durable A3 semantics without provider or model execution."""
    sections: dict[str, object] = {
        "run_id": admission.run_id,
        "sessions": _records(
            connection,
            """SELECT session_id, run_id, scope_kind, scope_subject_id, status, stop_reason,
                      query_count, fetch_count, session_json
               FROM research_sessions
               WHERE session_id IN (SELECT value FROM json_each(?)) ORDER BY session_id""",
            admission.session_ids,
            (
                "session_id",
                "run_id",
                "scope_kind",
                "scope_subject_id",
                "status",
                "stop_reason",
                "query_count",
                "fetch_count",
                "session",
            ),
            json_fields=frozenset({"session"}),
        ),
        "summaries": _records(
            connection,
            """SELECT summary_id, session_id, run_id, known_at, summary_hash, summary_json
               FROM research_candidate_summaries
               WHERE summary_id IN (SELECT value FROM json_each(?)) ORDER BY summary_id""",
            admission.summary_ids,
            ("summary_id", "session_id", "run_id", "known_at", "summary_hash", "summary"),
            json_fields=frozenset({"summary"}),
        ),
        "planned_tasks": _records(
            connection,
            """SELECT task_id, candidate_thesis_id, status, materialized_session_id,
                      materialized_task_id, completed_at, task_json
               FROM planned_research_tasks
               WHERE task_id IN (SELECT value FROM json_each(?)) ORDER BY task_id""",
            admission.planned_task_ids,
            (
                "task_id",
                "candidate_thesis_id",
                "status",
                "materialized_session_id",
                "materialized_task_id",
                "completed_at",
                "task",
            ),
            json_fields=frozenset({"task"}),
        ),
        "tasks": _records(
            connection,
            """SELECT task_id, session_id, round_number, status, task_json
               FROM research_tasks WHERE task_id IN (SELECT value FROM json_each(?)) ORDER BY task_id""",
            admission.task_ids,
            ("task_id", "session_id", "round_number", "status", "task"),
            json_fields=frozenset({"task"}),
        ),
        "results": _records(
            connection,
            """SELECT result_id, task_id, result_json FROM research_results
               WHERE result_id IN (SELECT value FROM json_each(?)) ORDER BY result_id""",
            admission.result_ids,
            ("result_id", "task_id", "result"),
            json_fields=frozenset({"result"}),
        ),
        "fetches": _records(
            connection,
            """SELECT fetch_id, task_id, result_id, status, fetch_json FROM research_fetches
               WHERE fetch_id IN (SELECT value FROM json_each(?)) ORDER BY fetch_id""",
            admission.fetch_ids,
            ("fetch_id", "task_id", "result_id", "status", "fetch"),
            json_fields=frozenset({"fetch"}),
        ),
        "failures": _records(
            connection,
            """SELECT failure_id, session_id, task_id, fetch_id, failure_kind,
                      occurred_at, detail_json FROM research_failures
               WHERE failure_id IN (SELECT value FROM json_each(?)) ORDER BY failure_id""",
            admission.failure_ids,
            (
                "failure_id",
                "session_id",
                "task_id",
                "fetch_id",
                "failure_kind",
                "occurred_at",
                "detail",
            ),
            json_fields=frozenset({"detail"}),
        ),
        "stops": _records(
            connection,
            """SELECT stop_event_id, session_id, reason, stopped_at, detail_json
               FROM research_stop_events
               WHERE stop_event_id IN (SELECT value FROM json_each(?)) ORDER BY stop_event_id""",
            admission.stop_event_ids,
            ("stop_event_id", "session_id", "reason", "stopped_at", "detail"),
            json_fields=frozenset({"detail"}),
        ),
        "source_definitions": _source_definitions(connection, admission.source_item_ids),
        "fragments": _records(
            connection,
            """SELECT fragment_id, asset_id, fragment_kind, locator_json, extracted_text,
                      cited_source_text, extraction_method, extraction_model, confidence
               FROM evidence_fragments
               WHERE fragment_id IN (SELECT value FROM json_each(?)) ORDER BY fragment_id""",
            admission.fragment_ids,
            (
                "fragment_id",
                "asset_id",
                "kind",
                "locator",
                "extracted_text",
                "cited_source_text",
                "extraction_method",
                "extraction_model",
                "confidence",
            ),
            json_fields=frozenset({"locator"}),
        ),
        "interpretations": _records(
            connection,
            """SELECT attempt_id, source_item_id, content_version, asset_id, interpreter_version,
                      outcome, failure_kind, observation_ids_json
               FROM claim_interpretation_attempts
               WHERE attempt_id IN (SELECT value FROM json_each(?)) ORDER BY attempt_id""",
            admission.interpretation_attempt_ids,
            (
                "attempt_id",
                "source_item_id",
                "content_version",
                "asset_id",
                "interpreter_version",
                "outcome",
                "failure_kind",
                "observation_ids",
            ),
            json_fields=frozenset({"observation_ids"}),
        ),
        "observations": _records(
            connection,
            """SELECT observation_id, observation_json FROM claim_observations
               WHERE observation_id IN (SELECT value FROM json_each(?)) ORDER BY observation_id""",
            admission.observation_ids,
            ("observation_id", "observation"),
            json_fields=frozenset({"observation"}),
        ),
    }
    return json.dumps(sections, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _records(
    connection: sqlite3.Connection,
    sql: str,
    identifiers: tuple[str, ...],
    fields: tuple[str, ...],
    *,
    json_fields: frozenset[str] = _EMPTY_JSON_FIELDS,
) -> tuple[dict[str, object], ...]:
    rows: list[sqlite3.Row] = cast(
        "list[sqlite3.Row]",
        connection.execute(
            sql,
            (json.dumps(identifiers, separators=(",", ":")),),
        ).fetchall(),
    )
    records: list[dict[str, object]] = []
    for row in rows:
        record: dict[str, object] = {}
        for index, field in enumerate(fields):
            value = cast("object", row[index])
            record[field] = json.loads(str(value)) if field in json_fields and value is not None else value
        records.append(record)
    return tuple(records)


def _source_definitions(
    connection: sqlite3.Connection,
    source_item_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    return _records(
        connection,
        """SELECT DISTINCT revision.definition_hash, revision.provenance_group,
                  revision.definition_json
           FROM source_items AS item
           JOIN source_definition_revisions AS revision
             ON revision.definition_hash = item.source_definition_hash
           WHERE item.source_item_id IN (SELECT value FROM json_each(?))
           ORDER BY revision.definition_hash""",
        source_item_ids,
        ("definition_hash", "provenance_group", "definition"),
        json_fields=frozenset({"definition"}),
    )
