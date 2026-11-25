"""Backend interface + normalization shared by the Argo CD API and the Kubernetes backends.

Both backends return the *same shapes*: raw ``Application`` objects (the Argo CD API returns the CR
as-is) and an Argo CD-style resource tree (``{"nodes": [...]}``). All summarizing happens here, so the
tools and the deterministic triage behave identically in both auth modes.
"""

from __future__ import annotations

import abc
import time
from datetime import datetime, timezone
from typing import Any

from ..guard import refuse_secret_kind

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Rollout"}
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError", "ErrImageNeverPull",
               "PreCreateHookError", "PostStartHookError"}


class ScopeError(LookupError):
    """The requested resource is not part of the application's resource tree."""


def parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_minutes(v: str | None, now: datetime | None = None) -> float | None:
    t = parse_ts(v)
    if t is None:
        return None
    return round(((now or datetime.now(timezone.utc)) - t).total_seconds() / 60, 1)


# --------------------------------------------------------------------------- applications
def summarize_application(app: dict, threshold_minutes: int = 10, detail: bool = False) -> dict[str, Any]:
    md, spec, st = app.get("metadata", {}), app.get("spec", {}), app.get("status", {}) or {}
    sync, health = st.get("sync", {}) or {}, st.get("health", {}) or {}
    op = st.get("operationState") or {}
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    out: dict[str, Any] = {
        "name": md.get("name"),
        "project": spec.get("project"),
        "destination": f"{spec.get('destination', {}).get('server') or spec.get('destination', {}).get('name')}"
                       f" ns={spec.get('destination', {}).get('namespace')}",
        "destination_namespace": spec.get("destination", {}).get("namespace"),
        "sources": [{k: s.get(k) for k in ("repoURL", "path", "chart", "targetRevision") if s.get(k)} for s in sources],
        "sync_status": sync.get("status", "Unknown"),
        "health_status": health.get("status", "Unknown"),
        "health_message": health.get("message"),
        "health_since": health.get("lastTransitionTime"),
        "revision": sync.get("revision") or (sync.get("revisions") or [None])[0],
        "automated_sync": bool((spec.get("syncPolicy") or {}).get("automated")),
        "reconciled_at": st.get("reconciledAt"),
        "conditions": [{"type": c.get("type"), "message": c.get("message"), "at": c.get("lastTransitionTime")}
                       for c in st.get("conditions", []) or []],
    }
    if op:
        res = op.get("syncResult", {}) or {}
        failed = [f"{r.get('kind')}/{r.get('namespace') or '-'}/{r.get('name')}: {r.get('status')} {r.get('message', '')}".strip()
                  for r in res.get("resources", []) or []
                  if r.get("status") not in (None, "Synced", "Pruned", "PruneSkipped")
                  or r.get("hookPhase") in ("Failed", "Error")]
        out["last_operation"] = {
            "phase": op.get("phase"), "message": op.get("message"), "started_at": op.get("startedAt"),
            "finished_at": op.get("finishedAt"), "revision": res.get("revision") or (res.get("revisions") or [None])[0],
            "retry_count": op.get("retryCount"), "failed_resources": failed[:20],
            "initiated_by": (op.get("operation", {}) or {}).get("initiatedBy", {}),
        }
    else:
        out["last_operation"] = None
    flags = []
    if out["sync_status"] != "Synced":
        flags.append(f"sync={out['sync_status']}")
    if out["health_status"] not in ("Healthy",):
        flags.append(f"health={out['health_status']}")
    if op.get("phase") in ("Failed", "Error"):
        flags.append(f"last_operation={op.get('phase')}")
    if out["conditions"]:
        flags.append("conditions=" + ",".join(sorted({c["type"] for c in out["conditions"] if c["type"]})))
    if out["health_status"] == "Progressing":
        mins = age_minutes(out["health_since"])
        if mins is not None and mins > threshold_minutes:
            flags.append(f"progressing_for={mins}min (> {threshold_minutes})")
    if op.get("phase") == "Running":
        mins = age_minutes(op.get("startedAt"))
        if mins is not None and mins > threshold_minutes:
            flags.append(f"operation_running_for={mins}min")
    if not op and out["sync_status"] == "OutOfSync" and not out["automated_sync"]:
        flags.append("never_synced_or_no_operation_history_and_autosync_disabled")
    out["problem_flags"] = flags
    out["problematic"] = bool(flags)
    hints = []
    if not op and out["sync_status"] == "OutOfSync" and not out["automated_sync"]:
        hints.append("No sync operation has ever been recorded and automated sync is disabled: the app was most "
                     "likely never synced, which is why its resources are Missing (root_cause_category never_synced).")
    if op.get("phase") in ("Failed", "Error"):
        hints.append(f"The last sync operation {op.get('phase')}: see last_operation.message and failed_resources "
                     "(root_cause_category sync_failed).")
    for c in out["conditions"]:
        if c["type"] in ("ComparisonError", "InvalidSpecError"):
            hints.append(f"{c['type']}: Argo CD cannot render/compare the manifests (root_cause_category comparison_error).")
    if out["health_status"] in ("Degraded", "Progressing") and out["sync_status"] == "Synced":
        hints.append("Synced but not Healthy: the manifests were applied; inspect the resource tree for failing pods.")
    if hints:
        out["diagnostic_hints"] = hints
    if detail:
        out["sync_policy"] = spec.get("syncPolicy")
        out["history"] = [{"revision": h.get("revision") or h.get("revisions"), "deployed_at": h.get("deployedAt"),
                           "id": h.get("id")} for h in (st.get("history") or [])[-3:]]
        out["images"] = (st.get("summary") or {}).get("images", [])
        res = st.get("resources", []) or []
        out["resources_total"] = len(res)
        out["resources_not_ok"] = [
            {"kind": r.get("kind"), "namespace": r.get("namespace"), "name": r.get("name"), "sync": r.get("status"),
             "health": (r.get("health") or {}).get("status"), "message": (r.get("health") or {}).get("message")}
            for r in res if r.get("status") != "Synced" or (r.get("health") or {}).get("status") not in (None, "Healthy")
        ][:40]
    return out


def verdict_for(sync: str, health: str) -> str:
    return {"Healthy": "healthy" if sync == "Synced" else "out_of_sync", "Degraded": "degraded",
            "Progressing": "progressing", "Missing": "missing", "Suspended": "suspended"}.get(health, "unknown")


# --------------------------------------------------------------------------- pods
def pod_container_states(pod: dict) -> list[dict[str, Any]]:
    st = pod.get("status", {}) or {}
    spec_containers = {c["name"]: c for c in (pod.get("spec", {}) or {}).get("containers", [])}
    out = []
    for kind, key in (("init", "initContainerStatuses"), ("main", "containerStatuses")):
        for cs in st.get(key, []) or []:
            state = cs.get("state", {}) or {}
            last = (cs.get("lastState", {}) or {}).get("terminated")
            item: dict[str, Any] = {"container": cs.get("name"), "type": kind, "image": cs.get("image"),
                                    "ready": cs.get("ready"), "restart_count": cs.get("restartCount", 0)}
            if "waiting" in state:
                item["state"] = "waiting"
                item["reason"] = state["waiting"].get("reason")
                item["message"] = state["waiting"].get("message")
            elif "terminated" in state:
                t = state["terminated"]
                item.update(state="terminated", reason=t.get("reason"), exit_code=t.get("exitCode"),
                            message=t.get("message"), finished_at=t.get("finishedAt"))
            elif "running" in state:
                item.update(state="running", started_at=state["running"].get("startedAt"))
            if last:
                item["last_terminated"] = {"reason": last.get("reason"), "exit_code": last.get("exitCode"),
                                           "finished_at": last.get("finishedAt"), "message": last.get("message")}
            sc = spec_containers.get(cs.get("name"))
            if sc:
                item["resources"] = sc.get("resources")
                if sc.get("command") or sc.get("args"):
                    item["command"] = (sc.get("command") or []) + (sc.get("args") or [])
            out.append(item)
    return out


def pod_health(pod: dict) -> tuple[str, str | None, str | None]:
    """(status, message, reason) in Argo CD terms."""
    st = pod.get("status", {}) or {}
    phase = st.get("phase")
    for cs in pod_container_states(pod):
        if cs.get("state") == "waiting" and cs.get("reason") in BAD_WAITING:
            return "Degraded", cs.get("message") or cs.get("reason"), cs.get("reason")
        if cs.get("state") == "terminated" and cs.get("exit_code") not in (0, None) and cs["type"] == "main":
            return "Degraded", f"exit code {cs.get('exit_code')} ({cs.get('reason')})", cs.get("reason")
    if phase == "Failed":
        return "Degraded", st.get("message") or st.get("reason"), st.get("reason") or "Failed"
    if phase == "Succeeded":
        return "Healthy", None, "Completed"
    if phase == "Pending":
        conds = [c for c in st.get("conditions", []) or [] if c.get("status") == "False"]
        msg = "; ".join(f"{c.get('reason')}: {c.get('message')}" for c in conds if c.get("message")) or None
        return "Progressing", msg, (conds[0].get("reason") if conds else "Pending")
    if phase == "Running":
        if all(c.get("ready") for c in st.get("containerStatuses", []) or []):
            return "Healthy", None, "Running"
        return "Progressing", "containers not ready", "NotReady"
    return "Unknown", None, phase


# --------------------------------------------------------------------------- tree
def _info(node: dict) -> dict[str, str]:
    return {i.get("name"): i.get("value") for i in node.get("info", []) or [] if i.get("name")}


def node_ref(node: dict) -> str:
    return f"{node.get('kind')}/{node.get('namespace') or '-'}/{node.get('name')}"


def summarize_tree(tree: dict, only_unhealthy: bool = True, max_nodes: int = 60) -> dict[str, Any]:
    nodes = tree.get("nodes", []) or []
    by_uid = {n.get("uid"): n for n in nodes if n.get("uid")}
    counts: dict[str, int] = {}
    rows = []
    for n in nodes:
        h = (n.get("health") or {}).get("status") or "-"
        counts[h] = counts.get(h, 0) + 1
        if only_unhealthy and h in ("Healthy", "-"):
            continue
        info = _info(n)
        parents = [node_ref(by_uid[p["uid"]]) if p.get("uid") in by_uid else
                   f"{p.get('kind')}/{p.get('namespace') or '-'}/{p.get('name')}" for p in n.get("parentRefs", []) or []]
        row: dict[str, Any] = {"ref": node_ref(n), "health": h,
                               "message": (n.get("health") or {}).get("message")}
        if parents:
            row["parents"] = parents
        if info:
            row["info"] = info
        if n.get("images"):
            row["images"] = n["images"]
        if n.get("containers"):
            row["containers"] = n["containers"]
        if n.get("createdAt"):
            row["created_at"] = n["createdAt"]
        rows.append(row)
    rows.sort(key=lambda r: (r["health"] in ("Healthy", "-"), r["ref"]))
    return {"node_count": len(nodes), "health_counts": counts, "nodes": rows[:max_nodes],
            "omitted_nodes": max(0, len(rows) - max_nodes),
            "orphaned_nodes": len(tree.get("orphanedNodes", []) or [])}


# --------------------------------------------------------------------------- events
def normalize_events(items: list[dict], limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for e in items:
        io = e.get("involvedObject") or e.get("regarding") or {}
        out.append({
            "type": e.get("type"), "reason": e.get("reason"),
            "message": e.get("message") or e.get("note"),
            "count": e.get("count") or (e.get("series") or {}).get("count") or 1,
            "first_seen": e.get("firstTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp"),
            "last_seen": e.get("lastTimestamp") or (e.get("series") or {}).get("lastObservedTime") or e.get("eventTime"),
            "object": f"{io.get('kind')}/{io.get('namespace') or '-'}/{io.get('name')}",
            "source": (e.get("source") or {}).get("component") or e.get("reportingComponent"),
        })
    out.sort(key=lambda e: (e["type"] != "Warning", -(_ts(e["last_seen"]))))
    return out[:limit]


def _ts(v: str | None) -> float:
    t = parse_ts(v)
    return t.timestamp() if t else 0.0


# --------------------------------------------------------------------------- backend interface
class Backend(abc.ABC):
    mode: str = "abstract"
    tree_ttl_seconds = 30.0

    def __init__(self) -> None:
        self._tree_cache: dict[str, tuple[float, dict]] = {}

    # --- required ------------------------------------------------------------------
    @abc.abstractmethod
    def describe(self) -> str: ...

    @abc.abstractmethod
    def list_applications(self, project: str | None = None, selector: str | None = None) -> list[dict]: ...

    @abc.abstractmethod
    def get_application(self, name: str) -> dict: ...

    @abc.abstractmethod
    def _fetch_tree(self, name: str) -> dict: ...

    @abc.abstractmethod
    def _fetch_live(self, app: str, node: dict) -> dict: ...

    @abc.abstractmethod
    def _fetch_events(self, app: str, node: dict | None) -> list[dict]: ...

    @abc.abstractmethod
    def _fetch_logs(self, app: str, pod: dict, container: str, tail: int, previous: bool) -> str: ...

    @abc.abstractmethod
    def access_report(self) -> dict[str, Any]: ...

    # --- shared --------------------------------------------------------------------
    def resource_tree(self, name: str, fresh: bool = False) -> dict:
        hit = self._tree_cache.get(name)
        if hit and not fresh and time.monotonic() - hit[0] < self.tree_ttl_seconds:
            return hit[1]
        tree = self._fetch_tree(name)
        self._tree_cache[name] = (time.monotonic(), tree)
        return tree

    def find_node(self, app: str, kind: str, name: str, namespace: str | None = None) -> dict:
        """Scope check: only resources that belong to the app's tree can be read in detail."""
        for fresh in (False, True):
            for n in self.resource_tree(app, fresh=fresh).get("nodes", []):
                if n.get("kind", "").lower() == kind.lower() and n.get("name") == name and (
                        not namespace or (n.get("namespace") or "") == namespace):
                    return n
        raise ScopeError(f"{kind}/{namespace or '-'}/{name} is not part of application '{app}' "
                         f"(call argocd_resource_tree first and use refs from it)")

    def live_resource(self, app: str, kind: str, name: str, namespace: str | None = None) -> dict:
        refuse_secret_kind(kind)
        node = self.find_node(app, kind, name, namespace)
        return self._fetch_live(app, node)

    def events(self, app: str, kind: str | None = None, name: str | None = None,
               namespace: str | None = None, limit: int = 30) -> list[dict]:
        node = self.find_node(app, kind, name, namespace) if kind and name else None
        return normalize_events(self._fetch_events(app, node), limit)

    def pod_logs(self, app: str, pod: str, namespace: str | None, container: str | None,
                 tail: int, previous: bool) -> tuple[str, str, dict]:
        node = self.find_node(app, "Pod", pod, namespace)
        manifest = self._fetch_live(app, node)
        states = pod_container_states(manifest)
        names = [c["container"] for c in states]
        if container is None:
            bad = [c for c in states if not c.get("ready")]
            container = (bad[0] if bad else (states[0] if states else {})).get("container")
        if container is None or (names and container not in names):
            raise ValueError(f"container '{container}' not found in pod {pod}; containers: {names}")
        text = self._fetch_logs(app, manifest, container, tail, previous)
        return container, text, {"containers": states}
