"""Pydantic models: admission input spec, run configuration, and machine-readable outputs.

Split of responsibility:
  * *Computed* models (AuditComputed / AdmissionComputed) are filled by deterministic code.
  * *Narrative* models (AuditNarrative / AdmissionNarrative) are the LLM's output_pydantic.
  * Final reports = computed + narrative, assembled in code (report.py). The LLM can
    neither change a number nor upgrade a verdict.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Severity = Literal["critical", "high", "medium", "low"]
Verdict = Literal["FITS", "FITS_WITH_CONDITIONS", "DOES_NOT_FIT"]
CheckStatus = Literal["PASS", "WARN", "FAIL", "NOT_VERIFIED", "SKIPPED"]
Confidence = Literal["high", "medium", "low"]
Concurrency = Literal["sequential", "per_namespace", "all"]


# =========================================================================== admission input
class HostPort(BaseModel):
    port: int
    container_port: int | None = None
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"


class ContainerSpec(BaseModel):
    name: str
    requests: dict[str, str] = Field(default_factory=dict, description='e.g. {"cpu": "500m", "memory": "1Gi"}')
    limits: dict[str, str] = Field(default_factory=dict)
    host_ports: list[HostPort] = Field(default_factory=list)


class InitContainerSpec(ContainerSpec):
    sidecar: bool = Field(False, description="restartPolicy: Always (native sidecar, KEP-753)")


class StrategySpec(BaseModel):
    type: Literal["RollingUpdate", "Recreate", "OnDelete"] = "RollingUpdate"
    max_surge: int | str | None = Field(None, description='int or percent string, e.g. 1 or "25%"')
    max_unavailable: int | str | None = None


class PVCSpec(BaseModel):
    name: str
    storage_class: str | None = None
    size: str = "10Gi"
    access_modes: list[Literal["ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod"]] = ["ReadWriteOnce"]
    per_replica: bool = Field(True, description="StatefulSet volumeClaimTemplate (one PVC per replica)")
    existing: bool = Field(False, description="claim already exists in the namespace")


class AdmissionSpec(BaseModel):
    """What to check before deploy. Either structured fields or `manifest_yaml`."""

    name: str
    namespace: str
    kind: Literal["Deployment", "StatefulSet", "DaemonSet"] = "Deployment"
    change_type: Literal["new", "resize", "scale"] = "new"
    replicas: int | None = Field(None, ge=0)
    labels: dict[str, str] = Field(default_factory=dict)
    containers: list[ContainerSpec] = Field(default_factory=list)
    init_containers: list[InitContainerSpec] = Field(default_factory=list)
    strategy: StrategySpec | None = None
    node_selector: dict[str, str] = Field(default_factory=dict)
    affinity: dict[str, Any] = Field(default_factory=dict, description="raw v1.Affinity")
    tolerations: list[dict[str, Any]] = Field(default_factory=list)
    topology_spread_constraints: list[dict[str, Any]] = Field(default_factory=list)
    pvcs: list[PVCSpec] = Field(default_factory=list)
    priority_class_name: str | None = None
    runtime_class_name: str | None = None
    host_network: bool = False
    manifest_yaml: str | None = Field(None, description="full manifest (Deployment/StatefulSet/DaemonSet [+ PVCs])")

    @model_validator(mode="after")
    def _need_something(self):
        if not self.manifest_yaml and self.change_type != "scale" and not self.containers:
            raise ValueError("containers (or manifest_yaml) are required unless change_type=scale")
        if self.change_type == "scale" and self.replicas is None:
            raise ValueError("replicas is required for change_type=scale")
        return self

    def to_engine(self) -> dict:
        d = self.model_dump(exclude_none=True)
        if self.labels == {}:
            d.pop("labels", None)
        if self.replicas is None:
            d.pop("replicas", None)
        return d


# =========================================================================== run config
class NodeGroup(BaseModel):
    name: str
    pool: str
    min: int = 0
    max: int
    current: int
    provisioning_minutes: int = 5


class AutoscalerConfig(BaseModel):
    enabled: bool = False
    kind: Literal["cluster-autoscaler", "karpenter", "none"] = "none"
    node_groups: list[NodeGroup] = Field(default_factory=list)


class ReferencePod(BaseModel):
    name: str
    requests: dict[str, str]


class RunConfig(BaseModel):
    mode: Literal["audit", "admission"]
    cluster_name: str = "cluster"
    snapshot_file: str | None = Field(None, description="offline mode: load this snapshot instead of the API")
    kube_context: str | None = None
    prometheus_url: str | None = None
    update_concurrency: Concurrency = "sequential"
    custom_update_groups: list[list[str]] | None = None
    require_n_minus_1: bool = True
    require_update_reserve: bool = True
    allow_preemption: bool = False
    strict_future_rollout: bool = True
    n_minus_1_max_nodes: int = 30
    n_minus_1_min_priority: int = Field(0, description="pods below this priority may stay pending after N-1")
    scoring: Literal["LeastAllocated", "MostAllocated"] = "LeastAllocated"
    reference_pods: list[ReferencePod] | None = None
    autoscaler: AutoscalerConfig = Field(default_factory=AutoscalerConfig)
    ipam: dict[str, Any] = Field(default_factory=lambda: {"mode": "podcidr", "reserved_per_cidr": 3})
    pool_label: str | None = None
    admission: AdmissionSpec | None = None

    def autoscaler_dict(self) -> dict | None:
        """Engine form of the autoscaler config (None = fixed node count)."""
        return self.autoscaler.model_dump() if self.autoscaler.enabled else None

    def reference_pod_dicts(self) -> list[dict] | None:
        return [r.model_dump() for r in self.reference_pods] if self.reference_pods else None

    def collector_kwargs(self) -> dict:
        """Arguments for tools.collector.collect_snapshot."""
        return {"cluster_name": self.cluster_name, "kube_context": self.kube_context,
                "prometheus_url": self.prometheus_url, "ipam": self.ipam, "pool_label": self.pool_label}

    def policy(self) -> dict:
        """Engine policy for engine.admission."""
        return {
            "update_concurrency": self.update_concurrency,
            "custom_update_groups": self.custom_update_groups,
            "require_n_minus_1": self.require_n_minus_1,
            "require_update_reserve": self.require_update_reserve,
            "allow_preemption": self.allow_preemption,
            "strict_future_rollout": self.strict_future_rollout,
            "autoscaler": self.autoscaler_dict(),
            "n_minus_1_max_nodes": self.n_minus_1_max_nodes,
            "n_minus_1_min_priority": self.n_minus_1_min_priority,
        }

    def audit_config(self) -> dict:
        """Engine config for engine.audit.run_audit."""
        return {
            "update_concurrency": self.update_concurrency,
            "custom_update_groups": self.custom_update_groups,
            "reference_pods": self.reference_pod_dicts(),
            "autoscaler": self.autoscaler_dict(),
            "n_minus_1_max_nodes": self.n_minus_1_max_nodes,
        }


# =========================================================================== outputs (computed)
class Risk(BaseModel):
    id: str
    severity: Severity
    category: str
    title: str
    evidence: list[str] = Field(description="paths into the computed JSON + the values found there")
    recommendation: str
    source: Literal["computed", "analyst"] = "computed"


class PoolSummary(BaseModel):
    pool: str
    reference_pod: str
    schedulable_nodes: int
    utilization_pct: dict[str, float]
    limiting_resource: str
    headroom_reference_pods: int
    fragmentation_pct: float


class NodeRow(BaseModel):
    node: str
    pool: str
    zone: str | None = None
    ready: bool
    schedulable: bool
    allocatable: dict[str, str]
    requested: dict[str, str]
    free: dict[str, str]
    utilization_pct: dict[str, float]
    most_used_resource: str | None = None
    pods: int
    besteffort_pods: int


class CheckResult(BaseModel):
    check: Literal["namespace_quota", "steady_state_placement", "app_rollout", "others_update_reserve", "n_minus_1_after"]
    status: CheckStatus
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class RemediationOption(BaseModel):
    option: Literal["strategy", "anti_affinity", "replicas", "pool", "add_nodes", "requests"]
    change: str
    verdict: Verdict
    conditions: list[str] = Field(default_factory=list)


class AuditComputed(BaseModel):
    cluster: str | None = None
    collected_at: str | None = None
    pools: list[PoolSummary]
    update_reserve: dict[str, Any]
    n_minus_1: dict[str, Any]
    nodes: list[NodeRow]
    risks: list[Risk]
    data_gaps: list[str] = Field(default_factory=list)


class AdmissionComputed(BaseModel):
    workload: str
    change_type: str
    replicas: int
    pod_effective_requests: dict[str, str]
    verdict: Verdict
    conditions: list[str]
    confidence: Confidence
    checks: list[CheckResult]
    not_verified: list[str] = Field(default_factory=list)
    remediation: list[RemediationOption] = Field(default_factory=list)


# =========================================================================== outputs (LLM narrative)
class Finding(BaseModel):
    statement: str = Field(description="one sentence, every number copied from tool output")
    evidence_refs: list[str] = Field(description="JSON paths from tool output, e.g. capacity.pools.general.utilization_pct.cpu")
    confidence: Confidence
    assumptions: list[str] = Field(default_factory=list)


class AuditNarrative(BaseModel):
    executive_summary: str = Field(description="5-8 sentences: limiting resource per pool, update reserve, N-1 outcome")
    key_findings: list[Finding]
    additional_risks: list[Risk] = Field(default_factory=list,
                                         description="risks NOT already in the computed list; source=analyst")
    prioritized_actions: list[str] = Field(description="ordered, each references risk ids, e.g. 'R01, R03: ...'")
    assumptions: list[str]
    open_questions: list[str] = Field(default_factory=list)


class CheckExplanation(BaseModel):
    check: str
    status: CheckStatus
    explanation: str = Field(description="why, in operator terms, citing node names and numbers from tool output")


class AdmissionNarrative(BaseModel):
    verdict: Verdict = Field(description="MUST equal the computed verdict")
    verdict_rationale: str
    checks: list[CheckExplanation]
    recommended_change: str | None = Field(None, description="the minimal change from remediation options, if any")
    conditions_for_operator: list[str] = Field(default_factory=list)
    confidence: Confidence
    assumptions: list[str]


class AuditReport(BaseModel):
    computed: AuditComputed
    narrative: AuditNarrative | None = None
    assumptions: list[str]


class AdmissionReport(BaseModel):
    computed: AdmissionComputed
    narrative: AdmissionNarrative | None = None
    assumptions: list[str]
