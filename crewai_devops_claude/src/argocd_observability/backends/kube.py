"""Mode 1: kube current-context. Reads Argo CD ``Application`` CRs and workloads via the K8s API."""

from __future__ import annotations

from typing import Any

from ..kube_client import KubeReader
from .base import Backend, pod_container_states, pod_health

PLURALS = {
    "Pod": "pods", "Service": "services", "Endpoints": "endpoints", "PersistentVolumeClaim": "persistentvolumeclaims",
    "ConfigMap": "configmaps", "ServiceAccount": "serviceaccounts", "Namespace": "namespaces", "Node": "nodes",
    "Deployment": "deployments", "ReplicaSet": "replicasets", "StatefulSet": "statefulsets",
    "DaemonSet": "daemonsets", "Job": "jobs", "CronJob": "cronjobs", "Ingress": "ingresses",
    "NetworkPolicy": "networkpolicies", "HorizontalPodAutoscaler": "horizontalpodautoscalers",
    "PodDisruptionBudget": "poddisruptionbudgets", "Application": "applications", "AppProject": "appprojects",
    "ApplicationSet": "applicationsets", "Rollout": "rollouts", "CiliumNetworkPolicy": "ciliumnetworkpolicies",
}
DEFAULT_VERSION = {"": "v1", "apps": "v1", "batch": "v1", "networking.k8s.io": "v1", "autoscaling": "v2",
                   "policy": "v1", "argoproj.io": "v1alpha1", "cilium.io": "v2"}
CLUSTER_SCOPED = {"Namespace", "Node"}

# (verb, group, resource, subresource, namespaced?) -> what we want to know
READ_CHECKS = [
    ("list", "argoproj.io", "applications", None, True),
    ("get", "argoproj.io", "applications", None, True),
    ("list", "", "pods", None, False),
    ("get", "", "pods", "log", False),
    ("list", "", "events", None, False),
    ("list", "apps", "replicasets", None, False),
    ("get", "", "services", "proxy", True),
    ("get", "", "pods", "proxy", True),
    ("list", "metrics.k8s.io", "pods", None, False),
]
WRITE_CHECKS = [
    ("get", "", "secrets", None, False),
    ("patch", "argoproj.io", "applications", None, True),
    ("delete", "argoproj.io", "applications", None, True),
    ("create", "", "pods", "exec", False),
    ("delete", "", "pods", None, False),
    ("patch", "apps", "deployments", None, False),
    ("create", "", "pods", "portforward", False),
]


def api_path(kind: str, group: str | None, version: str | None, namespace: str | None, name: str | None = None) -> str:
    group = group or ""
    version = version or DEFAULT_VERSION.get(group, "v1")
    plural = PLURALS.get(kind, kind.lower() + "s")
    base = f"/api/{version}" if not group else f"/apis/{group}/{version}"
    ns = f"/namespaces/{namespace}" if namespace and kind not in CLUSTER_SCOPED else ""
    return f"{base}{ns}/{plural}" + (f"/{name}" if name else "")


class KubeBackend(Backend):
    mode = "kube"

    def __init__(self, kube: KubeReader, argocd_namespace: str = "argocd", max_pods_per_ns: int = 500) -> None:
        super().__init__()
        self.kube = kube
        self.ns = argocd_namespace
        self.max_pods_per_ns = max_pods_per_ns

    def describe(self) -> str:
        return f"kube context '{self.kube.context_name}' (Application CRs in namespace '{self.ns}')"

    # ------------------------------------------------------------------ apps
    def list_applications(self, project: str | None = None, selector: str | None = None) -> list[dict]:
        data = self.kube.get_json(f"/apis/argoproj.io/v1alpha1/namespaces/{self.ns}/applications",
                                  {"labelSelector": selector} if selector else None)
        items = data.get("items", [])
        if project:
            items = [a for a in items if a.get("spec", {}).get("project") == project]
        return items

    def get_application(self, name: str) -> dict:
        return self.kube.get_json(f"/apis/argoproj.io/v1alpha1/namespaces/{self.ns}/applications/{name}")

    # ------------------------------------------------------------------ tree
    def _fetch_tree(self, name: str) -> dict:
        app = self.get_application(name)
        nodes: list[dict] = []
        owners: dict[str, dict] = {}  # uid -> node
        workload_ns: set[str] = set()
        for r in (app.get("status", {}) or {}).get("resources", []) or []:
            node = {"kind": r.get("kind"), "group": r.get("group", ""), "version": r.get("version"),
                    "namespace": r.get("namespace"), "name": r.get("name"),
                    "health": r.get("health") or ({"status": "Healthy"} if r.get("status") == "Synced" else None),
                    "syncStatus": r.get("status")}
            if r.get("kind") in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet"):
                try:
                    obj = self.kube.get_json(api_path(r["kind"], r.get("group"), r.get("version"), r.get("namespace"), r["name"]))
                    node["uid"] = obj["metadata"]["uid"]
                    node["createdAt"] = obj["metadata"].get("creationTimestamp")
                    node["info"] = _workload_info(obj)
                    owners[node["uid"]] = node
                    workload_ns.add(r.get("namespace"))
                except Exception as exc:  # noqa: BLE001 - live object missing (e.g. never synced)
                    node["info"] = [{"name": "Live object", "value": f"not readable: {str(exc)[:80]}"}]
            nodes.append(node)
        for ns in sorted(n for n in workload_ns if n):
            nodes.extend(self._children(ns, owners))
        return {"nodes": nodes}

    def _children(self, ns: str, owners: dict[str, dict]) -> list[dict]:
        out: list[dict] = []
        lists = [("ReplicaSet", "apps"), ("Job", "batch"), ("Pod", "")]
        for kind, group in lists:
            q = {"limit": self.max_pods_per_ns}
            try:
                items = self.kube.get_json(api_path(kind, group, None, ns), q).get("items", [])
            except Exception:  # noqa: BLE001
                continue
            for obj in items:
                md = obj.get("metadata", {})
                refs = [o for o in md.get("ownerReferences", []) or [] if o.get("uid") in owners]
                if not refs:
                    continue
                node: dict[str, Any] = {
                    "kind": kind, "group": group, "version": DEFAULT_VERSION.get(group, "v1"), "namespace": ns,
                    "name": md.get("name"), "uid": md.get("uid"), "createdAt": md.get("creationTimestamp"),
                    "parentRefs": [{"kind": o.get("kind"), "name": o.get("name"), "namespace": ns, "uid": o.get("uid")}
                                   for o in refs],
                }
                if kind == "Pod":
                    status, msg, reason = pod_health(obj)
                    states = pod_container_states(obj)
                    node["health"] = {"status": status, "message": msg}
                    node["images"] = sorted({c.get("image") for c in states if c.get("image")})
                    ready = sum(1 for c in states if c["type"] == "main" and c.get("ready"))
                    total = sum(1 for c in states if c["type"] == "main")
                    node["info"] = [{"name": "Status Reason", "value": reason or ""},
                                    {"name": "Containers", "value": f"{ready}/{total}"},
                                    {"name": "Restart Count", "value": str(sum(c.get("restart_count", 0) for c in states))},
                                    {"name": "Node", "value": (obj.get("spec") or {}).get("nodeName") or ""}]
                    node["containers"] = [{k: c.get(k) for k in ("container", "state", "reason", "exit_code",
                                                                  "restart_count", "last_terminated") if c.get(k) is not None}
                                          for c in states]
                else:
                    node["health"] = _rs_health(obj) if kind == "ReplicaSet" else _job_health(obj)
                    node["info"] = _workload_info(obj)
                    # ReplicaSets scaled to 0 (old revisions) are noise
                    if kind == "ReplicaSet" and not (obj.get("spec") or {}).get("replicas"):
                        continue
                    owners[md.get("uid")] = node
                out.append(node)
        return out

    # ------------------------------------------------------------------ details
    def _fetch_live(self, app: str, node: dict) -> dict:
        return self.kube.get_json(api_path(node["kind"], node.get("group"), node.get("version"),
                                           node.get("namespace"), node["name"]))

    def _fetch_events(self, app: str, node: dict | None) -> list[dict]:
        if node is None:
            fs = f"involvedObject.kind=Application,involvedObject.name={app}"
            ns = self.ns
        else:
            fs = f"involvedObject.kind={node['kind']},involvedObject.name={node['name']}"
            ns = node.get("namespace") or "default"
        return self.kube.get_json(f"/api/v1/namespaces/{ns}/events", {"fieldSelector": fs, "limit": 200}).get("items", [])

    def _fetch_logs(self, app: str, pod: dict, container: str, tail: int, previous: bool) -> str:
        md = pod["metadata"]
        return self.kube.get_text(f"/api/v1/namespaces/{md['namespace']}/pods/{md['name']}/log",
                                  {"container": container, "tailLines": tail, "previous": str(previous).lower(),
                                   "timestamps": "true", "limitBytes": 512_000})

    # ------------------------------------------------------------------ access
    def access_report(self) -> dict[str, Any]:
        def run(checks):
            res = {}
            for verb, group, resource, sub, namespaced in checks:
                key = f"{verb} {resource}{'/' + sub if sub else ''}{'.' + group if group else ''}"
                try:
                    ok, _ = self.kube.can_i(verb, resource, group, self.ns if namespaced else None, sub)
                    res[key] = ok
                except Exception as exc:  # noqa: BLE001
                    res[key] = f"unknown ({str(exc)[:60]})"
            return res

        reads, writes = run(READ_CHECKS), run(WRITE_CHECKS)
        warnings = []
        dangerous = [k for k, v in writes.items() if v is True]
        if dangerous:
            warnings.append("The kube context has write/sensitive permissions (" + ", ".join(dangerous) +
                            "). The agent stays read-only: the tool layer only sends GET requests (plus "
                            "SelfSubjectAccessReview) and refuses Secrets. Prefer the read-only "
                            "ServiceAccount from deploy/kube-readonly.")
        missing = [k for k, v in reads.items() if v is not True]
        if missing:
            warnings.append("Missing read permissions: " + ", ".join(missing))
        return {"mode": self.mode, "identity": f"kube context {self.kube.context_name}",
                "read_permissions": reads, "write_or_sensitive_permissions": writes, "warnings": warnings}


def _workload_info(obj: dict) -> list[dict[str, str]]:
    st, spec = obj.get("status", {}) or {}, obj.get("spec", {}) or {}
    info = []
    if "replicas" in spec:
        info.append({"name": "Replicas", "value": f"desired={spec.get('replicas')} ready={st.get('readyReplicas', 0)} "
                                                  f"updated={st.get('updatedReplicas', 0)} available={st.get('availableReplicas', 0)}"})
    for c in st.get("conditions", []) or []:
        if c.get("status") != "True" or c.get("type") in ("Failed", "ReplicaFailure"):
            info.append({"name": f"Condition {c.get('type')}", "value": f"{c.get('status')} {c.get('reason', '')}: {c.get('message', '')}"[:300]})
    images = sorted({c.get("image") for c in ((spec.get("template") or {}).get("spec") or {}).get("containers", [])})
    if images:
        info.append({"name": "Images", "value": ", ".join(i for i in images if i)})
    return info


def _rs_health(obj: dict) -> dict:
    spec, st = obj.get("spec", {}) or {}, obj.get("status", {}) or {}
    want, ready = spec.get("replicas", 0) or 0, st.get("readyReplicas", 0) or 0
    for c in st.get("conditions", []) or []:
        if c.get("type") == "ReplicaFailure" and c.get("status") == "True":
            return {"status": "Degraded", "message": c.get("message")}
    if ready < want:
        return {"status": "Progressing", "message": f"Waiting for rollout to finish: {ready} of {want} ready"}
    return {"status": "Healthy"}


def _job_health(obj: dict) -> dict:
    st = obj.get("status", {}) or {}
    for c in st.get("conditions", []) or []:
        if c.get("type") == "Failed" and c.get("status") == "True":
            return {"status": "Degraded", "message": f"{c.get('reason')}: {c.get('message')}"}
        if c.get("type") == "Complete" and c.get("status") == "True":
            return {"status": "Healthy"}
    return {"status": "Progressing", "message": f"active={st.get('active', 0)} failed={st.get('failed', 0)}"}
