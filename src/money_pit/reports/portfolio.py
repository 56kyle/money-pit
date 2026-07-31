"""Module containing pure static portfolio-review renderers."""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue

from money_pit.schemas.portfolio_plan import PortfolioPlan  # noqa: TC001


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class ReportReadError(Exception):
    """Raised when a typed portfolio-review input cannot be read."""


class ReportRenderError(Exception):
    """Raised when a static report cannot be rendered safely."""


class ReportEvidence(BaseModel):
    """One inert, human-readable evidence locator in a portfolio report."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class PortfolioReview(BaseModel):
    """Typed data required by JSON, Markdown, and HTML portfolio reports."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    plan: PortfolioPlan
    current_weights: dict[str, float]
    risk_contributions: dict[str, float]
    sector_exposures: dict[str, float]
    factor_exposures: dict[str, float]
    thesis_statuses: dict[str, str]
    conflicting_claims: tuple[str, ...]
    source_reliability: dict[str, float]
    evidence: tuple[ReportEvidence, ...]
    scenarios: tuple[dict[str, JsonValue], ...]
    sensitivities: tuple[dict[str, JsonValue], ...]
    approval_state: str = Field(min_length=1)
    execution_state: str = Field(min_length=1)


def load_portfolio_review(path: Path) -> PortfolioReview:
    """Read and validate a complete portfolio-review snapshot."""
    try:
        serialized: str = path.read_text(encoding="utf-8")
        return PortfolioReview.model_validate_json(serialized)
    except (OSError, ValueError) as exc:
        raise ReportReadError(f"cannot read portfolio review from {path}") from exc


def render_json(review: PortfolioReview) -> str:
    """Render a deterministic JSON portfolio review."""
    return review.model_dump_json(indent=2)


def _markdown_text(value: object) -> str:
    text: str = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("`", "\\`")
        .replace("!", "\\!")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\n", " ")
        .replace("<", "\\<")
        .replace(">", "\\>")
    )


def _markdown_mapping(title: str, values: Mapping[str, object]) -> list[str]:
    lines: list[str] = [f"## {title}", "", "| Name | Value |", "| --- | ---: |"]
    lines.extend(f"| {_markdown_text(name)} | {_markdown_text(value)} |" for name, value in sorted(values.items()))
    lines.append("")
    return lines


def render_markdown(review: PortfolioReview) -> str:
    """Render an inert Markdown portfolio review."""
    payload = review.plan.payload
    lines: list[str] = [
        f"# {_markdown_text(review.title)}",
        "",
        f"Plan: `{_markdown_text(payload.plan_id)}`",
        "",
        f"Plan hash: `{review.plan.plan_hash}`",
        "",
        f"Approval: {_markdown_text(review.approval_state)}",
        "",
        f"Execution: {_markdown_text(review.execution_state)}",
        "",
    ]
    lines.extend(_markdown_mapping("Current allocation", review.current_weights))
    lines.extend(_markdown_mapping("Target allocation", payload.target_weights))
    lines.extend(_markdown_mapping("Risk contribution", review.risk_contributions))
    lines.extend(_markdown_mapping("Sector exposure", review.sector_exposures))
    lines.extend(_markdown_mapping("Factor exposure", review.factor_exposures))
    lines.extend(_markdown_mapping("Theses", review.thesis_statuses))
    lines.extend(_markdown_mapping("Source reliability", review.source_reliability))
    lines.extend(
        [
            "## Proposed trades",
            "",
            "| Instrument | Side | Quantity | Notional | Tax known |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    lines.extend(
        "| {instrument} | {side} | {quantity} | {notional} | {tax_known} |".format(
            instrument=_markdown_text(trade.instrument),
            side=_markdown_text(trade.side),
            quantity=trade.quantity,
            notional=trade.estimated_notional,
            tax_known=trade.tax_cost_known,
        )
        for trade in payload.proposed_trades
    )
    lines.extend(["", "## Evidence", "", "| Label | Locator | Source | Status |", "| --- | --- | --- | --- |"])
    lines.extend(
        "| {label} | {locator} | {source} | {status} |".format(
            label=_markdown_text(evidence.label),
            locator=_markdown_text(evidence.locator),
            source=_markdown_text(evidence.source_item_id),
            status=_markdown_text(evidence.status),
        )
        for evidence in review.evidence
    )
    lines.extend(["", "## Conflicting claims", ""])
    lines.extend(f"- {_markdown_text(claim)}" for claim in review.conflicting_claims)
    lines.extend(["", "## Rejected candidates", ""])
    lines.extend(
        f"- {_markdown_text(candidate.instrument)}: {_markdown_text('; '.join(candidate.reasons))}"
        for candidate in payload.rejected_candidates
    )
    lines.extend(
        _markdown_mapping(
            "Plan estimates",
            {
                "turnover": payload.turnover_estimate,
                "expected risk change": payload.expected_risk_change,
                "expected return change": payload.expected_return_change,
            },
        )
    )
    lines.extend(_markdown_mapping("Tax estimates", payload.tax_estimates))
    lines.extend(["", "## Scenarios", ""])
    lines.extend(f"- {_markdown_text(json.dumps(scenario, sort_keys=True))}" for scenario in review.scenarios)
    lines.extend(["", "## Sensitivities", ""])
    lines.extend(f"- {_markdown_text(json.dumps(sensitivity, sort_keys=True))}" for sensitivity in review.sensitivities)
    lines.append("")
    return "\n".join(lines)


def _html_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _html_mapping(title: str, values: Mapping[str, object]) -> str:
    rows: str = "".join(
        f"<tr><th>{_html_text(name)}</th><td>{_html_text(value)}</td></tr>" for name, value in sorted(values.items())
    )
    return f"<section><h2>{_html_text(title)}</h2><table>{rows}</table></section>"


def render_html(review: PortfolioReview) -> str:
    """Render a self-contained inert HTML portfolio review."""
    payload = review.plan.payload
    sections: list[str] = [
        _html_mapping("Current allocation", review.current_weights),
        _html_mapping("Target allocation", payload.target_weights),
        _html_mapping("Risk contribution", review.risk_contributions),
        _html_mapping("Sector exposure", review.sector_exposures),
        _html_mapping("Factor exposure", review.factor_exposures),
        _html_mapping("Theses", review.thesis_statuses),
        _html_mapping("Source reliability", review.source_reliability),
        _html_mapping(
            "Plan estimates",
            {
                "turnover": payload.turnover_estimate,
                "expected risk change": payload.expected_risk_change,
                "expected return change": payload.expected_return_change,
            },
        ),
        _html_mapping("Tax estimates", payload.tax_estimates),
    ]
    trade_rows: str = "".join(
        "<tr>"
        f"<td>{_html_text(trade.instrument)}</td>"
        f"<td>{_html_text(trade.side)}</td>"
        f"<td>{_html_text(trade.quantity)}</td>"
        f"<td>{_html_text(trade.estimated_notional)}</td>"
        f"<td>{_html_text(trade.tax_cost_known)}</td>"
        "</tr>"
        for trade in payload.proposed_trades
    )
    evidence_rows: str = "".join(
        "<tr>"
        f"<td>{_html_text(evidence.label)}</td>"
        f"<td>{_html_text(evidence.locator)}</td>"
        f"<td>{_html_text(evidence.source_item_id)}</td>"
        f"<td>{_html_text(evidence.status)}</td>"
        "</tr>"
        for evidence in review.evidence
    )
    conflicts: str = "".join(f"<li>{_html_text(claim)}</li>" for claim in review.conflicting_claims)
    rejected: str = "".join(
        f"<li>{_html_text(item.instrument)}: {_html_text('; '.join(item.reasons))}</li>"
        for item in payload.rejected_candidates
    )
    sections.extend(
        [
            (
                "<section><h2>Proposed trades</h2><table>"
                "<thead><tr><th>Instrument</th><th>Side</th><th>Quantity</th>"
                "<th>Notional</th><th>Tax known</th></tr></thead>"
                f"<tbody>{trade_rows}</tbody></table></section>"
            ),
            (
                "<section><h2>Evidence</h2><table>"
                "<thead><tr><th>Label</th><th>Locator</th><th>Source</th>"
                f"<th>Status</th></tr></thead><tbody>{evidence_rows}</tbody></table></section>"
            ),
            f"<section><h2>Conflicting claims</h2><ul>{conflicts}</ul></section>",
            f"<section><h2>Rejected candidates</h2><ul>{rejected}</ul></section>",
            f"<section><h2>Scenarios</h2>{_html_text(json.dumps(review.scenarios, sort_keys=True))}</section>",
            f"<section><h2>Sensitivities</h2>{_html_text(json.dumps(review.sensitivities, sort_keys=True))}</section>",
        ]
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{_html_text(review.title)}</title></head><body>"
        f"<h1>{_html_text(review.title)}</h1>"
        f"<p>Plan: <code>{_html_text(payload.plan_id)}</code></p>"
        f"<p>Plan hash: <code>{_html_text(review.plan.plan_hash)}</code></p>"
        f"<p>Approval: {_html_text(review.approval_state)}</p>"
        f"<p>Execution: {_html_text(review.execution_state)}</p>"
        f"{''.join(sections)}</body></html>"
    )


def write_report_bundle(review: PortfolioReview, report_directory: Path) -> tuple[Path, Path, Path]:
    """Write the three static report formats and return their paths."""
    if report_directory.exists() and not report_directory.is_dir():
        raise ReportRenderError("report path exists and is not a directory")
    report_directory.mkdir(parents=True, exist_ok=True)
    json_path: Path = report_directory / "portfolio-review.json"
    markdown_path: Path = report_directory / "portfolio-review.md"
    html_path: Path = report_directory / "portfolio-review.html"
    _ = json_path.write_text(render_json(review), encoding="utf-8")
    _ = markdown_path.write_text(render_markdown(review), encoding="utf-8")
    _ = html_path.write_text(render_html(review), encoding="utf-8")
    return json_path, markdown_path, html_path
