"""argocd-observability: a strictly read-only Argo CD SRE observer agent for CrewAI.

Building blocks for a larger SRE crew::

    from argocd_observability import (
        build_argocd_observability_agent, health_overview_task, diagnose_app_task,
        ArgoCDHealthReport, ObservabilitySettings,
    )
"""

from .agent import agent_context, build_argocd_observability_agent
from .context import ObservabilityContext, create_context, finalize_report
from .guard import ReadOnlyViolation
from .models import AppDiagnosis, ArgoCDHealthReport, Evidence
from .report import Scope, render_markdown
from .settings import ObservabilitySettings
from .tasks import diagnose_app_task, health_overview_task

__all__ = [
    "AppDiagnosis", "ArgoCDHealthReport", "Evidence", "ObservabilityContext", "ObservabilitySettings",
    "ReadOnlyViolation", "Scope", "agent_context", "build_argocd_observability_agent", "create_context",
    "diagnose_app_task", "finalize_report", "health_overview_task", "render_markdown",
]
