"""Scope, guardrail, evidence verification and Markdown rendering."""

# no `from __future__ import annotations`: CrewAI inspects the guardrail return annotation at runtime

import json
import re
from typing import Any, Callable, Tuple

from pydantic import BaseModel, Field

from .models import ArgoCDHealthReport


class Scope(BaseModel):
    apps: list[str] = Field(default_factory=list)
    project: str | None = None
    all: bool = False

    def describe(self) -> str:
        if self.apps:
            return ("app " if len(self.apps) == 1 else "apps ") + ", ".join(self.apps)
        if self.project:
            return f"all applications in project {self.project}"
        return "all applications"

    def list_hint(self) -> str:
        if self.apps:
            return "with names=" + json.dumps(self.apps)
        if self.project:
            return f'with project="{self.project}"'
        return "without filters"


# --------------------------------------------------------------------------- evidence verification
def _norm(s: str) -> str:
    s = s.replace('\\"', '"').replace("\\n", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def unverified_evidence(report: ArgoCDHealthReport, outputs: list[str], probe_len: int = 40) -> list[str]:
    """Evidence whose detail does not appear (verbatim, whitespace-normalized) in any tool output."""
    hay = _norm("\n".join(outputs))
    bad = []
    for app in report.apps:
        for ev in app.evidence:
            d = _norm(ev.detail)
            # compare a probe from the middle of the quote: tolerant to the LLM trimming the ends
            probe = d if len(d) <= probe_len else d[(len(d) - probe_len) // 2:(len(d) + probe_len) // 2]
            if probe and probe not in hay:
                bad.append(f"{app.app_name}: {ev.detail[:80]}")
    return bad


# --------------------------------------------------------------------------- guardrail
def make_guardrail(ctx: Any | None = None, expect_apps: list[str] | None = None) -> Callable[[Any], Tuple[bool, Any]]:
    """CrewAI task guardrail: structural checks only (never blocks on fuzzy matching)."""

    def guardrail(output: Any) -> Tuple[bool, Any]:
        rep = getattr(output, "pydantic", None)
        if not isinstance(rep, ArgoCDHealthReport):
            return False, "Output must be valid JSON matching the ArgoCDHealthReport schema."
        problems = []
        for a in rep.apps:
            if a.verdict != "healthy":
                if not a.probable_root_cause:
                    problems.append(f"{a.app_name}: probable_root_cause is required for verdict {a.verdict}")
                if not [e for e in a.evidence if e.detail.strip()]:
                    problems.append(f"{a.app_name}: at least one evidence item quoting tool output is required")
        if expect_apps:
            names = {a.app_name for a in rep.apps}
            missing = [n for n in expect_apps if n not in names]
            if missing:
                problems.append(f"missing diagnosis for: {missing}")
        if ctx is not None and ctx.ledger.seen_apps:
            unknown = [a.app_name for a in rep.apps if a.app_name not in ctx.ledger.seen_apps]
            if unknown:
                problems.append(f"apps not returned by any tool (do not invent apps): {unknown}")
        if problems:
            return False, "Fix the report: " + "; ".join(problems)
        return True, output

    return guardrail


# --------------------------------------------------------------------------- markdown
_ICON = {"healthy": "OK", "degraded": "DEGRADED", "out_of_sync": "OUT OF SYNC", "progressing": "PROGRESSING",
         "missing": "MISSING", "suspended": "SUSPENDED", "unknown": "UNKNOWN"}


def render_markdown(rep: ArgoCDHealthReport) -> str:
    lines = [f"# Argo CD health report - {rep.scope}", "",
             f"_generated {rep.generated_at} - auth {rep.auth_mode} - read-only_", "", rep.summary, ""]
    lines += ["| App | Sync | Health | Verdict | Root cause | Confidence |", "|---|---|---|---|---|---|"]
    for a in rep.apps:
        rc = (a.probable_root_cause or "-").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {a.app_name} | {a.sync_status} | {a.health_status} | {_ICON.get(a.verdict, a.verdict)} | "
                     f"{rc[:140]} | {a.confidence if a.verdict != 'healthy' else '-'} |")
    for a in rep.unhealthy:
        lines += ["", f"## {a.app_name} - {_ICON.get(a.verdict, a.verdict)} ({a.root_cause_category})", ""]
        meta = [f"project `{a.project}`" if a.project else None,
                f"namespace `{a.destination_namespace}`" if a.destination_namespace else None,
                f"revision `{a.revision[:12]}`" if a.revision else None,
                f"last operation `{a.operation_phase}`" if a.operation_phase else None]
        if any(meta):
            lines.append(" - ".join(m for m in meta if m))
            lines.append("")
        lines += [f"**Probable root cause** (confidence {a.confidence}): {a.probable_root_cause}", ""]
        if a.failing_resources:
            lines += ["**Failing resources:** " + ", ".join(f"`{r}`" for r in a.failing_resources), ""]
        if a.evidence:
            lines.append("**Evidence**")
            lines.append("")
            for e in a.evidence:
                ts = f" @ {e.observed_at}" if e.observed_at else ""
                detail = e.detail.replace("\n", " ").strip()
                lines.append(f"- [{e.source}] {e.reference}{ts}: `{detail[:400]}`")
            lines.append("")
        if a.suggested_next_steps:
            lines.append("**Suggested next steps** (not executed)")
            lines.append("")
            lines += [f"{i}. {s}" for i, s in enumerate(a.suggested_next_steps, 1)]
    lines += ["", "## Data sources", ""] + [f"- {s}" for s in rep.data_sources_used or ["(none recorded)"]]
    lines += ["", "## Limitations", ""] + [f"- {s}" for s in rep.limitations or ["none"]]
    return "\n".join(lines) + "\n"
