"""Read-only collector: Kubernetes API (+ optional Prometheus / metrics-server) -> snapshot JSON.

Only list/get calls are used. Minimal RBAC: see k8s/capacity-reader-rbac.yaml.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

log = logging.getLogger(__name__)

POD_SPEC_FIELDS = (
    "nodeName", "containers", "initContainers", "overhead", "runtimeClassName", "nodeSelector", "affinity",
    "tolerations", "topologySpreadConstraints", "priority", "priorityClassName", "preemptionPolicy",
    "hostNetwork", "volumes",
)
CONTAINER_FIELDS = ("name", "resources", "ports", "restartPolicy")


def _trim_pod_spec(spec: dict) -> dict:
    out = {k: spec[k] for k in POD_SPEC_FIELDS if spec.get(k) not in (None, [], {})}
    for key in ("containers", "initContainers"):
        if key in out:
            out[key] = [{k: c[k] for k in CONTAINER_FIELDS if c.get(k)} for c in out[key]]
    if "volumes" in out:
        out["volumes"] = [v for v in out["volumes"] if v.get("persistentVolumeClaim")]
    return out


def _md(md: dict) -> dict:
    return {k: md.get(k) for k in ("name", "namespace", "uid", "labels", "annotations", "ownerReferences",
                                   "creationTimestamp", "deletionTimestamp") if md.get(k) is not None}


def _list(fn, *args, **kwargs) -> list:
    items, cont = [], None
    while True:
        resp = fn(*args, _continue=cont, limit=500, **kwargs) if cont else fn(*args, limit=500, **kwargs)
        items.extend(resp.items)
        cont = resp.metadata._continue
        if not cont:
            return items


def collect_snapshot(*, cluster_name: str = "cluster", kube_context: str | None = None,
                     prometheus_url: str | None = None, ipam: dict | None = None,
                     pool_label: str | None = None, usage_window: str = "7d") -> dict:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=kube_context)
    api = client.ApiClient()
    ser = api.sanitize_for_serialization
    core, apps, policy = client.CoreV1Api(api), client.AppsV1Api(api), client.PolicyV1Api(api)
    sched, storage, node_api = client.SchedulingV1Api(api), client.StorageV1Api(api), client.NodeV1Api(api)
    gaps: list[str] = []

    version = client.VersionApi(api).get_code().git_version

    # -------------------------------------------------------------- nodes (+ CSINode limits)
    csi_limits: dict[str, dict[str, int]] = {}
    csi_read = False
    try:
        # raw JSON: the typed client model rejects CSINodes with `drivers: null` (no CSI drivers, e.g. kind)
        import json as _json

        raw = storage.list_csi_node(_preload_content=False)
        csi_read = True
        for d in _json.loads(raw.data).get("items", []):
            csi_limits[d["metadata"]["name"]] = {
                drv["name"]: (drv.get("allocatable") or {}).get("count")
                for drv in (d.get("spec") or {}).get("drivers") or [] if (drv.get("allocatable") or {}).get("count")
            }
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"CSINode not readable ({exc.__class__.__name__}): CSI attach limits not checked")

    cilium_cidrs = _cilium_pod_cidrs(api, gaps) if (ipam or {}).get("mode") == "cilium-cluster-pool" else {}

    nodes = []
    for n in _list(core.list_node):
        d = ser(n)
        md, spec, st = d["metadata"], d.get("spec") or {}, d.get("status") or {}
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in st.get("conditions") or [])
        cidrs = spec.get("podCIDRs") or ([spec["podCIDR"]] if spec.get("podCIDR") else [])
        entry = {
            "name": md["name"], "labels": md.get("labels") or {}, "taints": spec.get("taints") or [],
            "unschedulable": bool(spec.get("unschedulable")), "ready": ready,
            "capacity": st.get("capacity") or {}, "allocatable": st.get("allocatable") or {},
            "pod_cidrs": cilium_cidrs.get(md["name"], cidrs),
            "csi_limits": csi_limits.get(md["name"], {}),
            "csi_node_read": csi_read,
            "instance_type": (md.get("labels") or {}).get("node.kubernetes.io/instance-type"),
        }
        nodes.append(entry)
    effective_ipam = dict(ipam or {"mode": "unknown"})
    if effective_ipam.get("mode") == "cilium-cluster-pool":
        effective_ipam["mode"] = "podcidr"

    # -------------------------------------------------------------- pods and controllers
    pods = []
    for p in _list(core.list_pod_for_all_namespaces):
        d = ser(p)
        pods.append({"metadata": _md(d["metadata"]), "spec": _trim_pod_spec(d.get("spec") or {}),
                     "status": {"phase": (d.get("status") or {}).get("phase")}})

    workloads = []
    for kind, fn in (("Deployment", apps.list_deployment_for_all_namespaces),
                     ("StatefulSet", apps.list_stateful_set_for_all_namespaces),
                     ("DaemonSet", apps.list_daemon_set_for_all_namespaces)):
        for o in _list(fn):
            d = ser(o)
            spec = d.get("spec") or {}
            tmpl = spec.get("template") or {}
            workloads.append({
                "kind": kind, "metadata": _md(d["metadata"]),
                "spec": {
                    "replicas": spec.get("replicas"), "selector": spec.get("selector"),
                    "strategy": spec.get("strategy"), "updateStrategy": spec.get("updateStrategy"),
                    "template": {"metadata": {"labels": (tmpl.get("metadata") or {}).get("labels") or {}},
                                 "spec": _trim_pod_spec(tmpl.get("spec") or {})},
                    "volumeClaimTemplates": spec.get("volumeClaimTemplates") or [],
                },
            })
    replicasets = []
    for o in _list(apps.list_replica_set_for_all_namespaces):
        d = ser(o)
        own = next((r for r in d["metadata"].get("ownerReferences") or [] if r.get("controller")), None)
        replicasets.append({"namespace": d["metadata"]["namespace"], "name": d["metadata"]["name"],
                            "owner_kind": own and own["kind"], "owner_name": own and own["name"]})

    # -------------------------------------------------------------- policy objects
    pdbs = []
    for o in _list(policy.list_pod_disruption_budget_for_all_namespaces):
        d = ser(o)
        s, st = d.get("spec") or {}, d.get("status") or {}
        pdbs.append({"namespace": d["metadata"]["namespace"], "name": d["metadata"]["name"],
                     "selector": s.get("selector"), "min_available": s.get("minAvailable"),
                     "max_unavailable": s.get("maxUnavailable"), "expected_pods": st.get("expectedPods"),
                     "disruptions_allowed": st.get("disruptionsAllowed")})
    quotas = []
    for o in _list(core.list_resource_quota_for_all_namespaces):
        d = ser(o)
        s, st = d.get("spec") or {}, d.get("status") or {}
        quotas.append({"namespace": d["metadata"]["namespace"], "name": d["metadata"]["name"],
                       "hard": st.get("hard") or s.get("hard") or {}, "used": st.get("used") or {},
                       "scopes": s.get("scopes") or [], "scope_selector": s.get("scopeSelector")})
    limitranges = [{"namespace": d["metadata"]["namespace"], "name": d["metadata"]["name"],
                    "limits": (d.get("spec") or {}).get("limits") or []}
                   for d in map(ser, _list(core.list_limit_range_for_all_namespaces))]
    pcs = [{"name": d["metadata"]["name"], "value": d.get("value", 0), "global_default": d.get("globalDefault", False),
            "preemption_policy": d.get("preemptionPolicy")} for d in map(ser, _list(sched.list_priority_class))]
    try:
        rcs = [{"name": d["metadata"]["name"], "overhead": d.get("overhead")} for d in map(ser, _list(node_api.list_runtime_class))]
    except Exception:  # noqa: BLE001
        rcs = []
        gaps.append("RuntimeClass not readable: pod overhead for new pods not applied")
    namespaces = [{"name": d["metadata"]["name"], "labels": d["metadata"].get("labels") or {}}
                  for d in map(ser, _list(core.list_namespace))]

    # -------------------------------------------------------------- storage
    scs = []
    for d in map(ser, _list(storage.list_storage_class)):
        ann = d["metadata"].get("annotations") or {}
        scs.append({"name": d["metadata"]["name"], "provisioner": d.get("provisioner"),
                    "volume_binding_mode": d.get("volumeBindingMode", "Immediate"),
                    "allowed_topologies": d.get("allowedTopologies"),
                    "default": ann.get("storageclass.kubernetes.io/is-default-class") == "true"})
    pvs = []
    for d in map(ser, _list(core.list_persistent_volume)):
        s = d.get("spec") or {}
        claim = s.get("claimRef")
        pvs.append({"name": d["metadata"]["name"], "storage_class": s.get("storageClassName"),
                    "capacity": (s.get("capacity") or {}).get("storage"),
                    "csi_driver": (s.get("csi") or {}).get("driver"),
                    "local": bool(s.get("local") or s.get("hostPath")),
                    "node_affinity_terms": ((s.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms"),
                    "claim": f"{claim['namespace']}/{claim['name']}" if claim else None,
                    "phase": (d.get("status") or {}).get("phase")})
    pvcs = []
    for d in map(ser, _list(core.list_persistent_volume_claim_for_all_namespaces)):
        s = d.get("spec") or {}
        pvcs.append({"namespace": d["metadata"]["namespace"], "name": d["metadata"]["name"],
                     "storage_class": s.get("storageClassName"), "volume_name": s.get("volumeName"),
                     "size": ((s.get("resources") or {}).get("requests") or {}).get("storage"),
                     "access_modes": s.get("accessModes") or [], "phase": (d.get("status") or {}).get("phase")})

    # -------------------------------------------------------------- usage
    usage = collect_usage(api, prometheus_url, usage_window, gaps)

    return {
        "schema": "k8s-capacity-snapshot/v1",
        "meta": {"cluster": cluster_name, "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "kubernetes_version": version, "sources": ["kubernetes-api"] + ([usage["source"]] if usage.get("pods") else []),
                 "pool_label": pool_label, "ipam": effective_ipam},
        "namespaces": namespaces, "nodes": nodes, "pods": pods, "workloads": workloads, "replicasets": replicasets,
        "pdbs": pdbs, "quotas": quotas, "limitranges": limitranges, "priority_classes": pcs, "runtime_classes": rcs,
        "storage_classes": scs, "pvs": pvs, "pvcs": pvcs, "usage": usage, "data_gaps": gaps,
    }


def _cilium_pod_cidrs(api, gaps: list[str]) -> dict[str, list[str]]:
    from kubernetes import client
    try:
        objs = client.CustomObjectsApi(api).list_cluster_custom_object("cilium.io", "v2", "ciliumnodes")
        return {o["metadata"]["name"]: ((o.get("spec") or {}).get("ipam") or {}).get("podCIDRs") or []
                for o in objs.get("items", [])}
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"CiliumNode not readable ({exc.__class__.__name__}): per-node pod CIDRs unknown")
        return {}


PROM_QUERIES = {
    # p95 over the window of 5m-rate CPU, and max working set memory
    "cpu": 'quantile_over_time(0.95, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m]))[{w}:5m])',
    "memory": 'max_over_time(sum by (namespace, pod) (container_memory_working_set_bytes{container!="",image!=""})[{w}:5m])',
}


def collect_usage(api, prometheus_url: str | None, window: str, gaps: list[str]) -> dict:
    if prometheus_url:
        try:
            import requests

            pods: dict[str, dict[str, Any]] = {}
            for res, q in PROM_QUERIES.items():
                r = requests.get(f"{prometheus_url.rstrip('/')}/api/v1/query", params={"query": q.replace("{w}", window)},
                                 timeout=60)
                r.raise_for_status()
                for item in r.json()["data"]["result"]:
                    key = f"{item['metric'].get('namespace')}/{item['metric'].get('pod')}"
                    val = float(item["value"][1])
                    pods.setdefault(key, {})[res] = f"{int(val * 1000)}m" if res == "cpu" else str(int(val))
            return {"source": "prometheus", "window": f"{window} (cpu p95, memory max)", "pods": pods}
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"Prometheus query failed ({exc.__class__.__name__}); falling back to metrics-server")
    try:
        from kubernetes import client

        objs = client.CustomObjectsApi(api).list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
        pods = {}
        from k8s_capacity.engine.quantity import to_internal

        for o in objs.get("items", []):
            cpu = sum(to_internal("cpu", c["usage"]["cpu"]) for c in o.get("containers", []))
            mem = sum(to_internal("memory", c["usage"]["memory"]) for c in o.get("containers", []))
            pods[f"{o['metadata']['namespace']}/{o['metadata']['name']}"] = {"cpu": f"{cpu}m", "memory": str(mem)}
        gaps.append("usage from metrics-server is a point-in-time sample, not a peak")
        return {"source": "metrics-server", "window": "instant", "pods": pods}
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"no usage metrics ({exc.__class__.__name__})")
        return {"source": "none", "pods": {}}
