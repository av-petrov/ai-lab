"""CrewAI tools for the argocd-observability agent.

Every tool:
  * is read-only - all I/O goes through the guarded clients (see ``guard.py``);
  * has typed pydantic arguments and a precise description;
  * returns redacted, size-bounded JSON (``sanitize.to_llm_json``);
  * converts errors into JSON the agent can cite; guard refusals are returned as ``refused``;
  * records the data source it used (or why it was unavailable) in the context ledger.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, Literal, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from ..backends.base import ScopeError, summarize_application, summarize_tree
from ..context import ObservabilityContext
from ..guard import ReadOnlyViolation
from ..sanitize import redact_text, to_llm_json, truncate_text
from ..sanitize import tail_lines as keep_last_lines


def _guarded(fn: Callable[..., Any]) -> Callable[..., str]:
    @functools.wraps(fn)
    def wrapper(self: "_ObsTool", *args: Any, **kwargs: Any) -> str:
        ctx = self._ctx
        try:
            result = fn(self, *args, **kwargs)
        except ReadOnlyViolation as exc:
            ctx.ledger.refuse(str(exc))
            result = {"refused": str(exc)}
        except ScopeError as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{exc.__class__.__name__}: {str(exc)[:500]}",
                      "hint": "report this data source as unavailable; do not guess"}
        text = result if isinstance(result, str) else to_llm_json(result, ctx.settings.max_tool_output_chars)
        ctx.ledger.record_output(text)
        return text
    return wrapper


class _ObsTool(BaseTool):
    _ctx: ObservabilityContext = PrivateAttr()

    def __init__(self, ctx: ObservabilityContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx


# =========================================================================== access
class _NoArgs(BaseModel):
    pass


class AccessCheckTool(_ObsTool):
    name: str = "argocd_access_check"
    description: str = (
        "Report which auth mode and identity this agent uses, which read permissions it has, any write "
        "permissions the credentials carry (the agent never uses them), and which metrics sources "
        "(Prometheus, Argo CD /metrics scrape, metrics-server) are available. Call once at the start.")
    args_schema: Type[BaseModel] = _NoArgs

    @_guarded
    def _run(self) -> Any:
        self._ctx.ledger.use(self._ctx.primary_source)
        return self._ctx.access_report()


# =========================================================================== applications
class ListAppsInput(BaseModel):
    project: str | None = Field(None, description="Only applications of this Argo CD project.")
    names: list[str] | None = Field(None, description="Only these application names.")
    selector: str | None = Field(None, description="Kubernetes label selector on the Application objects.")
    only_problematic: bool = Field(False, description="Return only apps that are not Synced+Healthy, failed, "
                                                      "have conditions or are progressing too long.")


class ListApplicationsTool(_ObsTool):
    name: str = "argocd_list_applications"
    description: str = (
        "Step 1. List Argo CD Applications with sync status, health status (+message), last operation "
        "phase/message/failed resources, conditions, revision, auto-sync flag and computed problem_flags. "
        "Use this first to find which apps need a deeper look.")
    args_schema: Type[BaseModel] = ListAppsInput

    @_guarded
    def _run(self, project: str | None = None, names: list[str] | None = None, selector: str | None = None,
             only_problematic: bool = False) -> Any:
        ctx = self._ctx
        apps = ctx.backend.list_applications(project=project, selector=selector)
        ctx.ledger.use(ctx.primary_source)
        rows = [summarize_application(a, ctx.settings.progressing_threshold_minutes) for a in apps]
        if names:
            wanted = set(names)
            missing = sorted(wanted - {r["name"] for r in rows})
            rows = [r for r in rows if r["name"] in wanted]
        else:
            missing = []
        for r in rows:
            ctx.ledger.seen_apps.add(r["name"])
            ctx.ledger.app_facts[r["name"]] = r
        total = len(rows)
        if only_problematic:
            rows = [r for r in rows if r["problematic"]]
        for r in rows:
            r.pop("sources", None)
        out: dict[str, Any] = {"total": total, "problematic": sum(1 for r in rows if r["problematic"]),
                               "applications": rows}
        if missing:
            out["not_found"] = missing
        return out


class AppInput(BaseModel):
    app_name: str = Field(description="Argo CD Application name.")


class GetApplicationTool(_ObsTool):
    name: str = "argocd_get_application"
    description: str = (
        "Step 1b. Full status of ONE application: sources, destination, sync policy, sync/health, conditions, "
        "last operation (phase, message, failed resources), recent history, images, and every managed "
        "resource that is not Synced/Healthy (resources_not_ok). Read-only; never refreshes or syncs.")
    args_schema: Type[BaseModel] = AppInput

    @_guarded
    def _run(self, app_name: str) -> Any:
        ctx = self._ctx
        app = ctx.backend.get_application(app_name)
        ctx.ledger.use(ctx.primary_source)
        ctx.ledger.seen_apps.add(app_name)
        summary = summarize_application(app, ctx.settings.progressing_threshold_minutes, detail=True)
        ctx.ledger.app_facts[app_name] = summary
        return summary


class TreeInput(BaseModel):
    app_name: str = Field(description="Argo CD Application name.")
    only_unhealthy: bool = Field(True, description="Only nodes whose health is not Healthy (default).")


class ResourceTreeTool(_ObsTool):
    name: str = "argocd_resource_tree"
    description: str = (
        "Step 2. Walk the live resource tree of an application (Deployments -> ReplicaSets -> Pods, Jobs, "
        "Ingresses, ...) and return the failing nodes with health message, parents, status reason "
        "(e.g. ImagePullBackOff, CrashLoopBackOff), restart counts and images. Refs look like "
        "'Kind/namespace/name' and are the only resources the event/log/live tools accept.")
    args_schema: Type[BaseModel] = TreeInput

    @_guarded
    def _run(self, app_name: str, only_unhealthy: bool = True) -> Any:
        ctx = self._ctx
        tree = ctx.backend.resource_tree(app_name, fresh=True)
        ctx.ledger.use(ctx.primary_source)
        out = summarize_tree(tree, only_unhealthy=only_unhealthy)
        if not tree.get("nodes"):
            out["note"] = "empty tree: no live resources exist (e.g. never synced, or all resources Missing)"
        return out


class ResourceInput(BaseModel):
    app_name: str = Field(description="Argo CD Application the resource belongs to.")
    kind: str = Field(description="Resource kind from the tree, e.g. Pod, Deployment, Job. Secrets are refused.")
    name: str = Field(description="Resource name from the tree.")
    namespace: str | None = Field(None, description="Resource namespace from the tree.")


class LiveResourceTool(_ObsTool):
    name: str = "argocd_live_resource_status"
    description: str = (
        "Read the live STATUS of one resource from the app's tree (e.g. a Pod's containerStatuses with "
        "waiting reason, exit code, last termination reason/OOMKilled; a Deployment's conditions). Returns "
        "status plus container images/commands/resources/probes; env values are redacted. Secrets are refused.")
    args_schema: Type[BaseModel] = ResourceInput

    @_guarded
    def _run(self, app_name: str, kind: str, name: str, namespace: str | None = None) -> Any:
        ctx = self._ctx
        obj = ctx.backend.live_resource(app_name, kind, name, namespace)
        ctx.ledger.use(ctx.primary_source)
        spec = obj.get("spec", {}) or {}
        pod_spec = spec if kind.lower() == "pod" else ((spec.get("template") or {}).get("spec") or {})
        containers = [
            {"name": c.get("name"), "image": c.get("image"), "command": c.get("command"), "args": c.get("args"),
             "resources": c.get("resources"), "readinessProbe": c.get("readinessProbe"),
             "livenessProbe": c.get("livenessProbe"),
             "env_names": [e.get("name") for e in c.get("env", []) or []]}
            for c in (pod_spec.get("initContainers", []) or []) + (pod_spec.get("containers", []) or [])
        ]
        md = obj.get("metadata", {}) or {}
        return {"ref": f"{kind}/{namespace or md.get('namespace') or '-'}/{name}",
                "labels": md.get("labels"), "creationTimestamp": md.get("creationTimestamp"),
                "containers": containers, "nodeName": pod_spec.get("nodeName"),
                "replicas": spec.get("replicas"), "status": obj.get("status")}


# =========================================================================== events
class EventsInput(BaseModel):
    app_name: str = Field(description="Argo CD Application name.")
    kind: str | None = Field(None, description="Resource kind from the tree. Omit kind+name for the Application's own events.")
    name: str | None = Field(None, description="Resource name from the tree.")
    namespace: str | None = Field(None, description="Resource namespace from the tree.")
    limit: int = Field(20, ge=1, le=50, description="Max events (Warnings first, newest first).")


class EventsTool(_ObsTool):
    name: str = "k8s_events"
    description: str = (
        "Step 3. Kubernetes events for one failing resource of an app (or for the Application itself when "
        "kind/name are omitted): type, reason, message, count, first/last seen. Use after the resource tree "
        "shows a failing node; events explain image pull errors, scheduling failures, probe failures, "
        "sync hooks and more.")
    args_schema: Type[BaseModel] = EventsInput

    @_guarded
    def _run(self, app_name: str, kind: str | None = None, name: str | None = None,
             namespace: str | None = None, limit: int = 20) -> Any:
        ctx = self._ctx
        evs = ctx.backend.events(app_name, kind, name, namespace, limit=limit)
        ctx.ledger.use(ctx.primary_source)
        return {"target": f"{kind}/{namespace or '-'}/{name}" if kind else f"Application/{app_name}",
                "count": len(evs), "events": evs,
                **({"note": "no events found (events expire after ~1h by default)"} if not evs else {})}


# =========================================================================== logs
class LogsInput(BaseModel):
    app_name: str = Field(description="Argo CD Application the pod belongs to.")
    pod: str = Field(description="Pod name from the resource tree.")
    namespace: str | None = Field(None, description="Pod namespace from the resource tree.")
    container: str | None = Field(None, description="Container; default: the first not-ready container.")
    tail_lines: int = Field(200, ge=1, description="Lines from the end (capped by ARGOCD_OBS_LOG_TAIL_MAX).")
    previous: bool = Field(False, description="Logs of the previous (crashed) container instance. Use for "
                                              "CrashLoopBackOff / restarts.")


class PodLogsTool(_ObsTool):
    name: str = "pod_logs"
    description: str = (
        "Step 4 (only if events/status do not explain the failure). Tail of a container's logs for a pod "
        "in the app's tree. Set previous=true for CrashLoopBackOff to see why the last instance died. "
        "Output is tail-limited and secrets in log lines are redacted. Cite exact lines as evidence.")
    args_schema: Type[BaseModel] = LogsInput

    @_guarded
    def _run(self, app_name: str, pod: str, namespace: str | None = None, container: str | None = None,
             tail_lines: int = 200, previous: bool = False) -> Any:
        ctx = self._ctx
        s = ctx.settings
        tail = max(1, min(tail_lines or s.log_tail_lines, s.log_tail_max))
        container, text, extra = ctx.backend.pod_logs(app_name, pod, namespace, container, tail, previous)
        ctx.ledger.use(ctx.primary_source + " (logs)")
        text = redact_text(keep_last_lines(text, tail))
        text = truncate_text(text, max(1000, s.max_tool_output_chars - 2500), keep="tail")
        states = [{k: c.get(k) for k in ("container", "state", "reason", "exit_code", "restart_count", "last_terminated")
                   if c.get(k) is not None} for c in extra["containers"]]
        return {"pod": f"{namespace or '-'}/{pod}", "container": container, "previous": previous,
                "tail_lines": tail, "container_states": states,
                "lines": text or "(no log output)"}


# =========================================================================== metrics
class MetricsInput(BaseModel):
    target: Literal["app", "controller", "repo_server", "server", "pod_usage", "promql"] = Field(
        description="app: argocd_app_info/sync totals for app_name; controller/repo_server/server: Argo CD "
                    "component metrics; pod_usage: CPU/memory of pods in a namespace; promql: custom query "
                    "(Prometheus only).")
    app_name: str | None = Field(None, description="Application name for target=app.")
    namespace: str | None = Field(None, description="Namespace for target=pod_usage.")
    pod_prefix: str | None = Field(None, description="Optional pod name prefix for target=pod_usage.")
    promql: str | None = Field(None, description="PromQL for target=promql (read-only query API).")
    range_minutes: int | None = Field(None, ge=1, le=180, description="Range query window for target=promql.")


class MetricsTool(_ObsTool):
    name: str = "argocd_metrics"
    description: str = (
        "Step 5 (only if still needed). Metrics: Argo CD's own Prometheus metrics (argocd_app_info, "
        "argocd_app_sync_total, argocd_app_reconcile, git/repo-server and controller metrics) and pod "
        "CPU/memory. Sources are detected automatically (PROMETHEUS_URL -> scrape of Argo CD /metrics via "
        "the API server proxy -> metrics-server). If no source is available it says so - report that as a "
        "limitation instead of retrying.")
    args_schema: Type[BaseModel] = MetricsInput

    @_guarded
    def _run(self, target: str, app_name: str | None = None, namespace: str | None = None,
             pod_prefix: str | None = None, promql: str | None = None, range_minutes: int | None = None) -> Any:
        ctx, m = self._ctx, self._ctx.metrics
        det = m.detect()
        for k, v in det.items():
            if not v.available:
                ctx.ledger.missing(k, v.detail)
        if target == "promql":
            if not promql:
                return {"error": "promql is required for target=promql"}
            res = m.promql(promql, range_minutes)
        elif target == "pod_usage":
            if not namespace:
                return {"error": "namespace is required for target=pod_usage"}
            res = m.pod_usage(namespace, pod_prefix)
        else:
            comp = {"app": "application-controller", "controller": "application-controller",
                    "repo_server": "repo-server", "server": "server"}[target]
            res = m.argocd_component(comp, app_name if target == "app" else None)
        src = str(res.get("source") or "")
        if res.get("available", True) and src:
            ctx.ledger.use({"scrape": "argocd_metrics_scrape", "metrics-server": "metrics_server"}.get(
                src.split(" ")[0], "prometheus"))
        res["sources_detected"] = {k: f"{'available' if v.available else 'unavailable'}: {v.detail}" for k, v in det.items()}
        return res


# =========================================================================== factory
def build_tools(ctx: ObservabilityContext) -> list[BaseTool]:
    return [AccessCheckTool(ctx), ListApplicationsTool(ctx), GetApplicationTool(ctx), ResourceTreeTool(ctx),
            EventsTool(ctx), LiveResourceTool(ctx), PodLogsTool(ctx), MetricsTool(ctx)]


def tool_context(tools: list[BaseTool]) -> ObservabilityContext | None:
    for t in tools:
        if isinstance(t, _ObsTool):
            return t._ctx
    return None


def dumps(obj: Any) -> str:  # tiny helper for scripts
    return json.dumps(obj, indent=1, default=str)
