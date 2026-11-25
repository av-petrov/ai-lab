"""Admission simulation: will this change fit, and what does it cost the cluster?

Checks (each PASS / WARN / FAIL / NOT_VERIFIED / SKIPPED):
  1. namespace_quota        ResourceQuota (steady state) + LimitRange defaults/validation
  2. steady_state_placement every replica placed per node with all scheduler filters
  3. app_rollout            resize: the rollout of this change itself;
                            new/scale: the next rolling update of the app after deploy
  4. others_update_reserve  update reserve of other workloads under the concurrency model,
                            before vs after (regressions only are attributed to the change)
  5. n_minus_1_after        node-failure simulation before vs after
Verdict rules live in `_verdict` (code), not in the LLM.
"""

from __future__ import annotations

import copy
import json

import yaml

from .failure import n_minus_1
from .model import Workload, resolve_int_or_percent
from .quantity import fmt, fmt_list, parse_resource_list, scale
from .rollout import RolloutSim, reserve_analysis, run_rollouts, static_deadlocks
from .state import ClusterState

DEFAULT_POLICY = {
    "update_concurrency": "sequential",   # sequential | per_namespace | all
    "custom_update_groups": None,
    "require_n_minus_1": True,            # N-1 regression -> DOES_NOT_FIT
    "require_update_reserve": True,       # breaking others' update reserve -> DOES_NOT_FIT
    "allow_preemption": False,            # placement only via preemption -> condition, or FAIL if False and strict
    "strict_future_rollout": True,        # new app whose own next rollout deadlocks -> DOES_NOT_FIT
    "autoscaler": None,
    "n_minus_1_max_nodes": 30,
    "n_minus_1_min_priority": 0,          # newly pending pods below this priority do not fail N-1
}


# =========================================================================== input
def spec_to_workload(spec: dict) -> tuple[Workload, list[dict]]:
    """AdmissionSpec (dict) or embedded manifest -> (Workload, new_claims)."""
    if spec.get("manifest_yaml"):
        wl, claims = manifest_to_workload(spec["manifest_yaml"], spec)
        return _apply_overrides(wl, spec, manifest=True), claims
    kind = spec.get("kind") or "Deployment"
    labels = dict(spec.get("labels") or {"app": spec["name"]})

    def container(c: dict, init: bool = False) -> dict:
        out = {"name": c["name"], "resources": {}}
        if c.get("requests"):
            out["resources"]["requests"] = dict(c["requests"])
        if c.get("limits"):
            out["resources"]["limits"] = dict(c["limits"])
        if c.get("host_ports"):
            out["ports"] = [{"containerPort": hp.get("container_port", hp["port"]), "hostPort": hp["port"],
                             "protocol": hp.get("protocol", "TCP")} for hp in c["host_ports"]]
        if init and c.get("sidecar"):
            out["restartPolicy"] = "Always"
        return out

    pod_spec = {
        "containers": [container(c) for c in spec.get("containers") or []],
        "initContainers": [container(c, True) for c in spec.get("init_containers") or []],
        "nodeSelector": spec.get("node_selector") or {},
        "affinity": spec.get("affinity") or {},
        "tolerations": spec.get("tolerations") or [],
        "topologySpreadConstraints": spec.get("topology_spread_constraints") or [],
        "priorityClassName": spec.get("priority_class_name"),
        "runtimeClassName": spec.get("runtime_class_name"),
        "hostNetwork": bool(spec.get("host_network")),
        "volumes": [],
    }
    vcts, new_claims = [], []
    for pvc in spec.get("pvcs") or []:
        if kind == "StatefulSet" and pvc.get("per_replica", True):
            vcts.append({"metadata": {"name": pvc["name"]}, "spec": {
                "storageClassName": pvc.get("storage_class"), "accessModes": pvc.get("access_modes") or ["ReadWriteOnce"],
                "resources": {"requests": {"storage": pvc.get("size", "1Gi")}}}})
        else:
            pod_spec["volumes"].append({"name": pvc["name"], "persistentVolumeClaim": {"claimName": pvc["name"]}})
            new_claims.append({"name": pvc["name"], "storage_class": pvc.get("storage_class"),
                               "size": pvc.get("size", "1Gi"), "access_modes": pvc.get("access_modes") or ["ReadWriteOnce"],
                               "existing": pvc.get("existing", False)})
    strat = spec.get("strategy") or {}
    default_surge = "25%" if kind == "Deployment" else 0
    default_unav = "25%" if kind == "Deployment" else 1
    wl = Workload(
        kind=kind, namespace=spec["namespace"], name=spec["name"], replicas=int(spec.get("replicas") or 1),
        strategy_type=strat.get("type") or "RollingUpdate",
        max_surge=strat.get("max_surge", default_surge), max_unavailable=strat.get("max_unavailable", default_unav),
        template_metadata={"labels": labels}, template_spec={k: v for k, v in pod_spec.items() if v not in (None, [], {})},
        selector={"matchLabels": labels}, volume_claim_templates=vcts,
    )
    return _apply_overrides(wl, spec, manifest=False), new_claims


def _apply_overrides(wl: Workload, spec: dict, manifest: bool) -> Workload:
    """Remediation variants patch a spec dict; for manifests the structured fields act as overrides."""
    t = copy.deepcopy(wl.template_spec)
    if manifest:
        if spec.get("replicas") is not None:
            wl.replicas = int(spec["replicas"])
        if spec.get("strategy"):
            st = spec["strategy"]
            wl.strategy_type = st.get("type") or wl.strategy_type
            wl.max_surge = st.get("max_surge", wl.max_surge)
            wl.max_unavailable = st.get("max_unavailable", wl.max_unavailable)
        if spec.get("node_selector"):
            t["nodeSelector"] = {**(t.get("nodeSelector") or {}), **spec["node_selector"]}
        if spec.get("tolerations"):
            t["tolerations"] = (t.get("tolerations") or []) + list(spec["tolerations"])
        if spec.get("affinity"):
            t["affinity"] = spec["affinity"]
    factor = spec.get("requests_factor")
    if factor:
        for c in (t.get("containers") or []):
            res = c.setdefault("resources", {})
            req = res.get("requests") or {}
            res["requests"] = {k: _scale_q(k, q, factor) for k, q in req.items()}
            if res.get("limits"):
                res["limits"] = {k: (_scale_q(k, q, factor) if k in req and k != "memory" else q)
                                 for k, q in res["limits"].items()}
    wl.template_spec = t
    return wl


def manifest_to_workload(text: str, spec: dict | None = None) -> tuple[Workload, list[dict]]:
    docs = [d for d in yaml.safe_load_all(text) if d]
    wdoc = next((d for d in docs if d.get("kind") in ("Deployment", "StatefulSet", "DaemonSet")), None)
    if wdoc is None:
        raise ValueError("manifest has no Deployment/StatefulSet/DaemonSet")
    md, s = wdoc.get("metadata") or {}, wdoc.get("spec") or {}
    kind = wdoc["kind"]
    strat = s.get("strategy") or s.get("updateStrategy") or {}
    ru = strat.get("rollingUpdate") or {}
    tmpl = s.get("template") or {}
    ns = md.get("namespace") or (spec or {}).get("namespace") or "default"
    wl = Workload(
        kind=kind, namespace=ns, name=md["name"], replicas=int(s.get("replicas", 1)),
        strategy_type=strat.get("type") or "RollingUpdate",
        max_surge=ru.get("maxSurge", "25%" if kind == "Deployment" else 0),
        max_unavailable=ru.get("maxUnavailable", "25%" if kind == "Deployment" else 1),
        template_metadata=tmpl.get("metadata") or {}, template_spec=tmpl.get("spec") or {},
        selector=s.get("selector"), volume_claim_templates=s.get("volumeClaimTemplates") or [],
    )
    new_claims = []
    for d in docs:
        if d.get("kind") == "PersistentVolumeClaim":
            ps = d.get("spec") or {}
            new_claims.append({"name": d["metadata"]["name"], "storage_class": ps.get("storageClassName"),
                               "size": ((ps.get("resources") or {}).get("requests") or {}).get("storage", "1Gi"),
                               "access_modes": ps.get("accessModes") or ["ReadWriteOnce"]})
    # claims declared in the manifest are new; drop them from pod volumes so they resolve as new
    names = {c["name"] for c in new_claims}
    wl.template_spec = copy.deepcopy(wl.template_spec)
    wl.template_spec["volumes"] = [v for v in wl.template_spec.get("volumes") or []
                                   if (v.get("persistentVolumeClaim") or {}).get("claimName") not in names]
    return wl, new_claims


# =========================================================================== LimitRange
def apply_limitranges(ctx, namespace: str, pod_spec: dict) -> tuple[dict, list[str], list[str]]:
    """Return (mutated spec, applied defaults, violations)."""
    spec = copy.deepcopy(pod_spec)
    applied, violations = [], []
    items = [it for lr in ctx.limitranges if lr["namespace"] == namespace for it in lr.get("limits") or []]
    for it in items:
        if it.get("type") != "Container":
            continue
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            res = c.setdefault("resources", {})
            lim = res.setdefault("limits", {})
            req = res.setdefault("requests", {})
            # 1) API defaulting: an explicit limit without a request -> request = limit
            for r, v in lim.items():
                req.setdefault(r, v)
            # 2) LimitRanger: default limits, then defaultRequest (which itself defaults to `default`)
            for r, v in (it.get("default") or {}).items():
                if r not in lim:
                    lim[r] = v
                    applied.append(f"{c['name']}: limits.{r}={v} (LimitRange default)")
            default_req = {**(it.get("default") or {}), **(it.get("defaultRequest") or {})}
            for r, v in default_req.items():
                if r not in req:
                    req[r] = v
                    applied.append(f"{c['name']}: requests.{r}={v} (LimitRange defaultRequest)")
            pr, pl = parse_resource_list(req), parse_resource_list(lim)
            for r, v in parse_resource_list(it.get("min")).items():
                if pr.get(r, 0) < v:
                    violations.append(f"{c['name']}: requests.{r} {fmt(r, pr.get(r, 0))} < LimitRange min {fmt(r, v)}")
            for r, v in parse_resource_list(it.get("max")).items():
                if r not in pl:
                    violations.append(f"{c['name']}: limits.{r} unset but LimitRange max {fmt(r, v)} requires a limit")
                elif pl[r] > v:
                    violations.append(f"{c['name']}: limits.{r} {fmt(r, pl[r])} > LimitRange max {fmt(r, v)}")
            for r, ratio in (it.get("maxLimitRequestRatio") or {}).items():
                if pr.get(r) and pl.get(r) and pl[r] / pr[r] > float(ratio):
                    violations.append(f"{c['name']}: {r} limit/request {pl[r] / pr[r]:.2f} > maxLimitRequestRatio {ratio}")
            # drop empty dicts
            if not lim:
                res.pop("limits")
            if not req:
                res.pop("requests")
    return spec, applied, violations


# =========================================================================== helpers
def _check(name: str, status: str, summary: str, **details) -> dict:
    return {"check": name, "status": status, "summary": summary, "details": details}


def _ctx_with(ctx, state: ClusterState, workloads: dict | None = None):
    c = copy.copy(ctx)
    c.state = state
    c.workloads = workloads if workloads is not None else dict(ctx.workloads)
    return c


def _baseline(ctx, policy: dict) -> dict:
    key = (id(ctx.state), policy["update_concurrency"], json.dumps(policy.get("custom_update_groups")),
           json.dumps(policy.get("autoscaler"), sort_keys=True, default=str))
    cache = getattr(ctx, "_baseline_cache", None)
    if cache is None:
        cache = {}
        ctx._baseline_cache = cache
    if key not in cache or cache[key]["state"] is not ctx.state:  # id() may be reused after GC
        cache[key] = {
            "state": ctx.state,
            "reserve": reserve_analysis(ctx, ctx.state, policy["update_concurrency"], policy.get("custom_update_groups")),
            "n1": n_minus_1(ctx, ctx.state, policy.get("autoscaler"), policy["n_minus_1_max_nodes"]),
        }
    return cache[key]


def _place_replicas(ctx, st: ClusterState, wl: Workload, spec: dict, count: int, new_claims: list[dict],
                    allow_preemption: bool, start_index: int = 0) -> dict:
    placed, failed, preempted = [], [], []
    per_node: dict[str, int] = {}
    if wl.kind == "DaemonSet":
        probe = ctx.make_pod({"name": f"{wl.name}-probe", "namespace": wl.namespace,
                              "labels": wl.template_metadata.get("labels") or {}}, spec)
        targets = sorted(n for n, i in st.nodes.items() if st.node_eligible(probe, i) and i.ready)
        items = [(f"{wl.name}-{n}", n) for n in targets]
    else:
        items = [(f"{wl.name}-{i}" if wl.kind == "StatefulSet" else f"{wl.name}-adm-{i}", None)
                 for i in range(start_index, start_index + count)]
    unverified: set[str] = set()
    for idx, (name, pin) in enumerate(items):
        labels = dict(wl.template_metadata.get("labels") or {})
        labels["pod-template-hash"] = "adm"
        pspec = copy.deepcopy(spec)
        claims = [dict(c) for c in new_claims if not c.get("existing")]
        if wl.kind == "StatefulSet":
            ordinal = start_index + idx
            for vct in wl.volume_claim_templates:
                vs = vct.get("spec") or {}
                claims.append({"name": f"{vct['metadata']['name']}-{wl.name}-{ordinal}",
                               "storage_class": vs.get("storageClassName"),
                               "size": ((vs.get("resources") or {}).get("requests") or {}).get("storage"),
                               "access_modes": vs.get("accessModes")})
        for c in new_claims:
            if c.get("existing"):
                pspec.setdefault("volumes", []).append({"name": c["name"], "persistentVolumeClaim": {"claimName": c["name"]}})
        pod = ctx.make_pod({"name": name, "namespace": wl.namespace, "labels": labels,
                            "ownerReferences": [{"kind": "ReplicaSet" if wl.kind == "Deployment" else wl.kind,
                                                 "name": wl.name, "controller": True}]},
                           pspec, {"phase": "Pending"}, new_claims=claims)
        # claims shared by all replicas: identical name -> RWO forces one node, handled by state
        pod.workload_key = wl.key
        if pin:
            pod.pinned_node = pin
        unverified.update(pod.unverified)
        unverified.update(st.unverified_resources(pod))
        st.quota.charge(pod, +1)
        pl = st.schedule(pod)
        if pl.ok:
            st.add_pod(pod, pl.node)
            placed.append(pod.key)
            per_node[pl.node] = per_node.get(pl.node, 0) + 1
            continue
        pl2 = st.schedule(pod, allow_preemption=True)
        if pl2.ok and pl2.victims:
            preempted.append({"pod": pod.key, "node": pl2.node, "victims": pl2.victims,
                              "pdb_violations": pl2.pdb_violations})
            if allow_preemption:
                st.place_with_preemption(pod, pl2)
                placed.append(pod.key)
                per_node[pl2.node] = per_node.get(pl2.node, 0) + 1
                continue
        failed.append({"pod": pod.key, "reason": pl.summary(), "per_node": {k: v for k, v in sorted(pl.reasons.items())}})
    return {"placed": placed, "per_node": per_node, "failed": failed, "preemption_possible": preempted,
            "unverified": sorted(unverified)}


# =========================================================================== main
def simulate_admission(ctx, spec: dict, policy: dict | None = None) -> dict:
    policy = {**DEFAULT_POLICY, **(policy or {})}
    wl, new_claims = spec_to_workload(spec)
    change = spec.get("change_type") or "new"
    existing = ctx.workloads.get(wl.key)
    notes: list[str] = []
    if change in ("resize", "scale") and existing is None:
        same_ns = sorted(k for k in ctx.workloads if k.split("/")[1] == wl.namespace)
        msg = (f"{wl.key} not found in snapshot (change_type={change} needs an existing workload); "
               f"workloads in namespace {wl.namespace}: {same_ns[:10] or 'none'}")
        checks = [_check("namespace_quota", "SKIPPED", msg)] + [
            _check(n, "SKIPPED", "target workload not found")
            for n in ("steady_state_placement", "app_rollout", "others_update_reserve", "n_minus_1_after")]
        checks[0]["status"] = "FAIL"
        return {"workload": wl.key, "change_type": change, "replicas": wl.replicas, "pod_effective_requests": {},
                "verdict": "DOES_NOT_FIT", "conditions": [msg], "confidence": "high", "checks": checks,
                "not_verified": [], "notes": [msg], "policy": policy, "error": msg}
    if change == "new" and existing is not None:
        notes.append(f"{wl.key} already exists; treated as resize")
        change = "resize"
    if change == "scale":
        tmpl_spec = existing.template_spec
        wl.template_metadata, wl.template_spec = existing.template_metadata, existing.template_spec
        wl.strategy_type, wl.max_surge, wl.max_unavailable = existing.strategy_type, existing.max_surge, existing.max_unavailable
        wl.volume_claim_templates = existing.volume_claim_templates
    tmpl_spec, lr_applied, lr_violations = apply_limitranges(ctx, wl.namespace, wl.template_spec)
    wl.template_spec = tmpl_spec
    base = ctx.state
    checks = []

    probe = ctx.make_pod({"name": f"{wl.name}-probe", "namespace": wl.namespace,
                          "labels": wl.template_metadata.get("labels") or {}}, tmpl_spec)
    pod_req = {k: v for k, v in probe.requests.items() if v}

    # ---------------------------------------------------------- 1. quota + LimitRange
    ledger = base.quota.clone()
    old_keys = [k for k, p in base.pods.items() if p.workload_key == wl.key]
    if change == "resize":
        for k in old_keys:
            ledger.charge(base.pods[k], -1)
        count = wl.replicas
    elif change == "scale":
        count = max(0, wl.replicas - len(old_keys))
    else:
        count = wl.replicas if wl.kind != "DaemonSet" else len(base.nodes)
    q_viol = ledger.check(probe, count) if count else []
    q_notes = ledger.unverified_notes(wl.namespace)
    status = "FAIL" if (q_viol or lr_violations) else ("NOT_VERIFIED" if q_notes else "PASS")
    checks.append(_check("namespace_quota", status,
                         "; ".join(q_viol + lr_violations) or f"{count} pod(s) fit the namespace quota",
                         pods_counted=count, pod_effective_requests=fmt_list(pod_req),
                         limitrange_defaults_applied=lr_applied, limitrange_violations=lr_violations,
                         quota_violations=q_viol, not_evaluated=q_notes))

    # ---------------------------------------------------------- 2. steady state
    st = base.clone()
    if change == "resize":
        for k in old_keys:
            st.remove_pod(k, refund_quota=True)
        res = _place_replicas(ctx, st, wl, tmpl_spec, wl.replicas, new_claims, policy["allow_preemption"])
    elif change == "scale":
        if wl.replicas < len(old_keys):
            for k in sorted(old_keys)[wl.replicas:]:
                st.remove_pod(k, refund_quota=True)
            res = {"placed": [], "per_node": {}, "failed": [], "preemption_possible": [], "unverified": []}
        else:
            res = _place_replicas(ctx, st, wl, tmpl_spec, count, new_claims, policy["allow_preemption"],
                                  start_index=len(old_keys))
    else:
        res = _place_replicas(ctx, st, wl, tmpl_spec, wl.replicas, new_claims, policy["allow_preemption"])
    if res["failed"]:
        s2 = "FAIL"
        summ = f"{len(res['failed'])} replica(s) cannot be placed: {res['failed'][0]['reason']}"
    elif res["preemption_possible"] and policy["allow_preemption"]:
        s2 = "WARN"
        summ = f"fits only by preempting lower-priority pods ({len(res['preemption_possible'])} replica(s))"
    else:
        s2 = "PASS"
        summ = f"all replicas placed: {res['per_node']}"
    if s2 == "PASS" and res["unverified"]:
        s2 = "NOT_VERIFIED" if any("Immediate" in u or "unknown" in u.lower() for u in res["unverified"]) else s2
    missing = [r for r in ("cpu", "memory") if not probe.requests.get(r)]
    if s2 == "PASS" and missing:
        # the scheduler counts a missing request as zero: "fits" only proves pod count / IPs
        s2 = "WARN"
        summ += (f"; but the pod has no {'/'.join(missing)} requests ({probe.qos}): the scheduler counts 0, "
                 "so this fit does not reserve real capacity - set requests")
    checks.append(_check("steady_state_placement", s2, summ, placement_per_node=res["per_node"],
                         failed=res["failed"][:5], preemption_candidates=res["preemption_possible"][:5],
                         not_evaluated=res["unverified"],
                         cluster_free_sum_would_suggest=_sum_suggests(base, pod_req, count, probe)))

    # post-change workload object
    wl_after = copy.deepcopy(wl)
    wl_after.pod_keys = [k for k, p in st.pods.items() if p.workload_key == wl.key]
    wls_after = dict(ctx.workloads)
    wls_after[wl.key] = wl_after
    ctx_after = _ctx_with(ctx, st, wls_after)

    if s2 == "FAIL":
        for name in ("app_rollout", "others_update_reserve", "n_minus_1_after"):
            checks.append(_check(name, "SKIPPED", "steady-state placement failed"))
        return _finish(wl, change, checks, policy, notes, pod_req)

    # ---------------------------------------------------------- 3. app rollout
    if change == "resize":
        ex = copy.deepcopy(existing)
        if spec.get("strategy") or spec.get("manifest_yaml"):  # the change may also alter the strategy
            ex.strategy_type, ex.max_surge, ex.max_unavailable = wl.strategy_type, wl.max_surge, wl.max_unavailable
        sim = RolloutSim(ctx, ex, new_template_spec=tmpl_spec, new_replicas=wl.replicas, tag="resize")
        rr = run_rollouts(base, [sim])["results"][0]
        deadlocks = static_deadlocks(ctx, base, ex, tmpl_spec, wl.replicas)
        what = "rollout of this change"
    else:
        sim = RolloutSim(ctx_after, wl_after, tag="next")
        rr = run_rollouts(st, [sim])["results"][0]
        deadlocks = static_deadlocks(ctx_after, st, wl_after)
        what = "next rolling update of the application after deploy"
    if rr.status == "STUCK":
        s3 = "FAIL" if (change == "resize" or policy["strict_future_rollout"]) else "WARN"
        summ = f"{what} gets stuck: {rr.stuck_reason}"
    elif rr.used_unavailability or rr.quota_blocked:
        s3 = "WARN"
        summ = (f"{what} completes only by temporarily reducing available replicas "
                f"(surge pods not placeable{' / quota' if rr.quota_blocked else ''})")
    else:
        s3 = "PASS"
        summ = f"{what} completes with surge {rr.max_surge}"
    checks.append(_check("app_rollout", s3, summ, rollout=rr.to_dict(), static_deadlocks=deadlocks))

    # ---------------------------------------------------------- 4. others' update reserve
    before = _baseline(ctx, policy)
    after_reserve = reserve_analysis(ctx_after, st, policy["update_concurrency"], policy.get("custom_update_groups"))
    infeasible_before = {tuple(g["group"]) for g in before["reserve"]["groups_infeasible"]}
    regress = [g for g in after_reserve["groups_infeasible"]
               if wl.key not in g["group"] and tuple(g["group"]) not in infeasible_before]
    preexisting = [g for g in after_reserve["groups_infeasible"] if tuple(g["group"]) in infeasible_before]
    if regress:
        s4 = "FAIL" if policy["require_update_reserve"] else "WARN"
        summ = f"{len(regress)} update group(s) of other workloads can no longer roll out: " + \
               "; ".join(f"{s['workload']}: {s['reason']}" for g in regress[:3] for s in g["stuck"][:1])
    else:
        s4 = "PASS"
        summ = "update reserve of other workloads preserved"
        if preexisting:
            summ += f" ({len(preexisting)} group(s) were already infeasible before the change)"
    checks.append(_check("others_update_reserve", s4, summ, concurrency_model=policy["update_concurrency"],
                         regressions=regress[:5], preexisting_infeasible=[g["group"] for g in preexisting][:10],
                         required_reserve_before=before["reserve"]["required_reserve_analytic"],
                         required_reserve_after=after_reserve["required_reserve_analytic"]))

    # ---------------------------------------------------------- 5. N-1
    n1_before = before["n1"]
    nodes = sorted({s["node"] for s in n1_before["scenarios"]} | set(res["per_node"]))
    n1_after = n_minus_1(ctx_after, st, policy.get("autoscaler"), nodes=nodes)
    bmap = {s["node"]: s for s in n1_before["scenarios"]}
    worse, expendable = [], []
    min_prio = policy.get("n_minus_1_min_priority", 0)
    rank = {"SURVIVES": 0, "SURVIVES_WITH_PREEMPTION": 1, "DEGRADED": 2}
    for s in n1_after["scenarios"]:
        b = bmap.get(s["node"])
        if b is None and s["node"] in base.nodes:
            b = next(iter(n_minus_1(ctx, base, policy.get("autoscaler"), nodes=[s["node"]])["scenarios"]), None)
        # compare per workload, not per pod name (a resize renames the app's pods)
        before_pending = (b or {}).get("pending_after_failure", [])
        budget: dict[str, int] = {}
        for p in before_pending:
            k = p.get("workload") or p["pod"]
            budget[k] = budget.get(k, 0) + 1
        new_pending = []
        for p in s["pending_after_failure"]:
            k = p.get("workload") or p["pod"]
            if budget.get(k, 0) > 0:
                budget[k] -= 1
            else:
                new_pending.append(p)
        important = [p["pod"] for p in new_pending if p["priority"] >= min_prio]
        expendable += [p["pod"] for p in new_pending if p["priority"] < min_prio]
        verdict_worse = b is not None and rank[s["verdict"]] > rank[b["verdict"]] and not new_pending
        if important or verdict_worse:
            worse.append({"node": s["node"], "before": b["verdict"] if b else "n/a (node added)", "after": s["verdict"],
                          "pending_before": len(before_pending), "pending_after": len(s["pending_after_failure"]),
                          "newly_pending": sorted(important)[:10]})
    if worse:
        s5 = "FAIL" if policy["require_n_minus_1"] else "WARN"
        w0 = worse[0]
        summ = (f"node failure tolerance degrades on {len(worse)} node(s), e.g. loss of {w0['node']}: "
                f"{w0['before']} -> {w0['after']}, pending pods {w0['pending_before']} -> {w0['pending_after']} "
                f"(new: {', '.join(w0['newly_pending'][:3])})")
    else:
        # not made worse by this change: pre-existing N-1 problems are reported, not attributed
        s5 = "PASS"
        summ = f"N-1 not degraded by the change for pods with priority >= {min_prio} (after: {n1_after['overall']}, before: {n1_before['overall']})"
        if n1_after["overall"] != "SURVIVES":
            summ += "; pre-existing node-failure risk, see audit"
    checks.append(_check("n_minus_1_after", s5, summ, regressions=worse[:5], expendable_newly_pending=sorted(set(expendable))[:10],
                         min_priority_counted=min_prio, overall_before=n1_before["overall"],
                         overall_after=n1_after["overall"], nodes_simulated=nodes, coverage=n1_after["coverage"]))
    return _finish(wl, change, checks, policy, notes, pod_req)


def _sum_suggests(base: ClusterState, pod_req: dict, count: int, probe=None) -> dict:
    """What a naive 'sum of free resources' check would say (shown only to explain fragmentation).
    eligible = nodes the pod may land on by selector/affinity/taints, ignoring resources."""
    def total(names):
        out: dict[str, int] = {}
        for n in names:
            for r, v in base.free(n).items():
                out[r] = out.get(r, 0) + max(0, v)
        return out

    need = scale({k: v for k, v in pod_req.items() if k in ("cpu", "memory")}, count)
    all_free = total(base.nodes)
    eligible = [n for n, i in base.nodes.items() if i.ready and not i.unschedulable and
                (probe is None or base.node_eligible(probe, i))]
    el_free = total(eligible)
    return {"cluster_free": fmt_list({k: all_free.get(k, 0) for k in need}),
            "eligible_nodes": eligible,
            "eligible_free": fmt_list({k: el_free.get(k, 0) for k in need}),
            "needed": fmt_list(need),
            "naive_sum_says_fits": all(el_free.get(k, 0) >= v for k, v in need.items())}


def _finish(wl, change, checks, policy, notes, pod_req) -> dict:
    verdict, conditions = _verdict(checks)
    unverified = sorted({u for c in checks for u in (c["details"].get("not_evaluated") or [])})
    confidence = "high" if not unverified else ("medium" if len(unverified) <= 2 else "low")
    if any(c["status"] == "NOT_VERIFIED" for c in checks):
        confidence = "low" if confidence != "high" else "medium"
    return {
        "workload": wl.key, "change_type": change, "replicas": wl.replicas,
        "pod_effective_requests": fmt_list(pod_req),
        "verdict": verdict, "conditions": conditions, "confidence": confidence,
        "checks": checks, "not_verified": unverified, "notes": notes, "policy": policy,
    }


def _verdict(checks: list[dict]) -> tuple[str, list[str]]:
    if any(c["status"] == "FAIL" for c in checks):
        return "DOES_NOT_FIT", [f"{c['check']}: {c['summary']}" for c in checks if c["status"] == "FAIL"]
    conds = [f"{c['check']}: {c['summary']}" for c in checks if c["status"] in ("WARN", "NOT_VERIFIED")]
    return ("FITS_WITH_CONDITIONS" if conds else "FITS"), conds


# =========================================================================== remediation
def search_remediations(ctx, spec: dict, policy: dict | None = None, max_added_nodes: int = 5) -> dict:
    """Smallest deterministic change that turns the verdict into FITS (or at least
    FITS_WITH_CONDITIONS). Every candidate is re-evaluated with the full check set."""
    policy = {**DEFAULT_POLICY, **(policy or {})}
    base_res = simulate_admission(ctx, spec, policy)
    if base_res.get("error"):
        return {"original_verdict": base_res["verdict"], "original_failures": ["target_not_found"], "options": []}
    if base_res["verdict"] == "FITS":
        return {"original_verdict": "FITS", "options": []}
    options = []
    failing = {c["check"] for c in base_res["checks"] if c["status"] == "FAIL"}

    def try_variant(label: str, change: str, variant: dict, c=ctx) -> dict | None:
        r = simulate_admission(c, variant, policy)
        if r["verdict"] != "DOES_NOT_FIT":
            return {"option": label, "change": change, "verdict": r["verdict"], "conditions": r["conditions"]}
        return None

    # 1. rollout strategy (deadlocks)
    if "app_rollout" in failing and (spec.get("kind") or "Deployment") == "Deployment":
        v = copy.deepcopy(spec)
        v["strategy"] = {"type": "RollingUpdate", "max_surge": 0, "max_unavailable": 1}
        o = try_variant("strategy", "maxSurge=0, maxUnavailable=1 (rollout without surge, -1 replica during update)", v)
        if o:
            options.append(o)
        aff = spec.get("affinity") or spec_to_workload(spec)[0].template_spec.get("affinity") or {}
        if (aff.get("podAntiAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution"):
            v = copy.deepcopy(spec)
            v["affinity"] = copy.deepcopy(aff)
            req = v["affinity"]["podAntiAffinity"].pop("requiredDuringSchedulingIgnoredDuringExecution")
            v["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"] = [
                {"weight": 100, "podAffinityTerm": t} for t in req]
            o = try_variant("anti_affinity", "required podAntiAffinity -> preferred (weight 100)", v)
            if o:
                options.append(o)
    # 2. fewer replicas
    R = int(spec.get("replicas") or 1)
    if spec.get("kind", "Deployment") != "DaemonSet" and R > 1:
        for r in range(R - 1, 0, -1):
            v = dict(spec, replicas=r)
            o = try_variant("replicas", f"replicas {R} -> {r}", v)
            if o:
                options.append(o)
                break
    # 3. other pool
    pool_label = _pool_label(ctx)
    current_pools = _target_pools(ctx, spec)
    for pool, nodes in sorted(ctx.pools().items()):
        if pool in current_pools or not pool_label:
            continue
        v = copy.deepcopy(spec)
        v["node_selector"] = {**(v.get("node_selector") or {}), pool_label: pool}
        for k in list(v["node_selector"]):
            if k != pool_label and any(ctx.state.nodes[n].labels.get(k) != v["node_selector"][k] for n in nodes):
                v["node_selector"].pop(k)
        taints = {(t.get("key"), t.get("value"), t.get("effect")) for n in nodes for t in ctx.state.nodes[n].taints}
        v["tolerations"] = (v.get("tolerations") or []) + [
            {"key": k, "operator": "Equal", "value": val, "effect": e} for k, val, e in sorted(taints, key=str)]
        o = try_variant("pool", f"nodeSelector {pool_label}={pool}" + (" + tolerate pool taints" if taints else ""), v)
        if o:
            options.append(o)
    # 4. add nodes of a pool type
    for pool, nodes in sorted(ctx.pools().items()):
        if current_pools and pool not in current_pools:
            continue
        tmpl = max(nodes, key=lambda n: (ctx.state.nodes[n].allocatable.get("cpu", 0), n))
        st = ctx.state.clone()
        for k in range(1, max_added_nodes + 1):
            ctx.add_synthetic_node(st, ctx.state.nodes[tmpl], f"{pool}-new-{k}")
            c2 = _ctx_with(ctx, st)
            o = try_variant("add_nodes", f"+{k} node(s) like {tmpl} in pool {pool}", spec, c2)
            if o:
                options.append(o)
                break
            st = st.clone()
    # 5. smaller requests (informational, only valid if usage supports it)
    for factor in (0.9, 0.75, 0.5):
        v = dict(spec, requests_factor=factor)
        o = try_variant("requests", f"requests x{factor} (only if usage metrics confirm)", v)
        if o:
            options.append(o)
            break
    rank = {"strategy": 0, "anti_affinity": 1, "replicas": 2, "pool": 3, "add_nodes": 4, "requests": 5}
    options.sort(key=lambda o: (o["verdict"] != "FITS", rank[o["option"]]))
    return {"original_verdict": base_res["verdict"], "original_failures": sorted(failing), "options": options}


def _scale_q(res: str, q: str, factor: float) -> str:
    from .quantity import to_internal
    v = int(to_internal(res, q) * factor)
    return f"{v}m" if res == "cpu" else str(v)


def _pool_label(ctx) -> str | None:
    if ctx.pool_label:
        return ctx.pool_label
    from .model import POOL_LABEL_CANDIDATES
    for k in POOL_LABEL_CANDIDATES:
        if any(k in n.labels for n in ctx.state.nodes.values()):
            return k
    return None


def _target_pools(ctx, spec: dict) -> set[str]:
    wl, _ = spec_to_workload(spec)
    probe = ctx.make_pod({"name": "p", "namespace": wl.namespace, "labels": {}}, wl.template_spec)
    return {n.pool for n in ctx.state.nodes.values() if ctx.state.node_eligible(probe, n)}


__all__ = ["simulate_admission", "search_remediations", "spec_to_workload", "apply_limitranges"]
