"""N-1: simulate the loss of a node and re-schedule its pods on the rest.

Rules:
  * DaemonSet pods are not rescheduled (they die with the node) - expected, not a loss.
  * static (mirror) pods are node-bound by design: reported, not counted as a loss;
    pods without a controller are lost.
  * pods bound to a node-local PV cannot move (unavailable until the node returns).
  * movable pods are re-queued by priority (desc), then name (deterministic; the real
    queue order for equal priority is by creation time).
  * first without preemption; if a pod does not fit, DefaultPreemption is emulated and
    victims are reported as displaced (they are not re-scheduled again).
  * PDBs do not block an involuntary failure; we report which PDBs drop below
    desiredHealthy, and separately which would block a voluntary drain of that node.
  * optional autoscaler: how many template nodes of the pool are needed for what is
    left pending, within node-group max, and the provisioning delay.
"""

from __future__ import annotations

from .model import NodeInfo
from .quantity import fmt_list
from .selectors import match_label_selector
from .state import ClusterState


def choose_failure_nodes(ctx, base: ClusterState, max_nodes: int = 30) -> list[tuple[str, str]]:
    """All nodes for small clusters, otherwise per pool: most requested and largest."""
    names = sorted(n for n, i in base.nodes.items() if i.ready)
    if len(names) <= max_nodes:
        return [(n, "all nodes simulated") for n in names]
    out = []
    for pool, nodes in ctx.pools().items():
        nodes = [n for n in nodes if base.nodes[n].ready]
        if not nodes:
            continue
        loaded = max(nodes, key=lambda n: _util(base, n))
        largest = max(nodes, key=lambda n: (base.nodes[n].allocatable.get("cpu", 0), base.nodes[n].allocatable.get("memory", 0)))
        busiest = max(nodes, key=lambda n: len(base.by_node[n]))
        for n, why in ((loaded, "most requested in pool"), (largest, "largest in pool"), (busiest, "most pods in pool")):
            if n not in [x for x, _ in out]:
                out.append((n, f"{why} {pool}"))
    return out


def _util(base: ClusterState, n: str) -> float:
    a, u = base.nodes[n].allocatable, base.used[n]
    return max(u.get(r, 0) / a[r] for r in ("cpu", "memory", "pods") if a.get(r))


def simulate_node_failure(ctx, base: ClusterState, node: str, autoscaler: dict | None = None,
                          allow_preemption: bool = True) -> dict:
    st = base.clone()
    failed: NodeInfo = st.nodes[node]
    pods = st.remove_node(node)
    ds, static, lost, local_pv, movable = [], [], [], [], []
    for p in pods:
        if p.is_daemonset:
            ds.append(p)
        elif p.is_mirror or p.owner_kind == "Node":
            static.append(p)          # static pods live and die with their node by design
        elif p.owner_kind is None:
            lost.append(p)
        elif p.local_pv_node == node:
            local_pv.append(p)
        else:
            movable.append(p)

    # PDB impact right after the failure (before rescheduling), computed on the pre-failure state
    pdb_breaches = []
    for pdb in base.pdbs:
        before = base.pdb_status(pdb)
        gone = [p for p in pods if p.namespace == pdb["namespace"] and match_label_selector(pdb.get("selector"), p.labels)]
        if gone and before["healthy"] - len(gone) < before["desired"]:
            pdb_breaches.append({"pdb": before["name"], "healthy_after_failure": before["healthy"] - len(gone),
                                 "desired": before["desired"]})
    drain_blockers = []
    for pdb in base.pdbs:
        stt = base.pdb_status(pdb)
        on_node = [p for p in pods if p.namespace == pdb["namespace"] and match_label_selector(pdb.get("selector"), p.labels)]
        if on_node and stt["allowed"] < len(on_node):
            drain_blockers.append({"pdb": stt["name"], "pods_on_node": len(on_node), "disruptions_allowed": stt["allowed"]})

    movable.sort(key=lambda p: (-p.priority, p.key))
    placed, preempted, pending, displaced = [], [], [], []
    for p in movable:
        pl = st.schedule(p)
        if pl.ok:
            st.add_pod(p, pl.node)
            placed.append({"pod": p.key, "node": pl.node})
            continue
        if allow_preemption:
            pl2 = st.schedule(p, allow_preemption=True)
            if pl2.ok and pl2.victims:
                ev = st.place_with_preemption(p, pl2)
                displaced.extend(ev)
                preempted.append({"pod": p.key, "node": pl2.node, "priority": p.priority,
                                  "victims": [f"{v.key} (prio {v.priority})" for v in ev],
                                  "pdb_violations": pl2.pdb_violations})
                continue
        pending.append({"pod": p.key, "workload": p.workload_key, "priority": p.priority,
                        "requests": fmt_list({k: v for k, v in p.requests.items() if k in ("cpu", "memory")}),
                        "reason": pl.summary()})

    autoscale = None
    if (pending or preempted) and autoscaler and autoscaler.get("enabled"):
        autoscale = _autoscaler_recovery(ctx, st, failed, [p for p in movable if p.key in {x["pod"] for x in pending}]
                                         + displaced, autoscaler)

    if pending:
        verdict = "DEGRADED"
    elif preempted:
        verdict = "SURVIVES_WITH_PREEMPTION"
    else:
        verdict = "SURVIVES"
    return {
        "node": node,
        "pool": failed.pool,
        "verdict": verdict,
        "pods_on_node": len(pods),
        "daemonset_pods_not_rescheduled": len(ds),
        "static_pods_node_bound": [p.key for p in static],
        "lost_pods": [p.key for p in lost],
        "unmovable_local_pv_pods": [p.key for p in local_pv],
        "rescheduled_without_preemption": len(placed),
        "rescheduled_with_preemption": preempted,
        "displaced_by_preemption": [f"{p.key} (prio {p.priority})" for p in displaced],
        "pending_after_failure": pending,
        "pdb_below_desired_after_failure": pdb_breaches,
        "pdb_blocking_voluntary_drain": drain_blockers,
        "autoscaler": autoscale,
        "placements": placed[:50],
    }


def _autoscaler_recovery(ctx, st: ClusterState, failed: NodeInfo, waiting: list, cfg: dict) -> dict:
    group = next((g for g in cfg.get("node_groups") or [] if g.get("pool") == failed.pool), None)
    if group is None:
        return {"pool": failed.pool, "can_scale": False, "reason": "no autoscaling node group configured for this pool"}
    current = int(group.get("current", 0)) - 1  # the failed node left the group
    headroom = int(group.get("max", 0)) - current
    sim = st.clone()
    added = 0
    remaining = list(waiting)
    while remaining and added < headroom:
        added += 1
        ctx.add_synthetic_node(sim, failed, f"{failed.pool}-autoscaled-{added}")
        still = []
        for p in sorted(remaining, key=lambda p: (-p.priority, p.key)):
            pl = sim.schedule(p)
            if pl.ok:
                sim.add_pod(p, pl.node)
            else:
                still.append(p)
        if len(still) == len(remaining):
            remaining = still
            break
        remaining = still
    return {
        "pool": failed.pool,
        "node_group": group.get("name"),
        "nodes_needed": added if not remaining else None,
        "headroom_in_group": headroom,
        "provisioning_minutes": group.get("provisioning_minutes"),
        "still_unplaceable_after_scale_out": [p.key for p in remaining],
        "note": ("template node = copy of the failed node (same labels/taints/allocatable) incl. its "
                 "DaemonSet pods; pods are unavailable until the node is Ready"),
    }


def n_minus_1(ctx, base: ClusterState, autoscaler: dict | None = None, max_nodes: int = 30,
              nodes: list[str] | None = None) -> dict:
    chosen = [(n, "requested") for n in nodes] if nodes else choose_failure_nodes(ctx, base, max_nodes)
    scenarios = []
    for n, why in chosen:
        if n not in base.nodes:
            continue
        r = simulate_node_failure(ctx, base, n, autoscaler)
        r["selected_because"] = why
        scenarios.append(r)
    order = {"DEGRADED": 0, "SURVIVES_WITH_PREEMPTION": 1, "SURVIVES": 2}
    worst = min(scenarios, key=lambda s: (order[s["verdict"]], -len(s["pending_after_failure"]))) if scenarios else None
    if not scenarios:
        overall = "NOT_VERIFIED"
    else:
        overall = worst["verdict"]
    return {
        "overall": overall,
        "nodes_simulated": len(scenarios),
        "nodes_total": len(base.nodes),
        "coverage": "all nodes" if len(scenarios) == len(base.nodes) else "sampled per pool (most requested / largest / most pods)",
        "worst_node": worst["node"] if worst else None,
        "scenarios": scenarios,
    }
