"""Audit assembly + deterministic risk rules (severity is decided here, not by the LLM)."""

from __future__ import annotations

from .capacity import capacity_model
from .failure import n_minus_1
from .rollout import per_node_ds_headroom, reserve_analysis

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def run_audit(ctx, config: dict) -> dict:
    cap = capacity_model(ctx, config.get("reference_pods"))
    reserve = reserve_analysis(ctx, ctx.state, config.get("update_concurrency", "sequential"),
                               config.get("custom_update_groups"))
    reserve["daemonset_surge_headroom_gaps"] = per_node_ds_headroom(ctx.state, ctx)
    n1 = n_minus_1(ctx, ctx.state, config.get("autoscaler"), config.get("n_minus_1_max_nodes", 30))
    risks = derive_risks(cap, reserve, n1, ctx)
    return {
        "cluster": (ctx.snapshot.get("meta") or {}).get("cluster"),
        "collected_at": (ctx.snapshot.get("meta") or {}).get("collected_at"),
        "summary": summarize(cap, reserve, n1),
        "capacity": cap,
        "update_reserve": reserve,
        "n_minus_1": n1,
        "risks": risks,
        "data_gaps": ctx.data_gaps,
        "config": {k: v for k, v in config.items() if k != "autoscaler"} | {"autoscaler": bool(config.get("autoscaler"))},
    }


def summarize(cap: dict, reserve: dict, n1: dict) -> dict:
    from .quantity import parse_resource_list
    cap_refs = {r["name"]: parse_resource_list(r["requests"]) for r in cap["reference_pods"]}
    pools = []
    for name, p in cap["pools"].items():
        refs = p["reference_pods"]
        # most conservative view: the largest reference pod (by cpu, then memory)
        main_name = max(refs, key=lambda k: (cap_refs[k]["cpu"], cap_refs[k]["memory"]))
        main = refs[main_name]
        pools.append({
            "pool": name,
            "reference_pod": f"{main_name} {main['reference_requests']}",
            "schedulable_nodes": p["schedulable_nodes"],
            "utilization_pct": p["utilization_pct"],
            "limiting_resource": main["limiting_resource"],
            "headroom_reference_pods": main["fits_per_node_sum"],
            "fragmentation_pct": main["fragmentation_pct"],
        })
    return {
        "pools": pools,
        "update_reserve": {
            "concurrency_model": reserve["concurrency_model"],
            "required_analytic": reserve["required_reserve_analytic"],
            "required_simulated_peak": reserve["required_reserve_simulated_peak"],
            "dominated_by": reserve.get("dominated_by"),
            "fits_topologically": not reserve["groups_infeasible"],
            "infeasible_groups": len(reserve["groups_infeasible"]),
            "static_deadlocks": len(reserve["static_deadlocks"]),
        },
        "n_minus_1": {"overall": n1["overall"], "worst_node": n1["worst_node"], "coverage": n1["coverage"]},
    }


def derive_risks(cap: dict, reserve: dict, n1: dict, ctx) -> list[dict]:
    risks: list[dict] = []

    def add(sev, cat, title, evidence, rec):
        risks.append({"id": f"R{len(risks) + 1:02d}", "severity": sev, "category": cat, "title": title,
                      "evidence": evidence, "recommendation": rec, "source": "computed"})

    # node-failure risks grouped by the same cause (same set of stranded workloads) across nodes
    degraded: dict[tuple, list[dict]] = {}
    for s in n1["scenarios"]:
        if s["verdict"] == "DEGRADED":
            key = tuple(sorted({p["workload"] or p["pod"] for p in s["pending_after_failure"]}))
            degraded.setdefault(key, []).append(s)
    for wls, scen in degraded.items():
        nodes = [s["node"] for s in scen]
        first = scen[0]
        pend = first["pending_after_failure"]
        asc = first.get("autoscaler") or {}
        eta = (f"; autoscaler needs {asc.get('nodes_needed')} node(s), ~{asc.get('provisioning_minutes')} min"
               if asc.get("nodes_needed") else "")
        add("critical", "node_failure",
            f"Loss of any of [{', '.join(nodes)}] leaves pods of {', '.join(wls)} unschedulable{eta}",
            [f"n_minus_1.scenarios[{n}].pending_after_failure" for n in nodes[:3]]
            + [f"{p['pod']}: {p['reason']}" for p in pend[:2]],
            f"Add N+1 headroom in pool {first['pool']} for these pods, or relax their hard placement constraints.")
    drain: dict[str, list[str]] = {}
    for s in n1["scenarios"]:
        if s["verdict"] == "SURVIVES_WITH_PREEMPTION":
            add("high", "node_failure",
                f"Loss of {s['node']} is absorbed only by preempting {len(s['displaced_by_preemption'])} lower-priority pod(s)",
                [f"n_minus_1.scenarios[{s['node']}].displaced_by_preemption: {', '.join(s['displaced_by_preemption'][:5])}"],
                "Confirm these workloads may be evicted on node failure, or add headroom.")
        if s["unmovable_local_pv_pods"]:
            add("medium", "node_failure", f"{len(s['unmovable_local_pv_pods'])} pod(s) on {s['node']} use local PVs and cannot move",
                [f"n_minus_1.scenarios[{s['node']}].unmovable_local_pv_pods: {s['unmovable_local_pv_pods'][:5]}"],
                "Ensure application-level replication for local-PV workloads (data stays on the failed node).")
        if s["lost_pods"]:
            add("low", "node_failure", f"{len(s['lost_pods'])} pod(s) on {s['node']} have no controller and are lost",
                [f"n_minus_1.scenarios[{s['node']}].lost_pods: {s['lost_pods'][:5]}"],
                "Run these pods under a controller (Deployment/StatefulSet).")
        for b in s["pdb_blocking_voluntary_drain"]:
            drain.setdefault(b["pdb"], []).append(f"{s['node']} (allowed={b['disruptions_allowed']}, on node={b['pods_on_node']})")
    for pdb, where in drain.items():
        add("medium", "maintenance", f"PDB {pdb} blocks draining {len(where)} node(s)",
            [f"n_minus_1.scenarios[*].pdb_blocking_voluntary_drain: {', '.join(where[:5])}"],
            "Increase replicas or relax minAvailable so node maintenance can proceed.")

    for g in reserve["groups_infeasible"]:
        for st in g["stuck"]:
            add("high", "rolling_update", f"Rolling update of {st['workload']} cannot complete",
                [f"update_reserve.groups_infeasible[{','.join(g['group'])}]: {st['reason']}"],
                "Free capacity where the surge pod can land, set maxUnavailable>=1, or relax required anti-affinity / spread.")
    for d in reserve["static_deadlocks"]:
        add("high", "rolling_update", "Rolling-update deadlock by construction", [f"update_reserve.static_deadlocks: {d}"],
            "Set maxUnavailable>=1 (maxSurge=0) or add an eligible node/domain.")
    for g in reserve["groups_degraded"]:
        add("medium", "rolling_update", f"Update of {', '.join(g['degraded'])} only progresses by dropping replicas",
            ["update_reserve.groups_degraded: surge pods unplaceable, maxUnavailable used"],
            "Reserve surge capacity for these workloads or accept reduced availability during updates.")
    for gap in reserve.get("daemonset_surge_headroom_gaps") or []:
        add("medium", "rolling_update", f"DaemonSet {gap['daemonset']} cannot surge on {gap['node']}",
            [f"update_reserve.daemonset_surge_headroom_gaps: short of {gap['short']}"],
            "Keep per-node headroom of one DaemonSet pod or use maxSurge=0.")

    for pool, p in cap["pools"].items():
        for r, u in p["utilization_pct"].items():
            if u >= 85:
                add("high", "capacity", f"Pool {pool}: {r} requests at {u}% of allocatable",
                    [f"capacity.pools.{pool}.utilization_pct.{r}: {u}"], f"Plan capacity for {r} in pool {pool}.")
            elif u >= 70 and r in ("cpu", "memory", "pods", "pod-ips"):
                add("medium", "capacity", f"Pool {pool}: {r} requests at {u}% of allocatable",
                    [f"capacity.pools.{pool}.utilization_pct.{r}: {u}"], f"Watch {r} growth in pool {pool}.")
        for ref, d in p["reference_pods"].items():
            if d["fits_if_perfectly_packed"] and d["fragmentation_pct"] >= 30:
                add("medium", "fragmentation",
                    f"Pool {pool}: {d['fragmentation_pct']}% fragmentation for {ref} ({d['reference_requests']})",
                    [f"capacity.pools.{pool}.reference_pods.{ref}: {d['fits_per_node_sum']} fit per node vs "
                     f"{d['fits_if_perfectly_packed']} ideal"],
                    "Consolidate (descheduler / bin-packing scoring) or align pod sizes with node shapes.")
    hr = cap["hidden_risks"]
    if hr["pending_pods"]:
        add("high", "capacity", f"{len(hr['pending_pods'])} pod(s) are Pending now",
            [f"capacity.hidden_risks.pending_pods: {hr['pending_pods'][0]['pod']}: {hr['pending_pods'][0]['reason']}"],
            "Resolve current unschedulable demand before adding new workloads.")
    if hr["besteffort_pods"]:
        add("medium", "hidden_demand", f"{len(hr['besteffort_pods'])} BestEffort pod(s) are invisible to the scheduler",
            [f"capacity.hidden_risks.besteffort_pods: {hr['besteffort_pods'][:5]}"],
            "Set requests (LimitRange defaultRequest per namespace) so their consumption is accounted.")
    if hr["memory_usage_above_requests"]:
        add("high", "hidden_demand", f"{len(hr['memory_usage_above_requests'])} pod(s) use more memory than requested",
            [f"capacity.hidden_risks.memory_usage_above_requests: {hr['memory_usage_above_requests'][:3]}"],
            "Raise memory requests to observed peak; these pods are first to be evicted under memory pressure.")
    if hr["limits_overcommit"]:
        add("medium", "overcommit", f"Limits overcommit on {len({o['node'] for o in hr['limits_overcommit']})} node(s)",
            [f"capacity.hidden_risks.limits_overcommit: {hr['limits_overcommit'][:3]}"],
            "Review memory limits vs requests on these nodes (OOM/eviction risk under simultaneous peaks).")
    if hr["quotas_above_85pct"]:
        add("medium", "quota", f"{len(hr['quotas_above_85pct'])} quota key(s) above 85%",
            [f"capacity.hidden_risks.quotas_above_85pct: {hr['quotas_above_85pct'][:3]}"],
            "Surge pods are charged to quota: leave room for maxSurge x requests of the largest workload.")
    if hr["requests_far_above_usage"]:
        add("low", "efficiency", f"{len(hr['requests_far_above_usage'])} pod(s) request far more than they use",
            [f"capacity.hidden_risks.requests_far_above_usage: {hr['requests_far_above_usage'][:3]}"],
            "Right-size requests (VPA recommendations) to recover schedulable capacity.")
    for u in cap["unverified"]:
        add("low", "data_gap", f"Not verified: {u}", [f"capacity.unverified: {u}"],
            "Provide the missing limit in config (ipam / CSINode) to include it in the analysis.")
    risks.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], r["id"]))
    for i, r in enumerate(risks, 1):
        r["id"] = f"R{i:02d}"
    return risks
