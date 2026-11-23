"""Load a normalized snapshot (produced by tools/collector.py or a test fixture)
into a ClusterState + workload index.

Snapshot schema "k8s-capacity-snapshot/v1" (all quantities as Kubernetes strings):
  meta:            cluster, collected_at, kubernetes_version, sources, pool_label,
                   ipam {mode: podcidr|aws-vpc-cni|static|calico-blocks|unknown, reserved_per_cidr,
                         per_node, instance_ip_limits{type:n}, cluster_pool_size}
  namespaces:      [{name, labels}]
  nodes:           [{name, labels, taints, unschedulable, ready, capacity, allocatable,
                     pod_cidrs, max_pod_ips?, csi_limits{driver:n}, instance_type}]
  pods:            [{metadata, spec, status}]           (subset of v1.Pod)
  workloads:       [{kind, metadata, spec}]             (Deployment/StatefulSet/DaemonSet)
  replicasets:     [{namespace, name, owner_kind, owner_name}]
  pdbs:            [{namespace, name, selector, min_available, max_unavailable, expected_pods}]
  quotas:          [{namespace, name, hard{}, used{}, scopes, scope_selector}]
  limitranges:     [{namespace, name, limits[]}]
  priority_classes:[{name, value, global_default, preemption_policy}]
  runtime_classes: [{name, overhead}]
  storage_classes: [{name, provisioner, volume_binding_mode, allowed_topologies, default}]
  pvs:             [{name, storage_class, capacity, csi_driver, node_affinity_terms, claim, phase, local}]
  pvcs:            [{namespace, name, storage_class, volume_name, size, access_modes, phase}]
  usage:           {source, window, pods{"ns/name": {cpu, memory}}}
  data_gaps:       [str]
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .model import (
    HOSTNAME_LABEL,
    ZONE_LABEL,
    NodeInfo,
    PodShape,
    VolumeContext,
    Workload,
    detect_pool,
    _hostnames_from_terms,
)
from .quantity import CSI_VOLUMES, POD_IPS, parse_resource_list, to_internal

if TYPE_CHECKING:
    from .state import ClusterState

SCHEMA = "k8s-capacity-snapshot/v1"


@dataclass
class Context:
    snapshot: dict
    state: "ClusterState"
    workloads: dict[str, Workload]
    pending: list[PodShape]
    priority_classes: dict[str, dict]
    runtime_classes: dict[str, dict]
    volume_ctx: VolumeContext
    limitranges: list[dict]
    usage: dict[str, dict[str, int]]
    pool_label: str | None
    data_gaps: list[str] = field(default_factory=list)

    def make_pod(self, metadata: dict, spec: dict, status: dict | None = None,
                 new_claims: list[dict] | None = None) -> PodShape:
        pod = PodShape.from_pod(metadata, spec, status, priority_classes=self.priority_classes,
                                runtime_classes=self.runtime_classes, volume_ctx=None)
        self.volume_ctx.apply(pod, spec, new_claims=new_claims)
        return pod

    def add_synthetic_node(self, st, template: NodeInfo, new_name: str) -> list[str]:
        """Add a copy of `template` to `st` together with the DaemonSet pods that would
        land on it (a new node is never empty). Returns the DaemonSets placed."""
        from .selectors import untolerated_taints

        node = template.clone_as(new_name)
        st.add_node(node)
        placed = []
        for wl in self.workloads.values():
            if wl.kind != "DaemonSet" or not wl.pod_keys:
                continue
            md = {"name": f"{wl.name}-{new_name}", "namespace": wl.namespace,
                  "labels": dict(wl.template_metadata.get("labels") or {}),
                  "ownerReferences": [{"kind": "DaemonSet", "name": wl.name, "controller": True}]}
            pod = self.make_pod(md, wl.template_spec)
            pod.workload_key = wl.key
            pod.pinned_node = new_name
            if st.node_level_ok(pod, node) and not untolerated_taints(pod.tolerations, node.taints):
                st.add_pod(pod, new_name)
                placed.append(wl.key)
        return placed

    def pools(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for n in self.state.nodes.values():
            out.setdefault(n.pool, []).append(n.name)
        return out


def load_snapshot(path_or_dict, scoring: str = "LeastAllocated") -> Context:
    from .state import ClusterState

    snap = path_or_dict if isinstance(path_or_dict, dict) else json.loads(Path(path_or_dict).read_text())
    if snap.get("schema", SCHEMA) != SCHEMA:
        raise ValueError(f"unsupported snapshot schema {snap.get('schema')}")
    meta = snap.get("meta") or {}
    gaps = list(snap.get("data_gaps") or [])
    pool_label = meta.get("pool_label")
    ipam = meta.get("ipam") or {"mode": "unknown"}

    nodes = [_node(n, ipam, pool_label, gaps) for n in snap.get("nodes") or []]
    namespaces = {n["name"]: n.get("labels") or {} for n in snap.get("namespaces") or []}
    pcs = {p["name"]: {"value": p.get("value", 0), "globalDefault": p.get("global_default", False),
                       "preemptionPolicy": p.get("preemption_policy")} for p in snap.get("priority_classes") or []}
    rcs = {r["name"]: {"overhead": r.get("overhead")} for r in snap.get("runtime_classes") or []}
    pvs = [_pv(p) for p in snap.get("pvs") or []]
    vctx = VolumeContext(snap.get("pvcs") or [], pvs, [
        {"name": s["name"], "provisioner": s.get("provisioner"), "volume_binding_mode": s.get("volume_binding_mode"),
         "allowedTopologies": s.get("allowed_topologies"), "default": s.get("default", False)}
        for s in snap.get("storage_classes") or []
    ])
    quotas = [{
        "namespace": q["namespace"], "name": q["name"],
        "hard": {k: to_internal(_qres(k), v) for k, v in (q.get("hard") or {}).items()},
        "used": {k: to_internal(_qres(k), v) for k, v in (q.get("used") or {}).items()},
        "scopes": q.get("scopes") or [], "scope_selector": q.get("scope_selector"),
    } for q in snap.get("quotas") or []]
    local_pvs = []
    for p in pvs:
        if p.get("local") and p.get("phase", "Available") == "Available" and not p.get("claim"):
            hosts = _hostnames_from_terms(p.get("node_affinity_terms") or [])
            node = _node_by_hostname(nodes, hosts[0]) if hosts else None
            if node:
                local_pvs.append({"name": p["name"], "node": node, "storage_class": p.get("storage_class"),
                                  "size": to_internal("storage", p.get("capacity") or "0")})

    state = ClusterState(nodes, namespaces, pdbs=snap.get("pdbs") or [], quotas=quotas,
                         local_pvs=local_pvs, scoring=scoring)
    ctx = Context(snapshot=snap, state=state, workloads={}, pending=[], priority_classes=pcs,
                  runtime_classes=rcs, volume_ctx=vctx, limitranges=snap.get("limitranges") or [],
                  usage={}, pool_label=pool_label, data_gaps=gaps)

    # local PV hostnames -> node names
    host_to_node = {n.labels.get(HOSTNAME_LABEL, n.name): n.name for n in nodes}

    rs_owner = {f"{r['namespace']}/{r['name']}": (r.get("owner_kind"), r.get("owner_name"))
                for r in snap.get("replicasets") or []}
    for raw in snap.get("workloads") or []:
        wl = _workload(raw)
        ctx.workloads[wl.key] = wl

    for raw in snap.get("pods") or []:
        md, spec, status = raw.get("metadata") or {}, raw.get("spec") or {}, raw.get("status") or {}
        phase = status.get("phase", "Running" if spec.get("nodeName") else "Pending")
        if phase in ("Succeeded", "Failed") or md.get("deletionTimestamp"):
            continue  # terminal / terminating pods do not hold requests
        pod = ctx.make_pod(md, spec, status)
        if pod.local_pv_node:
            pod.local_pv_node = host_to_node.get(pod.local_pv_node, pod.local_pv_node)
        pod.workload_key = _workload_key(md, rs_owner, ctx.workloads)
        if pod.workload_key in ctx.workloads:
            ctx.workloads[pod.workload_key].pod_keys.append(pod.key)
        node = spec.get("nodeName")
        if node and node in state.nodes:
            state.add_pod(pod, node)
        elif node:
            gaps.append(f"pod {pod.key} bound to unknown node {node}; ignored")
        else:
            ctx.pending.append(pod)

    us = snap.get("usage") or {}
    for k, v in (us.get("pods") or {}).items():
        ctx.usage[k] = {r: to_internal(r, q) for r, q in v.items() if q is not None}
    if not us.get("pods") and not any("usage" in g for g in gaps):
        gaps.append("no usage metrics: usage-vs-requests risks not evaluated")
    return ctx


def _qres(key: str) -> str:
    k = key.split(".", 1)[1] if key.startswith(("requests.", "limits.")) else key
    if k.endswith("storageclass.storage.k8s.io/requests.storage"):
        return "storage"
    return k


def _node(raw: dict, ipam: dict, pool_label: str | None, gaps: list[str]) -> NodeInfo:
    labels = raw.get("labels") or {}
    alloc = parse_resource_list(raw.get("allocatable"))
    cap = parse_resource_list(raw.get("capacity"))
    ips, src = _ip_capacity(raw, ipam)
    if ips is not None:
        alloc[POD_IPS] = ips
    for driver, n in (raw.get("csi_limits") or {}).items():
        if n is not None:
            alloc[f"{CSI_VOLUMES}/{driver}"] = int(n)
    return NodeInfo(
        name=raw["name"], labels=labels, taints=raw.get("taints") or [], allocatable=alloc, capacity=cap,
        unschedulable=bool(raw.get("unschedulable")), ready=raw.get("ready", True),
        pool=detect_pool(labels, pool_label), zone=labels.get(ZONE_LABEL), ip_capacity_source=src,
        csi_limits=dict(raw.get("csi_limits") or {}),
        csi_known=bool(raw.get("csi_limits")) or bool(raw.get("csi_node_read")),
    )


def _ip_capacity(raw: dict, ipam: dict) -> tuple[int | None, str]:
    if raw.get("max_pod_ips") is not None:
        return int(raw["max_pod_ips"]), raw.get("ip_source", "snapshot")
    mode = ipam.get("mode", "unknown")
    if mode == "podcidr":
        total = 0
        for cidr in raw.get("pod_cidrs") or []:
            net = ipaddress.ip_network(cidr, strict=False)
            if net.version == 4:
                total += net.num_addresses - int(ipam.get("reserved_per_cidr", 3))
        if total:
            return total, f"podCIDR {','.join(raw.get('pod_cidrs'))} minus {ipam.get('reserved_per_cidr', 3)} reserved"
        return None, "podcidr mode but node has no podCIDR"
    if mode == "aws-vpc-cni":
        itype = raw.get("instance_type") or (raw.get("labels") or {}).get("node.kubernetes.io/instance-type")
        limit = (ipam.get("instance_ip_limits") or {}).get(itype)
        if limit is not None:
            return int(limit), f"aws-vpc-cni table for {itype}"
        return None, f"aws-vpc-cni: no IP limit configured for {itype}"
    if mode == "static" and ipam.get("per_node"):
        return int(ipam["per_node"]), "static per-node limit from config"
    return None, f"ipam mode {mode}: per-node IP limit not known"


def _pv(raw: dict) -> dict:
    return dict(raw)


def _node_by_hostname(nodes: list[NodeInfo], host: str) -> str | None:
    for n in nodes:
        if n.labels.get(HOSTNAME_LABEL, n.name) == host or n.name == host:
            return n.name
    return None


def _workload(raw: dict) -> Workload:
    md, spec = raw.get("metadata") or {}, raw.get("spec") or {}
    kind = raw["kind"]
    strat = spec.get("strategy") or spec.get("updateStrategy") or {}
    stype = strat.get("type") or "RollingUpdate"
    ru = strat.get("rollingUpdate") or {}
    if kind == "Deployment":
        surge, unav = ru.get("maxSurge", "25%"), ru.get("maxUnavailable", "25%")
    elif kind == "StatefulSet":
        surge, unav = 0, ru.get("maxUnavailable", 1)
    else:  # DaemonSet
        surge, unav = ru.get("maxSurge", 0), ru.get("maxUnavailable", 1)
    tmpl = spec.get("template") or {}
    return Workload(
        kind=kind, namespace=md.get("namespace") or "default", name=md["name"],
        replicas=int(spec.get("replicas", 1)) if kind != "DaemonSet" else 0,
        strategy_type=stype, max_surge=surge, max_unavailable=unav,
        template_metadata=tmpl.get("metadata") or {}, template_spec=tmpl.get("spec") or {},
        selector=spec.get("selector"), volume_claim_templates=spec.get("volumeClaimTemplates") or [],
    )


def _workload_key(md: dict, rs_owner: dict, workloads: dict) -> str | None:
    ns = md.get("namespace") or "default"
    owners = md.get("ownerReferences") or []
    ctrl = next((o for o in owners if o.get("controller")), owners[0] if owners else None)
    if not ctrl:
        return None
    kind, name = ctrl.get("kind"), ctrl.get("name")
    if kind == "ReplicaSet":
        ok, on = rs_owner.get(f"{ns}/{name}", (None, None))
        if ok:
            return f"{ok}/{ns}/{on}"
        guess = name.rsplit("-", 1)[0]
        if f"Deployment/{ns}/{guess}" in workloads:
            return f"Deployment/{ns}/{guess}"
        return f"ReplicaSet/{ns}/{name}"
    return f"{kind}/{ns}/{name}"
