"""Small standalone crew: one agent, one task (diagnose one app, or a health overview)."""

from __future__ import annotations

from crewai import Crew, Process

from .agent import agent_context, build_argocd_observability_agent
from .context import ObservabilityContext, finalize_report
from .models import ArgoCDHealthReport
from .report import Scope, unverified_evidence
from .settings import ObservabilitySettings
from .tasks import diagnose_app_task, health_overview_task


def build_observability_crew(scope: Scope, settings: ObservabilitySettings | None = None, *,
                             context: ObservabilityContext | None = None, verbose: bool = False) -> Crew:
    agent = build_argocd_observability_agent(settings, context=context, verbose=verbose)
    task = (diagnose_app_task(agent, scope.apps[0]) if len(scope.apps) == 1
            else health_overview_task(agent, scope))
    return Crew(name="argocd-observability", agents=[agent], tasks=[task], process=Process.sequential,
                verbose=verbose)


def run_observability(scope: Scope, settings: ObservabilitySettings | None = None, *,
                      context: ObservabilityContext | None = None, verbose: bool = False) -> ArgoCDHealthReport:
    crew = build_observability_crew(scope, settings, context=context, verbose=verbose)
    result = crew.kickoff()
    report = result.pydantic
    if not isinstance(report, ArgoCDHealthReport):
        report = ArgoCDHealthReport.model_validate_json(result.raw)
    ctx = agent_context(crew.agents[0])
    return finalize_with_checks(report, ctx, scope)


def finalize_with_checks(report: ArgoCDHealthReport, ctx: ObservabilityContext, scope: Scope) -> ArgoCDHealthReport:
    report = finalize_report(report, ctx, scope.describe())
    bad = unverified_evidence(report, ctx.ledger.outputs)
    if bad:
        report.limitations.append(
            f"{len(bad)} evidence item(s) could not be matched verbatim to tool output (paraphrased?): "
            + "; ".join(bad[:5]))
    return report
