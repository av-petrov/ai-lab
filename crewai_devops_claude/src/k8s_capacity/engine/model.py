"""Normalized in-memory objects built from (subsets of) Kubernetes API objects.

The same constructor (`PodShape.from_pod`) is used for live pods, workload
templates and admission specs, so a template is treated exactly like a pod.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .quantity import CSI_VOLUMES, POD_IPS, parse_resource_list, to_internal

POOL_LABEL_CANDIDATES = (
    "node.kubernetes.io/pool",
    "eks.amazonaws.com/nodegroup",
    "cloud.google.com/gke-nodepool",
    "kubernetes.azure.com/agentpool",
    "karpenter.sh/nodepool",
    "node-pool",
    "pool",
)
ZONE_LABEL = "topology.kubernetes.io/zone"
HOSTNAME_LABEL = "kubernetes.io/hostname"


# --------------------------------------------------------------------------- requests
def _container_requests(c: dict) -> tuple[dict[str, int], dict[str, int]]:
    res = c.get("resources") or {}
    limits = parse_resource_list(res.get("limits"))
    requests = parse_resource_list(res.get("requests"))
    # API defaulting: a limit without a request implies request == limit
    for k, v in limits.items():
        requests.setdefault(k, v)
    return requests, limits


def effective_pod_resources(spec: dict, overhead: dict | None = None) -> tuple[dict[str, int], dict[str, int], str]:
    """Return (requests, limits, qos) the way the scheduler / quota see the pod.

    requests[r] = max(max over init containers (+ sidecars started before them),
                      sum(regular containers) + sum(sidecar init containers)) + overhead[r]
    Sidecars = initContainers with restartPolicy: Always (KEP-753).
    """
    regular_req: dict[str, int] = {}
    regular_lim: dict[str, int] = {}
    has_any = False
    for c in spec.get("containers") or []:
        r, l = _container_requests(c)
        has_any |= bool(r or l)
        for k, v in r.items():
            regular_req[k] = regular_req.get(k, 0) + v
        for k, v in l.items():
            regular_lim[k] = regular_lim.get(k, 0) + v

    sidecar_req: dict[str, int] = {}
    sidecar_lim: dict[str, int] = {}
    init_peak_req: dict[str, int] = {}
    init_peak_lim: dict[str, int] = {}
    for c in spec.get("initContainers") or []:
        r, l = _container_requests(c)
        has_any |= bool(r or l)
        if c.get("restartPolicy") == "Always":
            for k, v in r.items():
                sidecar_req[k] = sidecar_req.get(k, 0) + v
                init_peak_req[k] = max(init_peak_req.get(k, 0), sidecar_req[k])
            for k, v in l.items():
                sidecar_lim[k] = sidecar_lim.get(k, 0) + v
                init_peak_lim[k] = max(init_peak_lim.get(k, 0), sidecar_lim[k])
        else:
            for k in set(r) | set(sidecar_req):
                init_peak_req[k] = max(init_peak_req.get(k, 0), r.get(k, 0) + sidecar_req.get(k, 0))
            for k in set(l) | set(sidecar_lim):
                init_peak_lim[k] = max(init_peak_lim.get(k, 0), l.get(k, 0) + sidecar_lim.get(k, 0))

    def combine(regular, sidecar, init_peak):
        out = {}
        for k in set(regular) | set(sidecar) | set(init_peak):
            out[k] = max(init_peak.get(k, 0), regular.get(k, 0) + sidecar.get(k, 0))
        return out

    requests = combine(regular_req, sidecar_req, init_peak_req)
    limits = combine(regular_lim, sidecar_lim, init_peak_lim)
    for k, v in (overhead or {}).items():
        requests[k] = requests.get(k, 0) + v
        if k in limits:
            limits[k] += v

    # QoS (simplified to the same rules as k8s.io/kubernetes/pkg/apis/core/v1/helper/qos)
    if not has_any:
        qos = "BestEffort"
    else:
        guaranteed = True
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            r, l = _container_requests(c)
            for k in ("cpu", "memory"):
                if k not in l or r.get(k) != l.get(k):
                    guaranteed = False
        qos = "Guaranteed" if guaranteed else "Burstable"
    return requests, limits, qos


# --------------------------------------------------------------------------- objects
@dataclass
class NodeInfo:
    name: str
    labels: dict
    taints: list[dict]
    allocatable: dict[str, int]
    capacity: dict[str, int]
    unschedulable: bool = False
    ready: bool = True
    pool: str = "default"
    zone: str | None = None
    ip_capacity_source: str = "unknown"
    csi_limits: dict[str, int] = field(default_factory=dict)
    csi_known: bool = False          # CSINode was read (an empty driver list is a known "no CSI")
    synthetic: bool = False  # added by a what-if (autoscaler / +N nodes)

    def clone_as(self, name: str) -> "NodeInfo":
        n = copy.deepcopy(self)
        n.name = name
        n.labels = dict(n.labels)
        n.labels[HOSTNAME_LABEL] = name
        n.synthetic = True
        n.unschedulable = False
        n.ready = True
        return n


@dataclass
class PodShape:
    namespace: str
    name: str
    labels: dict
    requests: dict[str, int]
    limits: dict[str, int]
    qos: str
    node_name: str | None = None          # where it runs now (status / spec.nodeName)
    pinned_node: str | None = None        # spec.nodeName set by the user / DS controller affinity
    node_selector: dict = field(default_factory=dict)
    required_node_terms: list[dict] | None = None
    preferred_node_terms: list[dict] = field(default_factory=list)
    pod_affinity: list[dict] = field(default_factory=list)
    pod_anti_affinity: list[dict] = field(default_factory=list)
    pod_affinity_preferred: list[dict] = field(default_factory=list)
    pod_anti_affinity_preferred: list[dict] = field(default_factory=list)
    tolerations: list[dict] = field(default_factory=list)
    spread: list[dict] = field(default_factory=list)
    host_ports: list[tuple[str, str, int]] = field(default_factory=list)
    host_network: bool = False
    priority: int = 0
    priority_class: str | None = None
    preemption_policy: str = "PreemptLowerPriority"
    # volumes
    pvc_names: list[str] = field(default_factory=list)
    volume_node_terms: list[list[dict]] = field(default_factory=list)  # each must match (PV nodeAffinity / SC topology)
    rwo_claims: list[str] = field(default_factory=list)                # ns/claim that must stay on one node
    needs_local_pv: list[dict] = field(default_factory=list)           # [{storage_class, size}] unbound local claims
    local_pv_node: str | None = None                                   # bound to a node-local PV
    # ownership
    owner_kind: str | None = None
    workload_key: str | None = None
    is_daemonset: bool = False
    is_mirror: bool = False
    phase: str = "Running"
    creation: str = ""
    uid: str = ""
    unverified: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def movable(self) -> bool:
        """Will a controller recreate it elsewhere if its node disappears?"""
        return (
            self.owner_kind is not None
            and not self.is_daemonset
            and not self.is_mirror
            and self.local_pv_node is None
        )

    def copy_as(self, name: str, labels: dict | None = None) -> "PodShape":
        p = copy.deepcopy(self)
        p.name = name
        if labels is not None:
            p.labels = labels
        p.node_name = None
        p.uid = ""
        return p

    @classmethod
    def from_pod(
        cls,
        metadata: dict,
        spec: dict,
        status: dict | None = None,
        *,
        priority_classes: dict[str, dict] | None = None,
        runtime_classes: dict[str, dict] | None = None,
        volume_ctx: "VolumeContext | None" = None,
    ) -> "PodShape":
        spec = spec or {}
        status = status or {}
        unverified: list[str] = []

        overhead = parse_resource_list(spec.get("overhead"))
        if not overhead and spec.get("runtimeClassName") and runtime_classes is not None:
            rc = runtime_classes.get(spec["runtimeClassName"]) or {}
            overhead = parse_resource_list(((rc.get("overhead") or {}).get("podFixed")))
        requests, limits, qos = effective_pod_resources(spec, overhead)
        requests["pods"] = 1
        host_network = bool(spec.get("hostNetwork"))
        if not host_network:
            requests[POD_IPS] = 1

        affinity = spec.get("affinity") or {}
        na = affinity.get("nodeAffinity") or {}
        req_terms = None
        if na.get("requiredDuringSchedulingIgnoredDuringExecution"):
            req_terms = na["requiredDuringSchedulingIgnoredDuringExecution"].get("nodeSelectorTerms") or []
        pa = affinity.get("podAffinity") or {}
        paa = affinity.get("podAntiAffinity") or {}

        host_ports = []
        for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for port in c.get("ports") or []:
                if port.get("hostPort"):
                    host_ports.append((port.get("hostIP") or "0.0.0.0", port.get("protocol") or "TCP", int(port["hostPort"])))

        priority = spec.get("priority")
        pc_name = spec.get("priorityClassName")
        preemption_policy = spec.get("preemptionPolicy")
        if priority_classes is not None:
            pc = priority_classes.get(pc_name) if pc_name else next(
                (p for p in priority_classes.values() if p.get("globalDefault")), None
            )
            if pc:
                if priority is None:
                    priority = pc.get("value", 0)
                preemption_policy = preemption_policy or pc.get("preemptionPolicy")
            elif pc_name and priority is None:
                unverified.append(f"priorityClass {pc_name} not found; priority assumed 0")

        owners = metadata.get("ownerReferences") or []
        ctrl = next((o for o in owners if o.get("controller")), owners[0] if owners else None)
        annotations = metadata.get("annotations") or {}

        pod = cls(
            namespace=metadata.get("namespace") or "default",
            name=metadata.get("name") or "unnamed",
            uid=metadata.get("uid") or "",
            labels=dict(metadata.get("labels") or {}),
            requests=requests,
            limits=limits,
            qos=qos,
            node_name=spec.get("nodeName"),
            pinned_node=None,
            node_selector=dict(spec.get("nodeSelector") or {}),
            required_node_terms=req_terms,
            preferred_node_terms=na.get("preferredDuringSchedulingIgnoredDuringExecution") or [],
            pod_affinity=pa.get("requiredDuringSchedulingIgnoredDuringExecution") or [],
            pod_anti_affinity=paa.get("requiredDuringSchedulingIgnoredDuringExecution") or [],
            pod_affinity_preferred=pa.get("preferredDuringSchedulingIgnoredDuringExecution") or [],
            pod_anti_affinity_preferred=paa.get("preferredDuringSchedulingIgnoredDuringExecution") or [],
            tolerations=list(spec.get("tolerations") or []),
            spread=list(spec.get("topologySpreadConstraints") or []),
            host_ports=host_ports,
            host_network=host_network,
            priority=int(priority or 0),
            priority_class=pc_name,
            preemption_policy=preemption_policy or "PreemptLowerPriority",
            owner_kind=ctrl.get("kind") if ctrl else None,
            is_daemonset=bool(ctrl and ctrl.get("kind") == "DaemonSet"),
            is_mirror="kubernetes.io/config.mirror" in annotations,
            phase=status.get("phase") or ("Running" if spec.get("nodeName") else "Pending"),
            creation=metadata.get("creationTimestamp") or "",
            unverified=unverified,
        )
        if volume_ctx is not None:
            volume_ctx.apply(pod, spec)
        elif any(v.get("persistentVolumeClaim") for v in spec.get("volumes") or []):
            pod.unverified.append("PVC topology not evaluated (no volume context)")
        return pod


class VolumeContext:
    """Resolves PVC -> PV/StorageClass topology constraints for a pod."""

    def __init__(self, pvcs: list[dict], pvs: list[dict], storage_classes: list[dict], csi_driver_default: str | None = None):
        self.pvcs = {f"{c['namespace']}/{c['name']}": c for c in pvcs or []}
        self.pvs = {p["name"]: p for p in pvs or []}
        self.scs = {s["name"]: s for s in storage_classes or []}
        self.default_sc = next((s["name"] for s in storage_classes or [] if s.get("default")), None)

    def is_local_sc(self, sc_name: str | None) -> bool:
        sc = self.scs.get(sc_name or "") or {}
        return sc.get("provisioner") == "kubernetes.io/no-provisioner"

    def sc_terms(self, sc_name: str | None) -> list[dict] | None:
        sc = self.scs.get(sc_name or "") or {}
        topo = sc.get("allowedTopologies") or []
        if not topo:
            return None
        terms = []
        for t in topo:
            exprs = [
                {"key": e["key"], "operator": "In", "values": e.get("values") or []}
                for e in t.get("matchLabelExpressions") or []
            ]
            if exprs:
                terms.append({"matchExpressions": exprs})
        return terms or None

    def driver_for(self, pv: dict | None, sc_name: str | None) -> str | None:
        if pv and pv.get("csi_driver"):
            return pv["csi_driver"]
        sc = self.scs.get(sc_name or "") or {}
        prov = sc.get("provisioner")
        return prov if prov and prov != "kubernetes.io/no-provisioner" else None

    def apply(self, pod: PodShape, spec: dict, new_claims: list[dict] | None = None) -> None:
        """new_claims: claims that do not exist yet (admission / StatefulSet templates):
        [{name, storage_class, size, access_modes}]"""
        drivers: dict[str, int] = {}
        for vol in spec.get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if not claim:
                continue
            key = f"{pod.namespace}/{claim}"
            pod.pvc_names.append(key)
            pvc = self.pvcs.get(key)
            if pvc is None:
                pod.unverified.append(f"PVC {key} not found in snapshot")
                continue
            self._apply_claim(pod, key, pvc.get("storage_class"), pvc.get("size"),
                              pvc.get("access_modes") or [], self.pvs.get(pvc.get("volume_name") or ""), drivers)
        for c in new_claims or []:
            key = f"{pod.namespace}/{c['name']}"
            pod.pvc_names.append(key)
            self._apply_claim(pod, key, c.get("storage_class") or self.default_sc, c.get("size"),
                              c.get("access_modes") or ["ReadWriteOnce"], None, drivers)
        for d, n in drivers.items():
            pod.requests[f"{CSI_VOLUMES}/{d}"] = n

    def _apply_claim(self, pod, key, sc_name, size, access_modes, pv, drivers):
        if pv is not None:
            if pv.get("node_affinity_terms"):
                pod.volume_node_terms.append(pv["node_affinity_terms"])
            if pv.get("local"):
                hosts = _hostnames_from_terms(pv.get("node_affinity_terms") or [])
                pod.local_pv_node = hosts[0] if len(hosts) == 1 else pod.local_pv_node
        else:
            sc = self.scs.get(sc_name or "")
            if sc is None:
                pod.unverified.append(f"StorageClass {sc_name!r} of {key} unknown: topology not checked")
            elif self.is_local_sc(sc_name):
                pod.needs_local_pv.append({"claim": key, "storage_class": sc_name,
                                           "size": to_internal("storage", size or "0")})
            else:
                terms = self.sc_terms(sc_name)
                if sc.get("volume_binding_mode") == "WaitForFirstConsumer":
                    if terms:
                        pod.volume_node_terms.append(terms)
                else:
                    pod.unverified.append(
                        f"{key}: StorageClass {sc_name} uses Immediate binding; zone is chosen by the "
                        "provisioner before scheduling and is not simulated")
        if "ReadWriteOnce" in access_modes or "ReadWriteOncePod" in access_modes:
            pod.rwo_claims.append(key)
        drv = self.driver_for(pv, sc_name)
        if drv:
            drivers[drv] = drivers.get(drv, 0) + 1


def _hostnames_from_terms(terms: list[dict]) -> list[str]:
    out = []
    for t in terms:
        for e in t.get("matchExpressions") or []:
            if e.get("key") == HOSTNAME_LABEL and e.get("operator") == "In":
                out.extend(e.get("values") or [])
        for f in t.get("matchFields") or []:
            if f.get("key") == "metadata.name" and f.get("operator") == "In":
                out.extend(f.get("values") or [])
    return out


@dataclass
class Workload:
    kind: str                      # Deployment | StatefulSet | DaemonSet | ReplicaSet | Job | ...
    namespace: str
    name: str
    replicas: int
    strategy_type: str             # RollingUpdate | Recreate | OnDelete
    max_surge: int | str | None
    max_unavailable: int | str | None
    template_metadata: dict
    template_spec: dict
    selector: dict | None
    volume_claim_templates: list[dict] = field(default_factory=list)
    pod_keys: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


def resolve_int_or_percent(value, total: int, round_up: bool, default) -> int:
    """intstr.GetScaledValueFromIntOrPercent: maxSurge rounds up, maxUnavailable down."""
    import math

    if value is None:
        value = default
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.endswith("%"):
        pct = int(s[:-1])
        raw = pct * total / 100
        return math.ceil(raw) if round_up else math.floor(raw)
    return int(s)


def detect_pool(labels: dict, pool_label: str | None = None) -> str:
    if pool_label:
        return labels.get(pool_label, "default")
    for k in POOL_LABEL_CANDIDATES:
        if k in labels:
            return labels[k]
    if "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels:
        return "control-plane"
    return "default"
