"""CrewAI tools: thin wrappers around the deterministic engine.

Every tool:
  * is read-only (the only cluster access is tools/collector.py: list/get);
  * reads the snapshot from the run directory (never from LLM arguments);
  * saves its full output to results/<tool>.json and returns a compact JSON view
    with `result_file` + `snapshot_digest`, so every number is traceable.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Literal, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .. import runtime
from ..engine import admission as adm
from ..engine.audit import run_audit
from ..engine.capacity import capacity_model
from ..engine.failure import n_minus_1
from ..engine.quantity import fmt_list, parse_resource_list
from ..engine.rollout import per_node_ds_headroom, reserve_analysis


def _compact(obj: Any, max_list: int = 8, max_str: int = 400, depth: int = 0) -> Any:
    if isinstance(obj, dict):
        return {k: _compact(v, max_list, max_str, depth + 1) for k, v in obj.items()
                if not k.endswith("_raw") and k not in ("placements", "events", "free_raw")}
    if isinstance(obj, list):
        out = [_compact(v, max_list, max_str, depth + 1) for v in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"... {len(obj) - max_list} more in result_file")
        return out
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + "..."
    return obj


def _reply(name: str, full: dict, view: dict | None = None) -> str:
    path = runtime.save_result(name, full)
    body = {"result_file": path, "snapshot_digest": runtime.snapshot_digest()}
    body.update(_compact(view if view is not None else full))
    return json.dumps(body, ensure_ascii=False, default=str)


def _error(exc: Exception) -> str:
    return json.dumps({"error": f"{exc.__class__.__name__}: {exc}",
                       "hint": "do not estimate numbers yourself; report the check as NOT_VERIFIED"})


def _tool_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn any exception into a JSON error the agent can report instead of guessing numbers."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
    return wrapper


def _pick(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: d[k] for k in keys}


# =========================================================================== 1. collector
class CollectInput(BaseModel):
    refresh: bool = Field(False, description="re-read the cluster even if a snapshot already exists in this run")


class CollectClusterSnapshotTool(BaseTool):
    name: str = "collect_cluster_snapshot"
    description: str = (
        "Read-only collection of nodes (allocatable, taints, labels, podCIDR, CSI limits), all pods with "
        "effective requests, controllers, PDBs, ResourceQuotas, LimitRanges, PriorityClasses, StorageClasses, "
        "PV/PVC and usage metrics into the run snapshot. Returns an inventory + data gaps. Call once per run first."
    )
    args_schema: Type[BaseModel] = CollectInput

    @_tool_errors
    def _run(self, refresh: bool = False) -> str:
        cfg = runtime.config()
        p = runtime.snapshot_path()
        if refresh or not p.exists():
            from .collector import collect_snapshot

            p.write_text(json.dumps(collect_snapshot(**cfg.collector_kwargs())))
        ctx = runtime.context()
        st = ctx.state
        meta = ctx.snapshot.get("meta") or {}
        pools = {pool: {"nodes": nodes, "taints": sorted({f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}"
                                                          for n in nodes for t in st.nodes[n].taints})}
                 for pool, nodes in ctx.pools().items()}
        qos: dict[str, int] = {}
        for pod in st.pods.values():
            qos[pod.qos] = qos.get(pod.qos, 0) + 1
        view = {
            "cluster": meta.get("cluster"),
            "kubernetes_version": meta.get("kubernetes_version"),
            "source": meta.get("sources"),
            "pools": pools,
            "nodes_not_ready_or_cordoned": [n for n, i in st.nodes.items() if not i.ready or i.unschedulable],
            "pods_bound": len(st.pods), "pods_pending": len(ctx.pending), "pods_by_qos": qos,
            "workloads": len(ctx.workloads), "pdbs": len(st.pdbs), "quotas": len(st.quota.quotas),
            "pod_ip_limit_source": sorted({i.ip_capacity_source for i in st.nodes.values()}),
            "usage_source": (ctx.snapshot.get("usage") or {}).get("source", "none"),
            "data_gaps": ctx.data_gaps,
        }
        return _reply("collect_cluster_snapshot", view)


# =========================================================================== 2. capacity model
class RefPodInput(BaseModel):
    name: str
    cpu: str = Field(description='e.g. "500m"')
    memory: str = Field(description='e.g. "1Gi"')


class CapacityInput(BaseModel):
    reference_pods: list[RefPodInput] | None = Field(
        None, description="reference pod sizes for headroom/fragmentation; default: run config or p50/p90 of workloads")


class CapacityModelTool(BaseTool):
    name: str = "capacity_model"
    description: str = (
        "Per-node/pool/cluster capacity by REQUESTS vs allocatable for cpu, memory, pods, pod IPs, "
        "ephemeral-storage, hugepages, extended resources and CSI attach limits; limiting resource; "
        "fragmentation (pods of reference size that fit per node vs perfect packing); hidden risks "
        "(BestEffort, usage>requests, limits overcommit, quotas, pending pods)."
    )
    args_schema: Type[BaseModel] = CapacityInput

    @_tool_errors
    def _run(self, reference_pods: list | None = None) -> str:
        ctx = runtime.context()
        refs = None
        if reference_pods:
            refs = [{"name": r["name"] if isinstance(r, dict) else r.name,
                     "requests": {"cpu": (r["cpu"] if isinstance(r, dict) else r.cpu),
                                  "memory": (r["memory"] if isinstance(r, dict) else r.memory)}}
                    for r in reference_pods]
        else:
            refs = runtime.config().reference_pod_dicts()
        cap = capacity_model(ctx, refs)
        view = {
            "reference_pods": cap["reference_pods"],
            "pools": cap["pools"],
            "nodes": [_pick(n, ("node", "pool", "zone", "free", "utilization_pct", "most_used_resource",
                                "fit_by_reference_pod", "limits_overcommit_pct", "besteffort_pods"))
                      for n in cap["nodes"]],
            "hidden_risks": {k: (v if not isinstance(v, list) else {"count": len(v), "items": v[:5]})
                             for k, v in cap["hidden_risks"].items()},
            "unverified": cap["unverified"],
            "method": cap["method"],
        }
        return _reply("capacity_model", cap, view)


# =========================================================================== 3. placement what-if
class PlacementInput(BaseModel):
    namespace: str = "default"
    replicas: int = Field(1, ge=1, le=500)
    requests: dict[str, str] = Field(description='per pod, e.g. {"cpu": "1500m", "memory": "2Gi"}')
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict[str, Any]] = Field(default_factory=list)
    one_per_node: bool = Field(False, description="required podAntiAffinity on kubernetes.io/hostname")
    one_per_zone: bool = Field(False, description="required podAntiAffinity on topology.kubernetes.io/zone")
    priority_class_name: str | None = None
    allow_preemption: bool = False


class PlacementSimulatorTool(BaseTool):
    name: str = "simulate_placement"
    description: str = (
        "What-if: place N pods of a given size with the scheduler filters (taints, selectors, anti-affinity, "
        "all resources) node by node, without changing the cluster. Returns per-node placement, kubectl-style "
        "reasons for the replicas that do not fit, and what a naive cluster-wide sum would have claimed."
    )
    args_schema: Type[BaseModel] = PlacementInput

    @_tool_errors
    def _run(self, namespace: str = "default", replicas: int = 1, requests: dict | None = None,
             node_selector: dict | None = None, tolerations: list | None = None, one_per_node: bool = False,
             one_per_zone: bool = False, priority_class_name: str | None = None, allow_preemption: bool = False) -> str:
        ctx = runtime.context()
        st = ctx.state.clone()
        labels = {"capacity-probe": "probe"}
        anti = []
        if one_per_node:
            anti.append({"labelSelector": {"matchLabels": labels}, "topologyKey": "kubernetes.io/hostname"})
        if one_per_zone:
            anti.append({"labelSelector": {"matchLabels": labels}, "topologyKey": "topology.kubernetes.io/zone"})
        spec = {"containers": [{"name": "c", "resources": {"requests": requests or {}}}],
                "nodeSelector": node_selector or {}, "tolerations": tolerations or [],
                "priorityClassName": priority_class_name}
        if anti:
            spec["affinity"] = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": anti}}
        placed, failed, per_node = [], [], {}
        for i in range(replicas):
            pod = ctx.make_pod({"name": f"probe-{i}", "namespace": namespace, "labels": labels,
                                "ownerReferences": [{"kind": "ReplicaSet", "name": "probe", "controller": True}]}, spec)
            pod.workload_key = "probe"
            pl = st.schedule(pod, allow_preemption=allow_preemption)
            if pl.ok:
                if pl.victims:
                    st.place_with_preemption(pod, pl)
                else:
                    st.add_pod(pod, pl.node)
                per_node[pl.node] = per_node.get(pl.node, 0) + 1
                placed.append({"pod": pod.key, "node": pl.node, "victims": pl.victims})
            else:
                failed.append({"pod": pod.key, "reason": pl.summary()})
        req = parse_resource_list(requests or {})
        free_sum: dict[str, int] = {}
        for n in ctx.state.nodes:
            for r, v in ctx.state.free(n).items():
                free_sum[r] = free_sum.get(r, 0) + max(0, v)
        naive = all(free_sum.get(r, 0) >= v * replicas for r, v in req.items())
        full = {"requested_replicas": replicas, "placed": len(placed), "per_node": per_node,
                "failed": failed, "placements": placed,
                "naive_cluster_sum_says_fits": naive,
                "cluster_free_sum": fmt_list({r: free_sum.get(r, 0) for r in req}),
                "unverified": ctx.state.unverified_resources(pod)}
        return _reply("simulate_placement", full)


# =========================================================================== 4. rollout reserve
class ReserveInput(BaseModel):
    concurrency_model: Literal["sequential", "per_namespace", "all"] | None = Field(
        None, description="default from run config")
    workloads: list[str] | None = Field(None, description='limit to keys like "Deployment/shop/api"')


class RolloutReserveTool(BaseTool):
    name: str = "rollout_reserve"
    description: str = (
        "Emulates Deployment/StatefulSet/DaemonSet rolling updates on the real node layout (maxSurge rounds up, "
        "maxUnavailable down; quota charged for surge/pending pods) for every workload under the update "
        "concurrency model. Returns the analytic reserve (ceil(maxSurge x replicas) x requests), the simulated "
        "peak, infeasible update groups with the blocking reason, static deadlocks and DaemonSet surge gaps."
    )
    args_schema: Type[BaseModel] = ReserveInput

    @_tool_errors
    def _run(self, concurrency_model: str | None = None, workloads: list | None = None) -> str:
        ctx = runtime.context()
        cfg = runtime.config()
        model = concurrency_model or cfg.update_concurrency
        res = reserve_analysis(ctx, ctx.state, model, cfg.custom_update_groups, workloads)
        res["daemonset_surge_headroom_gaps"] = per_node_ds_headroom(ctx.state, ctx)
        view = {k: v for k, v in res.items() if k != "per_workload"}
        view["per_workload"] = [_pick(w, ("workload", "status", "max_surge", "max_unavailable",
                                          "analytic_reserve", "stuck_reason", "used_unavailability"))
                                for w in res["per_workload"] if w["status"] != "COMPLETED" or w["used_unavailability"]]
        view["completed_workloads"] = sum(1 for w in res["per_workload"] if w["status"] == "COMPLETED")
        return _reply(f"rollout_reserve_{model}", res, view)


# =========================================================================== 5. N-1
class FailureInput(BaseModel):
    nodes: list[str] | None = Field(None, description="nodes to fail; default: all (<= max) or per-pool sample")


class NodeFailureTool(BaseTool):
    name: str = "simulate_node_failure"
    description: str = (
        "N-1: removes a node and re-schedules its pods on the rest with all filters. DaemonSet pods are not "
        "rescheduled, bare/static pods are lost, local-PV pods cannot move; preemption of lower-priority pods "
        "is reported separately; PDB breaches and drain blockers; autoscaler recovery if configured."
    )
    args_schema: Type[BaseModel] = FailureInput

    @_tool_errors
    def _run(self, nodes: list | None = None) -> str:
        ctx = runtime.context()
        cfg = runtime.config()
        res = n_minus_1(ctx, ctx.state, cfg.autoscaler_dict(), cfg.n_minus_1_max_nodes, nodes)
        view = {k: v for k, v in res.items() if k != "scenarios"}
        view["scenarios"] = [_pick(s, ("node", "pool", "verdict", "selected_because", "pending_after_failure",
                                       "rescheduled_with_preemption", "lost_pods", "unmovable_local_pv_pods",
                                       "pdb_below_desired_after_failure", "pdb_blocking_voluntary_drain",
                                       "autoscaler"))
                             for s in res["scenarios"]]
        return _reply("simulate_node_failure", res, view)


# =========================================================================== 6. admission
class AdmissionInput(BaseModel):
    note: str | None = Field(None, description="optional; the spec is read from the run config, not from here")


def _admission_spec(cfg) -> dict:
    if cfg.admission is None:
        raise ValueError("run config has no admission spec")
    return cfg.admission.to_engine()


class AdmissionSimulatorTool(BaseTool):
    name: str = "simulate_admission"
    description: str = (
        "Runs the five admission checks for the change in the run config: namespace_quota (+LimitRange), "
        "steady_state_placement, app_rollout, others_update_reserve, n_minus_1_after. Returns the verdict "
        "computed by code (FITS / FITS_WITH_CONDITIONS / DOES_NOT_FIT) with per-check evidence."
    )
    args_schema: Type[BaseModel] = AdmissionInput

    @_tool_errors
    def _run(self, note: str | None = None) -> str:
        cfg = runtime.config()
        res = adm.simulate_admission(runtime.context(), _admission_spec(cfg), cfg.policy())
        return _reply("simulate_admission", res, {k: v for k, v in res.items() if k != "policy"})


class RemediationSearchTool(BaseTool):
    name: str = "search_remediations"
    description: str = (
        "When the verdict is not FITS: deterministically tries rollout strategy, required->preferred "
        "anti-affinity, fewer replicas, another node pool, +N nodes of a pool's type, smaller requests; each "
        "candidate is re-run through all five checks. Returns the options that fit, smallest change first."
    )
    args_schema: Type[BaseModel] = AdmissionInput

    @_tool_errors
    def _run(self, note: str | None = None) -> str:
        cfg = runtime.config()
        res = adm.search_remediations(runtime.context(), _admission_spec(cfg), cfg.policy())
        return _reply("search_remediations", res)


class AuditRiskTool(BaseTool):
    name: str = "computed_risk_register"
    description: str = (
        "Builds the deterministic risk register (severity decided by rules in code) from the latest capacity, "
        "rollout-reserve and N-1 results of this run. Call after the three analyses."
    )
    args_schema: Type[BaseModel] = AdmissionInput

    @_tool_errors
    def _run(self, note: str | None = None) -> str:
        res = run_audit(runtime.context(), runtime.config().audit_config())
        return _reply("audit", res, {"summary": res["summary"], "risks": res["risks"]})


__all__ = [
    "CollectClusterSnapshotTool", "CapacityModelTool", "PlacementSimulatorTool", "RolloutReserveTool",
    "NodeFailureTool", "AdmissionSimulatorTool", "RemediationSearchTool", "AuditRiskTool",
]
