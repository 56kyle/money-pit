"""Module containing inert static outcome-evaluation renderers."""

import html
import json

from money_pit.portfolio.outcomes import InvestmentOutcomeMetrics


def render_outcome_json(metrics: InvestmentOutcomeMetrics) -> str:
    """Render deterministic outcome metrics as JSON."""
    return metrics.model_dump_json(indent=2)


def render_outcome_markdown(metrics: InvestmentOutcomeMetrics) -> str:
    """Render a compact inert Markdown outcome report."""
    values: dict[str, object] = metrics.model_dump(mode="json")
    lines: list[str] = [
        "# Investment outcome evaluation",
        "",
        f"Plan: `{_markdown_text(metrics.plan_id)}`",
        "",
        f"Plan hash: `{metrics.plan_hash}`",
        "",
        f"Thesis revision: `{_markdown_text(metrics.thesis_revision_id)}`",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {_markdown_text(name)} | {_markdown_text(json.dumps(value, sort_keys=True))} |"
        for name, value in sorted(values.items())
        if name not in {"plan_id", "plan_hash", "thesis_revision_id"}
    )
    lines.append("")
    return "\n".join(lines)


def render_outcome_html(metrics: InvestmentOutcomeMetrics) -> str:
    """Render a self-contained inert HTML outcome report."""
    values: dict[str, object] = metrics.model_dump(mode="json")
    rows: str = "".join(
        f"<tr><th>{html.escape(name, quote=True)}</th>"
        + f"<td>{html.escape(json.dumps(value, sort_keys=True), quote=True)}</td></tr>"
        for name, value in sorted(values.items())
        if name not in {"plan_id", "plan_hash", "thesis_revision_id"}
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        + "<title>Investment outcome evaluation</title></head><body>"
        + "<h1>Investment outcome evaluation</h1>"
        + f"<p>Plan: <code>{html.escape(metrics.plan_id, quote=True)}</code></p>"
        + f"<p>Plan hash: <code>{metrics.plan_hash}</code></p>"
        + f"<p>Thesis revision: <code>{html.escape(metrics.thesis_revision_id, quote=True)}</code></p>"
        + f"<table>{rows}</table></body></html>"
    )


def _markdown_text(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("<", "\\<")
        .replace(">", "\\>")
    )
