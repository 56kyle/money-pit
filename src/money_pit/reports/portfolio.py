"""Module containing pure static portfolio-review renderers."""

from __future__ import annotations

import hashlib
import html
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Self

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import HttpUrl
from pydantic import JsonValue
from pydantic import model_validator

from money_pit.schemas.portfolio_plan import PortfolioPlan  # noqa: TC001


if TYPE_CHECKING:
    from collections.abc import Mapping


class ReportReadError(Exception):
    """Raised when a typed portfolio-review input cannot be read."""


class ReportRenderError(Exception):
    """Raised when a static report cannot be rendered safely."""


class ReportEvidence(BaseModel):
    """One safe web locator and optional local evidence thumbnail."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    web_url: HttpUrl | None = None
    local_thumbnail: Path | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1)
    source_item_id: str = Field(min_length=1)
    status: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        """Require a link or a safe relative image path."""
        has_locator = self.web_url is not None or self.local_thumbnail is not None
        if has_locator == (self.unavailable_reason is not None):
            raise ValueError("report evidence requires a URL or thumbnail, or an unavailable reason")
        if self.local_thumbnail is not None:
            path = self.local_thumbnail
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
            ):
                raise ValueError("local thumbnail must be a safe relative image path")
        return self


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
    source_authority_ratio_by_claim_category: dict[str, float]
    evidence: tuple[ReportEvidence, ...]
    scenarios: tuple[dict[str, JsonValue], ...]
    decision_diagnostics: tuple[dict[str, JsonValue], ...]
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
    lines.extend(
        _markdown_mapping("Source authority ratio by claim category", review.source_authority_ratio_by_claim_category)
    )
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
            locator=_markdown_evidence_locator(evidence),
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
    lines.extend(_markdown_mapping("Tax estimate", payload.tax_estimate.model_dump(mode="json")))
    lines.extend(["", "## Scenarios", ""])
    lines.extend(f"- {_markdown_text(json.dumps(scenario, sort_keys=True))}" for scenario in review.scenarios)
    lines.extend(["", "## Decision diagnostics", ""])
    lines.extend(f"- {_markdown_text(json.dumps(item, sort_keys=True))}" for item in review.decision_diagnostics)
    lines.append("")
    return "\n".join(lines)


def _html_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _markdown_evidence_locator(evidence: ReportEvidence) -> str:
    parts: list[str] = []
    if evidence.web_url is not None:
        parts.append(f"[source]({_markdown_url(str(evidence.web_url))})")
    if evidence.local_thumbnail is not None:
        parts.append(f"![{_markdown_text(evidence.label)}]({_markdown_url(evidence.local_thumbnail.as_posix())})")
    if evidence.unavailable_reason is not None:
        parts.append(f"Unavailable: {_markdown_text(evidence.unavailable_reason)}")
    return " ".join(parts)


def _markdown_url(value: str) -> str:
    return value.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _html_evidence_locator(evidence: ReportEvidence) -> str:
    parts: list[str] = []
    if evidence.web_url is not None:
        parts.append(f'<a href="{_html_text(evidence.web_url)}" rel="noreferrer">source</a>')
    if evidence.local_thumbnail is not None:
        parts.append(
            f'<img src="{_html_text(evidence.local_thumbnail.as_posix())}" '
            + f'alt="{_html_text(evidence.label)}" loading="lazy">'
        )
    if evidence.unavailable_reason is not None:
        parts.append(f"Unavailable: {_html_text(evidence.unavailable_reason)}")
    return " ".join(parts)


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
        _html_mapping("Source authority ratio by claim category", review.source_authority_ratio_by_claim_category),
        _html_mapping(
            "Plan estimates",
            {
                "turnover": payload.turnover_estimate,
                "expected risk change": payload.expected_risk_change,
                "expected return change": payload.expected_return_change,
            },
        ),
        _html_mapping("Tax estimate", payload.tax_estimate.model_dump(mode="json")),
    ]
    trade_rows: str = "".join(
        "<tr>"
        + f"<td>{_html_text(trade.instrument)}</td>"
        + f"<td>{_html_text(trade.side)}</td>"
        + f"<td>{_html_text(trade.quantity)}</td>"
        + f"<td>{_html_text(trade.estimated_notional)}</td>"
        + f"<td>{_html_text(trade.tax_cost_known)}</td>"
        + "</tr>"
        for trade in payload.proposed_trades
    )
    evidence_rows: str = "".join(
        "<tr>"
        + f"<td>{_html_text(evidence.label)}</td>"
        + f"<td>{_html_evidence_locator(evidence)}</td>"
        + f"<td>{_html_text(evidence.source_item_id)}</td>"
        + f"<td>{_html_text(evidence.status)}</td>"
        + "</tr>"
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
            f"<section><h2>Decision diagnostics</h2>{_html_text(json.dumps(review.decision_diagnostics, sort_keys=True))}</section>",
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


def write_report_bundle(
    review: PortfolioReview,
    report_directory: Path,
    *,
    thumbnail_sources: Mapping[Path, Path] | None = None,
) -> tuple[Path, Path, Path]:
    """Atomically install one immutable three-format report bundle."""
    if report_directory.exists() and not report_directory.is_dir():
        raise ReportRenderError("report path exists and is not a directory")
    contents: dict[Path, bytes] = {
        Path("portfolio-review.json"): render_json(review).encode(),
        Path("portfolio-review.md"): render_markdown(review).encode(),
        Path("portfolio-review.html"): render_html(review).encode(),
    }
    sources = {} if thumbnail_sources is None else dict(thumbnail_sources)
    required = {item.local_thumbnail for item in review.evidence if item.local_thumbnail is not None}
    if set(sources) != required:
        raise ReportRenderError("report thumbnail sources must exactly cover local evidence thumbnails")
    for relative, source in sources.items():
        payload = source.read_bytes()
        if len(payload) > 10_000_000:
            raise ReportRenderError("report thumbnail exceeds the bounded size")
        if hashlib.sha256(payload).hexdigest() != relative.stem:
            raise ReportRenderError("report thumbnail content does not match its asset identity")
        contents[relative] = payload
    if report_directory.is_dir():
        _require_exact_report_contents(report_directory, contents)
        return (
            report_directory / "portfolio-review.json",
            report_directory / "portfolio-review.md",
            report_directory / "portfolio-review.html",
        )
    report_directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{report_directory.name}-", dir=report_directory.parent) as temporary:
        staged = Path(temporary)
        for name, content in contents.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(content)
        try:
            _ = staged.replace(report_directory)
        except FileExistsError:
            _require_exact_report_contents(report_directory, contents)
    return (
        report_directory / "portfolio-review.json",
        report_directory / "portfolio-review.md",
        report_directory / "portfolio-review.html",
    )


def _require_exact_report_contents(report_directory: Path, expected: dict[Path, bytes]) -> None:
    actual_names = {item.relative_to(report_directory) for item in report_directory.rglob("*") if item.is_file()}
    if actual_names != set(expected):
        raise ReportRenderError("immutable report bundle collides with different files")
    if any((report_directory / name).read_bytes() != content for name, content in expected.items()):
        raise ReportRenderError("immutable report bundle collides with different content")
