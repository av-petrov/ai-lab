"""Rolling-update simulation and update-reserve analysis.

Each controller is emulated as a step-wise state machine on a shared ClusterState,
so several rollouts can run "in parallel" (round-robin) to model concurrent updates.

Deployment (RollingUpdate): new RS scales up while total pods <= replicas + maxSurge
  (maxSurge rounds up, maxUnavailable rounds down); old RS scales down by
  old + newAvailable - (replicas - maxUnavailable). Pending pods consume quota
  but not node capacity. No progress in a full round == stuck (deadlock).
Deployment (Recreate): delete all old, then create all new (downtime, no reserve).
StatefulSet (RollingUpdate): highest ordinal first, delete-then-create, maxUnavailable
  pods at a time; the new pod keeps the PVCs (local PV pins it to the same node).
DaemonSet (RollingUpdate): maxSurge>0 -> new pod on the same node before the old one is
  deleted (needs per-node headroom); maxSurge=0 -> delete then create.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .model import PodShape, Workload, resolve_int_or_percent
from .quantity import add, fmt_list, scale
from .state import ClusterState


@dataclass
class RolloutResult:
    workload: str
    kind: str
    strategy: str
    replicas: int
    max_surge: int
    max_unavailable: int
    status: str = "PENDING"          # COMPLETED | STUCK | SKIPPED | NOT_APPLICABLE
    stuck_reason: str | None = None
    used_unavailability: bool = False  # progressed only by removing old pods before new ones were placed
    analytic_reserve: dict[str, int] = field(default_factory=dict)
    peak_extra: dict[str, int] = field(default_factory=dict)
    quota_blocked: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "workload": self.workload, "kind": self.kind, "strategy": self.strategy,
            "replicas": self.replicas, "max_surge": self.max_surge, "max_unavailable": self.max_unavailable,
            "status": self.status, "stuck_reason": self.stuck_reason,
            "used_unavailability": self.used_unavailability,
            "analytic_reserve": fmt_list(_clean(self.analytic_reserve)),
            "analytic_reserve_raw": _clean(self.analytic_reserve),
            "simulated_peak_extra": fmt_list(_clean(self.peak_extra)),
            "quota_blocked": self.quota_blocked[:5], "events": self.events[-8:], "notes": self.notes,
        }


def _clean(rl: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in sorted(rl.items()) if v}


class RolloutSim:
    def __init__(self, ctx, wl: Workload, *, new_template_spec: dict | None = None,
                 new_replicas: int | None = None, tag: str = "new", check_quota: bool = True):
        self.ctx = ctx
        self.wl = wl
        self.spec = copy.deepcopy(new_template_spec if new_template_spec is not None else wl.template_spec)
        self.replicas = wl.replicas if new_replicas is None else new_replicas
        self.tag = tag
        self.check_quota = check_quota
        self.old: list[str] = []
        self.new_running: list[str] = []
        self.new_pending: list[PodShape] = []
        self.counter = 0
        self.finished = False
        self.last_failure: str | None = None
        self.on_change = lambda st: None  # set by run_rollouts to sample the peak after every placement
        kind = wl.kind
        if kind == "Deployment":
            surge = resolve_int_or_percent(wl.max_surge, self.replicas, True, "25%")
            unav = resolve_int_or_percent(wl.max_unavailable, self.replicas, False, "25%")
            if wl.strategy_type == "Recreate":
                surge, unav = 0, self.replicas
        elif kind == "StatefulSet":
            surge, unav = 0, max(1, resolve_int_or_percent(wl.max_unavailable, self.replicas, False, 1))
        elif kind == "DaemonSet":
            nodes = max(1, len(wl.pod_keys))
            surge = resolve_int_or_percent(wl.max_surge, nodes, True, 0)
            unav = resolve_int_or_percent(wl.max_unavailable, nodes, False, 1)
        else:
            surge, unav = 0, 0
        self.res = RolloutResult(wl.key, kind, wl.strategy_type, self.replicas, surge, unav)
        if kind == "Deployment" and wl.strategy_type == "RollingUpdate" and surge == 0 and unav == 0:
            self.res.status, self.res.stuck_reason = "STUCK", "maxSurge and maxUnavailable are both 0 (invalid spec)"
            self.finished = True

    # -------------------------------------------------------------- pods
    def _new_pod(self, ordinal: int | None = None, node: str | None = None) -> PodShape:
        wl = self.wl
        labels = dict(wl.template_metadata.get("labels") or {})
        spec = copy.deepcopy(self.spec)
        new_claims = None
        if wl.kind == "StatefulSet":
            name = f"{wl.name}-{ordinal}"
            new_claims = []
            spec.setdefault("volumes", [])
            for vct in wl.volume_claim_templates:
                tname = (vct.get("metadata") or {}).get("name", "data")
                claim = f"{tname}-{wl.name}-{ordinal}"
                if f"{wl.namespace}/{claim}" in self.ctx.volume_ctx.pvcs:
                    spec["volumes"].append({"name": tname, "persistentVolumeClaim": {"claimName": claim}})
                else:
                    vs = vct.get("spec") or {}
                    new_claims.append({"name": claim, "storage_class": vs.get("storageClassName"),
                                       "size": ((vs.get("resources") or {}).get("requests") or {}).get("storage"),
                                       "access_modes": vs.get("accessModes")})
            labels["controller-revision-hash"] = self.tag
            owner = "StatefulSet"
        elif wl.kind == "DaemonSet":
            name = f"{wl.name}-{self.tag}-{node}"
            labels["controller-revision-hash"] = self.tag
            owner = "DaemonSet"
        else:
            self.counter += 1
            name = f"{wl.name}-{self.tag}-{self.counter}"
            labels["pod-template-hash"] = self.tag
            owner = "ReplicaSet"
        md = {"name": name, "namespace": wl.namespace, "labels": labels,
              "ownerReferences": [{"kind": owner, "name": wl.name, "controller": True}]}
        pod = self.ctx.make_pod(md, spec, {"phase": "Pending"}, new_claims=new_claims)
        pod.workload_key = wl.key
        if node:
            pod.pinned_node = node
        if wl.kind == "StatefulSet":
            pod.local_pv_node = pod.local_pv_node and self._host_to_node(pod.local_pv_node)
        return pod

    def _host_to_node(self, host: str) -> str:
        for n in self.ctx.state.nodes.values():
            if n.labels.get("kubernetes.io/hostname") == host:
                return n.name
        return host

    def _create(self, st: ClusterState, pod: PodShape) -> bool:
        """Returns False if quota rejects creation."""
        if self.check_quota:
            v = st.quota.check(pod)
            if v:
                self.res.quota_blocked.extend(v)
                self.last_failure = f"quota: {v[0]}"
                return False
        st.quota.charge(pod, +1)
        return True

    def _try_place(self, st: ClusterState, pod: PodShape) -> bool:
        pl = st.schedule(pod)
        if pl.ok:
            st.add_pod(pod, pl.node)
            self.res.events.append(f"{pod.name} -> {pl.node}")
            self.on_change(st)
            return True
        self.last_failure = f"{pod.name}: {pl.summary()}"
        return False

    def _delete(self, st: ClusterState, key: str) -> None:
        st.remove_pod(key, refund_quota=True)
        self.res.events.append(f"deleted {key.split('/')[1]}")

    # -------------------------------------------------------------- lifecycle
    def start(self, st: ClusterState) -> None:
        self.old = sorted(k for k, p in st.pods.items() if p.workload_key == self.wl.key)
        tmpl_pod = self._new_pod(ordinal=0, node=None)
        self.counter = 0
        r = {k: v for k, v in tmpl_pod.requests.items()}
        if self.wl.kind == "Deployment":
            self.res.analytic_reserve = scale(r, self.res.max_surge)
        elif self.wl.kind == "DaemonSet":
            self.res.analytic_reserve = scale(r, self.res.max_surge)
            if self.res.max_surge:
                self.res.notes.append("maxSurge DaemonSet: every node needs headroom for one extra pod "
                                      "while its pod is being replaced")
        elif self.wl.kind == "StatefulSet" and self.old:
            old_req = st.pods[self.old[0]].requests
            self.res.analytic_reserve = {k: max(0, v - old_req.get(k, 0)) * self.res.max_unavailable
                                         for k, v in r.items()}
        if self.wl.strategy_type == "OnDelete":
            self.res.status = "NOT_APPLICABLE"
            self.res.notes.append("OnDelete: pods are replaced manually; no automatic rollout to simulate")
            self.finished = True
        if self.wl.strategy_type == "Recreate":
            self.res.notes.append("Recreate: no surge reserve, but the application is down during the update")
        if self.wl.kind == "DaemonSet":
            self.ds_nodes = [st.pod_node[k] for k in self.old]
            self.ds_done: list[str] = []
            self.ds_waiting: dict[str, PodShape] = {}
            self.ds_placed: list[str] = []
        if self.wl.kind == "StatefulSet":
            self.sts_queue = sorted(self.old, key=lambda k: _ordinal(k), reverse=True)
            self.sts_waiting: list[PodShape] = []
            existing = {_ordinal(k) for k in self.old}
            for i in range(self.replicas):  # scale-up ordinals
                if i not in existing:
                    self.sts_waiting.append(self._new_pod(ordinal=i))
            for k in list(self.old):  # scale-down ordinals
                if _ordinal(k) >= self.replicas:
                    self._delete(st, k)
                    self.old.remove(k)
                    self.sts_queue.remove(k)

    def step(self, st: ClusterState, phase: str) -> bool:
        """phase 'up': create/place new pods; phase 'down': delete old pods.
        run_rollouts runs 'up' for every active rollout before any 'down', so surges of
        concurrent rollouts overlap in time the way they do in a real cluster."""
        if self.finished:
            return False
        kind, strat = self.wl.kind, self.wl.strategy_type
        if kind == "Deployment" and strat == "Recreate":
            return self._recreate_up(st) if phase == "up" else self._recreate_down(st)
        if kind == "Deployment":
            return self._deploy_up(st) if phase == "up" else self._deploy_down(st)
        if kind == "StatefulSet":
            return self._sts_up(st) if phase == "up" else self._sts_down(st)
        if kind == "DaemonSet":
            return self._ds_up(st) if phase == "up" else self._ds_down(st)
        self.res.status, self.finished = "NOT_APPLICABLE", True
        return False

    def _finish(self, ok: bool):
        self.finished = True
        self.res.status = "COMPLETED" if ok else "STUCK"
        if not ok:
            self.res.stuck_reason = self.last_failure

    # ---------------------------------------------------------- Deployment / RollingUpdate
    def _deploy_up(self, st: ClusterState) -> bool:
        R, surge = self.replicas, self.res.max_surge
        progressed = False
        for p in list(self.new_pending):
            if self._try_place(st, p):
                self.new_pending.remove(p)
                self.new_running.append(p.key)
                progressed = True
        while (len(self.old) + len(self.new_running) + len(self.new_pending) < R + surge
               and len(self.new_running) + len(self.new_pending) < R):
            p = self._new_pod()
            if not self._create(st, p):
                break
            progressed = True
            if self._try_place(st, p):
                self.new_running.append(p.key)
            else:
                self.new_pending.append(p)
        return progressed

    def _deploy_down(self, st: ClusterState) -> bool:
        R, unav = self.replicas, self.res.max_unavailable
        progressed = False
        # maxScaledDown = allPods - minAvailable - newUnavailable = old + newRunning - (R - maxUnavailable)
        can_delete = min(len(self.old), len(self.old) + len(self.new_running) - (R - unav))
        if len(self.new_running) >= R:
            can_delete = len(self.old)
        if can_delete > 0:
            if self.new_pending:
                self.res.used_unavailability = True
            for k in self._deletion_order(st)[:can_delete]:
                self._delete(st, k)
                self.old.remove(k)
            progressed = True
        if not self.old and not self.new_pending and len(self.new_running) >= R:
            self._finish(True)
        return progressed

    def _deletion_order(self, st: ClusterState) -> list[str]:
        """ReplicaSet controller ranking: old pods on nodes with more pods of the same
        Deployment (old + new ReplicaSets) go first, then by name."""
        per_node: dict[str, int] = {}
        for k in self.old + self.new_running:
            per_node[st.pod_node[k]] = per_node.get(st.pod_node[k], 0) + 1
        return sorted(self.old, key=lambda k: (-per_node[st.pod_node[k]], k))

    # ---------------------------------------------------------- Deployment / Recreate
    def _recreate_down(self, st: ClusterState) -> bool:
        if not self.old:
            return False
        for k in list(self.old):
            self._delete(st, k)
        self.old = []
        return True

    def _recreate_up(self, st: ClusterState) -> bool:
        if self.old:
            return False
        progressed = False
        while len(self.new_running) + len(self.new_pending) < self.replicas:
            p = self._new_pod()
            if not self._create(st, p):
                break
            self.new_pending.append(p)
            progressed = True
        for p in list(self.new_pending):
            if self._try_place(st, p):
                self.new_pending.remove(p)
                self.new_running.append(p.key)
                progressed = True
        if len(self.new_running) >= self.replicas:
            self._finish(True)
        return progressed

    # ---------------------------------------------------------- StatefulSet
    def _sts_up(self, st: ClusterState) -> bool:
        progressed = False
        for p in list(self.sts_waiting):
            if not getattr(p, "_charged", False):
                if not self._create(st, p):
                    continue
                p._charged = True  # type: ignore[attr-defined]
            if self._try_place(st, p):
                self.sts_waiting.remove(p)
                progressed = True
        if not self.sts_queue and not self.sts_waiting:
            self._finish(True)
        return progressed

    def _sts_down(self, st: ClusterState) -> bool:
        if self.sts_waiting:
            return False  # OrderedReady: wait until replaced pods are Running
        progressed = False
        for k in self.sts_queue[: self.res.max_unavailable]:
            self.sts_queue.remove(k)
            self._delete(st, k)
            self.old.remove(k)
            self.sts_waiting.append(self._new_pod(ordinal=_ordinal(k)))
            progressed = True
        if not self.sts_queue and not self.sts_waiting:
            self._finish(True)
        return progressed

    # ---------------------------------------------------------- DaemonSet
    def _old_on(self, st: ClusterState, node: str) -> str | None:
        return next((k for k in self.old if st.pod_node.get(k) == node), None)

    def _ds_up(self, st: ClusterState) -> bool:
        progressed = False
        for node, p in list(self.ds_waiting.items()):
            if self._try_place(st, p):
                del self.ds_waiting[node]
                self.ds_placed.append(node)
                progressed = True
        if self.res.max_surge:
            budget = self.res.max_surge - len(self.ds_waiting) - len(self.ds_placed)
            todo = [n for n in self.ds_nodes if n not in self.ds_done and n not in self.ds_waiting
                    and n not in self.ds_placed]
            for node in todo[: max(0, budget)]:
                p = self._new_pod(node=node)
                self._create(st, p)
                progressed = True
                if self._try_place(st, p):
                    self.ds_placed.append(node)
                else:
                    self.ds_waiting[node] = p
        return progressed

    def _ds_down(self, st: ClusterState) -> bool:
        progressed = False
        for node in list(self.ds_placed):  # new pod running -> remove the old one on that node
            old = self._old_on(st, node)
            if old:
                self._delete(st, old)
                self.old.remove(old)
            self.ds_placed.remove(node)
            self.ds_done.append(node)
            progressed = True
        if not self.res.max_surge:
            budget = self.res.max_unavailable - len(self.ds_waiting)
            todo = [n for n in self.ds_nodes if n not in self.ds_done and n not in self.ds_waiting]
            for node in todo[: max(0, budget)]:
                old = self._old_on(st, node)
                if old:
                    self._delete(st, old)
                    self.old.remove(old)
                p = self._new_pod(node=node)
                self._create(st, p)
                self.ds_waiting[node] = p
                progressed = True
        if len(self.ds_done) == len(self.ds_nodes) and not self.ds_waiting:
            self._finish(True)
        return progressed

    def stall(self):
        if not self.finished:
            self._finish(False)


def _ordinal(key: str) -> int:
    try:
        return int(key.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return -1


# =========================================================================== runners
def run_rollouts(base: ClusterState, sims: list[RolloutSim], max_rounds: int = 500) -> dict:
    st = base.clone()
    for s in sims:
        s.start(st)
    baseline = st.total_requests()
    peak: dict[str, int] = {}
    own_base = {id(s): _wl_requests(st, s.wl.key) for s in sims}

    def sampler(sim):
        def record(state):
            for r, v in add(state.total_requests(), baseline, -1).items():
                peak[r] = max(peak.get(r, 0), v)
            for r, v in add(_wl_requests(state, sim.wl.key), own_base[id(sim)], -1).items():
                sim.res.peak_extra[r] = max(sim.res.peak_extra.get(r, 0), v)
        return record

    for s in sims:
        s.on_change = sampler(s)
    for _ in range(max_rounds):
        active = [s for s in sims if not s.finished]
        if not active:
            break
        moved = False
        for s in active:
            moved |= s.step(st, "up")
        for s in active:
            moved |= s.step(st, "down")
        if not moved:
            break
    for s in sims:
        s.stall()
    return {"state": st, "peak_extra": peak, "results": [s.res for s in sims]}


def _wl_requests(st: ClusterState, wl_key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in st.pods.values():
        if p.workload_key == wl_key:
            out = add(out, p.requests)
    return out


def rollout_workload(ctx, base: ClusterState, wl: Workload, **kw) -> RolloutResult:
    return run_rollouts(base, [RolloutSim(ctx, wl, **kw)])["results"][0]


# =========================================================================== static checks
def static_deadlocks(ctx, base: ClusterState, wl: Workload, template_spec: dict | None = None,
                     replicas: int | None = None) -> list[str]:
    """Explicit, explainable rule checks (the simulation catches the rest)."""
    if wl.kind != "Deployment" or wl.strategy_type != "RollingUpdate":
        return []
    R = wl.replicas if replicas is None else replicas
    unav = resolve_int_or_percent(wl.max_unavailable, R, False, "25%")
    if unav > 0:
        return []
    spec = template_spec if template_spec is not None else wl.template_spec
    probe = ctx.make_pod({"name": f"{wl.name}-probe", "namespace": wl.namespace,
                          "labels": dict(wl.template_metadata.get("labels") or {})}, spec)
    out = []
    for term in probe.pod_anti_affinity:
        if not base._term_selects(term, probe, probe):
            continue
        key = term["topologyKey"]
        domains = {n.labels[key] for n in base.nodes.values()
                   if key in n.labels and n.ready and not n.unschedulable and base.node_eligible(probe, n)}
        if R >= len(domains):
            out.append(
                f"{wl.key}: required podAntiAffinity on {key} (one pod per domain) with replicas={R} "
                f">= eligible domains={len(domains)} and maxUnavailable=0: the surge pod has no domain to go to")
    return out


# =========================================================================== reserve analysis
def update_groups(workloads: list[Workload], model: str, custom: list[list[str]] | None = None) -> list[list[Workload]]:
    if custom:
        idx = {w.key: w for w in workloads}
        return [[idx[k] for k in g if k in idx] for g in custom]
    if model == "sequential":
        return [[w] for w in workloads]
    if model == "per_namespace":
        groups: dict[str, list[Workload]] = {}
        for w in workloads:
            groups.setdefault(w.namespace, []).append(w)
        return list(groups.values())
    if model == "all":
        return [list(workloads)]
    raise ValueError(f"unknown concurrency model {model}")


def reserve_analysis(ctx, base: ClusterState, model: str = "sequential",
                     custom_groups: list[list[str]] | None = None,
                     include: list[str] | None = None) -> dict:
    wls = [w for w in ctx.workloads.values()
           if w.kind in ("Deployment", "StatefulSet", "DaemonSet") and (include is None or w.key in include)
           and any(base.pods.get(k) for k in w.pod_keys)]
    wls.sort(key=lambda w: w.key)
    groups = update_groups(wls, model, custom_groups)
    group_results = []
    required: dict[str, int] = {}
    per_workload = []
    for g in groups:
        out = run_rollouts(base, [RolloutSim(ctx, w, tag="upd") for w in g])
        peak = out["peak_extra"]
        for r, v in peak.items():
            required[r] = max(required.get(r, 0), v)
        analytic = {}
        for res in out["results"]:
            analytic = add(analytic, res.analytic_reserve)
        stuck = [r for r in out["results"] if r.status == "STUCK"]
        group_results.append({
            "group": [w.key for w in g],
            "feasible": not stuck,
            "analytic_reserve": fmt_list(_clean(analytic)),
            "simulated_peak_extra": fmt_list(_clean(peak)),
            "stuck": [{"workload": r.workload, "reason": r.stuck_reason} for r in stuck],
            "degraded": [r.workload for r in out["results"] if r.used_unavailability],
        })
        if len(g) == 1:
            per_workload.append(out["results"][0].to_dict())
        else:
            per_workload.extend(r.to_dict() for r in out["results"])
    deadlocks = []
    for w in wls:
        deadlocks.extend(static_deadlocks(ctx, base, w))
    analytic_required = {}
    # analytic required reserve by model: max over groups of sum within group
    dominated_by: dict[str, list[str]] = {}
    for g in groups:
        s: dict[str, int] = {}
        for w in g:
            res = next((p for p in per_workload if p["workload"] == w.key), None)
            if res:
                s = add(s, res["analytic_reserve_raw"])
        for r, v in s.items():
            if v > analytic_required.get(r, 0):
                analytic_required[r] = v
                dominated_by[r] = [w.key for w in g]
    return {
        "concurrency_model": model,
        "groups_total": len(group_results),
        "groups_infeasible": [g for g in group_results if not g["feasible"]],
        "groups_degraded": [g for g in group_results if g["degraded"] and g["feasible"]],
        "required_reserve_analytic": fmt_list(_clean(analytic_required)),
        "required_reserve_simulated_peak": fmt_list(_clean(required)),
        "required_reserve_raw": _clean(analytic_required),
        "dominated_by": {r: (v if len(v) <= 3 else v[:3] + [f"... {len(v) - 3} more"])
                         for r, v in dominated_by.items() if r in ("cpu", "memory")},
        "static_deadlocks": deadlocks,
        "per_workload": per_workload,
        "method": "analytic = ceil(maxSurge x replicas) x pod requests (max over groups of the sum inside a group); "
                  "simulated = controller emulation on the real node layout, peak of extra requests",
    }


def per_node_ds_headroom(base: ClusterState, ctx) -> list[dict]:
    """Nodes where a maxSurge DaemonSet cannot place its surge pod."""
    out = []
    for w in ctx.workloads.values():
        if w.kind != "DaemonSet" or not resolve_int_or_percent(w.max_surge, 1, True, 0):
            continue
        for k in w.pod_keys:
            node = base.pod_node.get(k)
            if node is None:
                continue
            req = base.pods[k].requests
            free = base.free(node)
            short = [r for r, v in req.items() if r in free and v > free[r]]
            if short:
                out.append({"daemonset": w.key, "node": node, "short": short})
    return out
