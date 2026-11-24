"""Structured output consumed by downstream SRE agents (triage, remediation, reporting)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EvidenceSource = Literal[
    "argocd_app_status", "resource_tree", "live_resource", "k8s_event", "container_log", "metric",
    "access_check",
]
AppVerdict = Literal["healthy", "degraded", "out_of_sync", "progressing", "missing", "suspended", "unknown"]
RootCauseCategory = Literal[
    "none", "image_pull", "crash_loop", "container_config", "scheduling", "probe_failure", "oom_killed",
    "sync_failed", "comparison_error", "never_synced", "drift", "resource_degraded", "progressing_timeout",
    "argocd_component", "unknown",
]
Confidence = Literal["high", "medium", "low"]


class Evidence(BaseModel):
    source: EvidenceSource = Field(description="Which data source the evidence came from.")
    reference: str = Field(description="What was read, e.g. 'Pod demo/web-7f9 events' or 'argocd_app_info{name=\"x\"}'.")
    detail: str = Field(description="The exact log line, event message, status field or metric value (verbatim).")
    observed_at: str | None = Field(None, description="Timestamp from the source, if any.")


class AppDiagnosis(BaseModel):
    app_name: str
    project: str | None = None
    destination_namespace: str | None = None
    sync_status: str = Field(description="Argo CD sync status: Synced / OutOfSync / Unknown.")
    health_status: str = Field(description="Argo CD health: Healthy / Degraded / Progressing / Missing / Suspended / Unknown.")
    operation_phase: str | None = Field(None, description="Last operation phase (Succeeded/Failed/Error/Running).")
    revision: str | None = Field(None, description="Last synced or compared revision.")
    verdict: AppVerdict
    failing_resources: list[str] = Field(default_factory=list, description="'Kind/namespace/name' of failing resources.")
    probable_root_cause: str | None = Field(None, description="One or two sentences; null if healthy.")
    root_cause_category: RootCauseCategory = "none"
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = "low"
    suggested_next_steps: list[str] = Field(
        default_factory=list,
        description="Suggestions for a human or a remediation agent. NEVER executed by this agent.")


class ArgoCDHealthReport(BaseModel):
    scope: str = Field(description="What was inspected, e.g. 'app hello-test' or 'all applications'.")
    generated_at: str = ""
    auth_mode: str = Field("", description="kube or argocd (filled in by code).")
    summary: str = Field(description="Two to five sentences for humans.")
    apps: list[AppDiagnosis] = Field(default_factory=list)
    data_sources_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list,
                                   description="Unavailable data sources and anything that could not be verified.")

    @property
    def unhealthy(self) -> list[AppDiagnosis]:
        return [a for a in self.apps if a.verdict != "healthy"]
