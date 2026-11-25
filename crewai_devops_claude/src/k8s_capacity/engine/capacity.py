"""Static capacity model: per-node / per-pool / cluster requests vs allocatable,
limiting resource, fragmentation and hidden risks.

free(node, r) = allocatable(r) - sum(requests(r) of all non-terminal pods bound to the node)
               (DaemonSet, static and system pods included)
Usage metrics are used ONLY for risk/inefficiency signals, never for fit decisions.
"""

from __future__ import annotations

import statistics

from .quantity import CSI_VOLUMES, POD_IPS, fmt, fmt_list
from .state import ClusterState

CORE = ("cpu", "memory", "pods", POD_IPS, "ephemeral-storage")


def _tracked(state: ClusterState) -> list[str]:
    res = set(CORE)
    for n in state.nodes.values():
        for r, v in n.allocatable.items():
            if v and (r.startswith("hugepages-") or "/" in r):
                res.add(r)
    return sorted(res, key=lambda r: (r not in CORE, CORE.index(r) if r in CORE else 0, r))


def reference_pods(ctx, user_refs: list[dict] | None = None) -> list[dict]:
    """User-supplied reference pods, else p50/p90 of controller pod requests."""
    if user_refs:
        from .quantity import parse_resource_list
        out = []
        for r in user_refs:
            req = parse_resource_list(r.get("requests"))
            req.setdefault("pods", 1)
            req.setdefault(POD_IPS, 1)
            out.append({"name": r.get("name", "custom"), "requests": req, "source": "input"})
        return out
    cpus, mems = [], []
    for p in ctx.state.pods.values():
        if p.workload_key and not p.is_daemonset:
            cpus.append(p.requests.get("cpu", 0))
            mems.append(p.requests.get("memory", 0))
    if not cpus:
        return [{"name": "ref-default", "requests": {"cpu": 500, "memory": 512 * 2**20, "pods": 1, POD_IPS: 1},
                 "source": "default (no workloads found)"}]

    def q(vals, pct):
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(round(pct * (len(vals) - 1))))]

    return [
        {"name": "ref-p50", "requests": {"cpu": max(1, q(cpus, .5)), "memory": max(1, q(mems, .5)), "pods": 1, POD_IPS: 1},
         "source": f"median of {len(cpus)} controller pods"},
        {"name": "ref-p90", "requests": {"cpu": max(1, q(cpus, .9)), "memory": max(1, q(mems, .9)), "pods": 1, POD_IPS: 1},
         "source": f"p90 of {len(cpus)} controller pods"},
    ]


def _fit_count(free: dict[str, int], req: dict[str, int]) -> tuple[int, str]:
    best, lim = None, None
    for r, v in req.items():
        if v <= 0:
            continue
        if r not in free:
            continue  # unknown limit (e.g. pod IPs) -> not counted, flagged elsewhere
        n = max(0, free[r]) // v
        if best is None or n < best:
            best, lim = n, r
    return (best if best is not None else 0), (lim or "n/a")


def capacity_model(ctx, refs: list[dict] | None = None, state: ClusterState | None = None) -> dict:
    st = state or ctx.state
    refs = reference_pods(ctx, refs)
    tracked = _tracked(st)
    rows = []
    for name in sorted(st.nodes):
        n = st.nodes[name]
        used, free = st.used[name], st.free(name)
        ds_req: dict[str, int] = {}
        lim_sum: dict[str, int] = {}
        besteffort = 0
        for p in st.by_node[name].values():
            if p.is_daemonset or p.is_mirror:
                for r, v in p.requests.items():
                    ds_req[r] = ds_req.get(r, 0) + v
            for r, v in p.limits.items():
                lim_sum[r] = lim_sum.get(r, 0) + v
            besteffort += p.qos == "BestEffort"
        util = {r: round(100 * used.get(r, 0) / n.allocatable[r], 1)
                for r in tracked if n.allocatable.get(r)}
        limiting = max(util, key=util.get) if util else None
        usage_cpu = sum(ctx.usage.get(k, {}).get("cpu", 0) for k in st.by_node[name])
        usage_mem = sum(ctx.usage.get(k, {}).get("memory", 0) for k in st.by_node[name])
        rows.append({
            "node": name, "pool": n.pool, "zone": n.zone, "ready": n.ready, "schedulable": not n.unschedulable,
            "taints": [f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in n.taints],
            "allocatable": fmt_list({r: n.allocatable[r] for r in tracked if r in n.allocatable}),
            "capacity_minus_allocatable": fmt_list({r: n.capacity.get(r, 0) - n.allocatable.get(r, 0)
                                                    for r in ("cpu", "memory") if r in n.capacity}),
            "requested": fmt_list({r: used.get(r, 0) for r in tracked if r in n.allocatable}),
            "requested_by_daemonsets_static": fmt_list({r: ds_req.get(r, 0) for r in ("cpu", "memory", "pods")}),
            "free": fmt_list({r: free[r] for r in tracked if r in free}),
            "free_raw": {r: free[r] for r in tracked if r in free},
            "utilization_pct": util,
            "most_used_resource": limiting,
            "limits_overcommit_pct": {r: round(100 * lim_sum.get(r, 0) / n.allocatable[r], 1)
                                      for r in ("cpu", "memory") if n.allocatable.get(r)},
            "usage": fmt_list({"cpu": usage_cpu, "memory": usage_mem}) if ctx.usage else None,
            "pods": len(st.by_node[name]),
            "besteffort_pods": besteffort,
            "pod_ip_limit_source": n.ip_capacity_source,
            "fit_by_reference_pod": {ref["name"]: dict(zip(("count", "limited_by"), _fit_count(free, ref["requests"])))
                                     for ref in refs},
        })

    def aggregate(names: list[str], label: str) -> dict:
        names = [x for x in names if st.nodes[x].ready and not st.nodes[x].unschedulable]
        out = {"scope": label, "schedulable_nodes": len(names)}
        tot_alloc: dict[str, int] = {}
        tot_used: dict[str, int] = {}
        for x in names:
            for r in tracked:
                if r in st.nodes[x].allocatable:
                    tot_alloc[r] = tot_alloc.get(r, 0) + st.nodes[x].allocatable[r]
                    tot_used[r] = tot_used.get(r, 0) + st.used[x].get(r, 0)
        out["allocatable"] = fmt_list(tot_alloc)
        out["requested"] = fmt_list(tot_used)
        out["utilization_pct"] = {r: round(100 * tot_used[r] / tot_alloc[r], 1) for r in tot_alloc if tot_alloc[r]}
        per_ref = {}
        for ref in refs:
            actual = sum(_fit_count(st.free(x), ref["requests"])[0] for x in names)
            agg_free = {r: tot_alloc[r] - tot_used.get(r, 0) for r in tot_alloc}
            ideal, ideal_lim = _fit_count(agg_free, ref["requests"])
            lim_counter: dict[str, int] = {}
            for x in names:
                c, lr = _fit_count(st.free(x), ref["requests"])
                lim_counter[lr] = lim_counter.get(lr, 0) + 1
            per_ref[ref["name"]] = {
                "reference_requests": fmt_list({k: v for k, v in ref["requests"].items() if k in ("cpu", "memory")}),
                "fits_per_node_sum": actual,
                "fits_if_perfectly_packed": ideal,
                "fragmentation_pct": round(100 * (1 - actual / ideal), 1) if ideal else 0.0,
                "limiting_resource": ideal_lim if actual == ideal else _dominant(lim_counter),
                "limiting_resource_by_node_count": lim_counter,
            }
        out["reference_pods"] = per_ref
        # largest single pod that still fits somewhere (cpu, memory jointly on one node)
        best = sorted(((st.free(x).get("cpu", 0), st.free(x).get("memory", 0), x) for x in names), reverse=True)[:3]
        out["largest_free_slots"] = [{"node": x, "cpu": fmt("cpu", c), "memory": fmt("memory", m)} for c, m, x in best]
        return out

    pools = {pool: aggregate(nodes, f"pool {pool}") for pool, nodes in sorted(ctx.pools().items())}
    cluster = aggregate(list(st.nodes), "cluster")
    return {
        "reference_pods": [{"name": r["name"], "source": r["source"],
                            "requests": fmt_list({k: v for k, v in r["requests"].items() if k in ("cpu", "memory")})}
                           for r in refs],
        "nodes": rows,
        "pools": pools,
        "cluster": cluster,
        "hidden_risks": hidden_risks(ctx, st),
        "unverified": _unverified(st),
        "method": ("free = allocatable - sum(requests of non-terminal pods incl. DaemonSet/static); "
                   "fits_per_node_sum = sum over nodes of min_r floor(free_r / ref_r); "
                   "fits_if_perfectly_packed = min_r floor(sum free_r / ref_r); "
                   "fragmentation = 1 - fits_per_node_sum / fits_if_perfectly_packed. "
                   "Reference pods are assumed to tolerate the pool's taints."),
    }


def _dominant(counter: dict[str, int]) -> str:
    return max(counter, key=counter.get) if counter else "n/a"


def _unverified(st: ClusterState) -> list[str]:
    out = []
    no_ip = [n for n, i in st.nodes.items() if POD_IPS not in i.allocatable]
    if no_ip:
        out.append(f"pod IP limit unknown on {len(no_ip)} node(s): IP exhaustion not checked "
                   f"({st.nodes[no_ip[0]].ip_capacity_source})")
    no_csi = [n for n, i in st.nodes.items() if not i.csi_known]
    if no_csi:
        out.append(f"CSI attach limits unknown on {len(no_csi)} node(s)")
    return out


def hidden_risks(ctx, st: ClusterState) -> dict:
    besteffort = sorted(p.key for p in st.pods.values() if p.qos == "BestEffort" and not p.is_mirror)
    over_mem, over_cpu, idle = [], [], []
    for k, u in ctx.usage.items():
        p = st.pods.get(k)
        if p is None:
            continue
        rm, rc = p.requests.get("memory", 0), p.requests.get("cpu", 0)
        if u.get("memory") and u["memory"] > rm:
            over_mem.append({"pod": k, "requests": fmt("memory", rm), "usage": fmt("memory", u["memory"])})
        if u.get("cpu") and rc and u["cpu"] > 1.5 * rc:
            over_cpu.append({"pod": k, "requests": fmt("cpu", rc), "usage": fmt("cpu", u["cpu"])})
        if rc >= 500 and u.get("cpu", 0) < 0.2 * rc and rm >= 2**30 and u.get("memory", rm) < 0.3 * rm:
            idle.append({"pod": k, "requests": fmt_list({"cpu": rc, "memory": rm}),
                         "usage": fmt_list({"cpu": u.get("cpu", 0), "memory": u.get("memory", 0)})})
    overcommit = []
    for name, n in st.nodes.items():
        for r in ("cpu", "memory"):
            lim = sum(p.limits.get(r, 0) for p in st.by_node[name].values())
            unlimited = sum(1 for p in st.by_node[name].values() if r not in p.limits and not p.is_mirror)
            if n.allocatable.get(r) and lim > 1.5 * n.allocatable[r]:
                overcommit.append({"node": name, "resource": r, "limits_pct": round(100 * lim / n.allocatable[r]),
                                   "pods_without_limit": unlimited})
    quotas = []
    for q in st.quota.quotas:
        for k, hard in q["hard"].items():
            used = q["used"].get(k, 0)
            if hard and used / hard >= 0.85:
                quotas.append({"quota": f"{q['namespace']}/{q['name']}", "key": k, "used_pct": round(100 * used / hard, 1)})
    return {
        "besteffort_pods": besteffort,
        "pending_pods": [{"pod": p.key, "reason": st.schedule(p).summary()} for p in ctx.pending][:20],
        "memory_usage_above_requests": over_mem,
        "cpu_usage_far_above_requests": over_cpu,
        "requests_far_above_usage": idle,
        "limits_overcommit": overcommit,
        "quotas_above_85pct": quotas,
        "not_ready_or_cordoned": [n for n, i in st.nodes.items() if not i.ready or i.unschedulable],
        "usage_source": (ctx.snapshot.get("usage") or {}).get("source", "none"),
    }


def median(values):
    return statistics.median(values) if values else 0
