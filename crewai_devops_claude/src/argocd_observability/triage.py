"""Deterministic triage (``argocd-observe --no-llm``).

Uses the same tools - and therefore the same guard, redaction, limits and ledger - as the LLM
agent, but applies fixed rules. Useful for CI, for the demo check, and as a cheap first pass.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .backends.base import verdict_for as _verdict
from .context import ObservabilityContext
from .models import AppDiagnosis, ArgoCDHealthReport, Evidence
from .report import Scope
from .tools.argocd_tools import build_tools

_IMAGE = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
_CONFIG = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}




class _Runner:
    def __init__(self, ctx: ObservabilityContext) -> None:
        self.tools = {t.name: t for t in build_tools(ctx)}

    def __call__(self, tool_name: str, /, **kw: Any) -> dict:
        return json.loads(self.tools[tool_name].run(**kw))


def _ns_name(ref: str) -> tuple[str, str, str]:
    kind, ns, name = ref.split("/", 2)
    return kind, (None if ns == "-" else ns), name  # type: ignore[return-value]


def diagnose_app(run: _Runner, app: dict) -> AppDiagnosis:
    name = app["name"]
    d = AppDiagnosis(app_name=name, project=app.get("project"), destination_namespace=app.get("destination_namespace"),
                     sync_status=app["sync_status"], health_status=app["health_status"],
                     operation_phase=(app.get("last_operation") or {}).get("phase"), revision=app.get("revision"),
                     verdict=_verdict(app["sync_status"], app["health_status"]))  # type: ignore[arg-type]
    if not app.get("problematic"):
        d.confidence = "high"
        return d
    ev = d.evidence
    ev.append(Evidence(source="argocd_app_status", reference=f"Application {name}",
                       detail=f"sync={app['sync_status']} health={app['health_status']}"
                              + (f" message={app['health_message']}" if app.get("health_message") else "")))
    op = app.get("last_operation") or {}
    # --- 1. Argo CD level problems -------------------------------------------------------------
    for c in app.get("conditions", []):
        if c["type"] in ("ComparisonError", "InvalidSpecError", "SyncError", "UnknownError"):
            ev.append(Evidence(source="argocd_app_status", reference=f"Application {name} condition {c['type']}",
                               detail=c["message"] or "", observed_at=c.get("at")))
            d.root_cause_category = "comparison_error" if c["type"] != "SyncError" else "sync_failed"
            d.probable_root_cause = f"Argo CD reports {c['type']}: {(c['message'] or '')[:300]}"
            d.confidence = "high"
            d.suggested_next_steps = ["Fix the source/manifests referenced in the condition message, then let "
                                      "Argo CD re-compare (a human or remediation agent triggers any refresh)."]
            return d
    if op.get("phase") in ("Failed", "Error"):
        ev.append(Evidence(source="argocd_app_status", reference=f"Application {name} operationState",
                           detail=f"phase={op['phase']} message={op.get('message')}", observed_at=op.get("finished_at")))
        for fr in op.get("failed_resources", [])[:3]:
            ev.append(Evidence(source="argocd_app_status", reference=f"Application {name} syncResult", detail=fr))
        d.root_cause_category, d.confidence = "sync_failed", "high"
        d.probable_root_cause = f"Last sync operation {op['phase']}: {(op.get('message') or '')[:300]}"
    if not op and app["sync_status"] == "OutOfSync" and app["health_status"] == "Missing" and not app.get("automated_sync"):
        detail = run("argocd_get_application", app_name=name)
        missing = [f"{r['kind']}/{r.get('namespace') or '-'}/{r['name']}" for r in detail.get("resources_not_ok", [])
                   if r.get("health") == "Missing" or r.get("sync") == "OutOfSync"]
        d.failing_resources = missing[:20]
        ev.append(Evidence(source="argocd_app_status", reference=f"Application {name}",
                           detail=f"automated_sync={app.get('automated_sync')} last_operation=null"))
        if missing:
            ev.append(Evidence(source="argocd_app_status", reference=f"Application {name} resources",
                               detail=f"{len(missing)} resource(s) OutOfSync/Missing, e.g. {', '.join(missing[:3])}"))
        d.root_cause_category, d.confidence = "never_synced", "high"
        d.probable_root_cause = ("The application has never been synced: there is no operation history, auto-sync "
                                 "is disabled, and all managed resources are Missing in the cluster.")
        d.suggested_next_steps = [f"Sync the app manually after reviewing the diff (`argocd app diff {name}`, then "
                                  f"`argocd app sync {name}`), or enable automated sync in its spec via Git."]
        return d
    # --- 2. resource tree ----------------------------------------------------------------------
    tree = run("argocd_resource_tree", app_name=name, only_unhealthy=True)
    nodes = tree.get("nodes", [])
    d.failing_resources = [n["ref"] for n in nodes][:20]
    pods = [n for n in nodes if n["ref"].startswith("Pod/")]
    for pod in pods[:3]:
        _, ns, pname = _ns_name(pod["ref"])
        reason = (pod.get("info") or {}).get("Status Reason") or ""
        msg = pod.get("message") or ""
        ev.append(Evidence(source="resource_tree", reference=pod["ref"],
                           detail=f"health={pod['health']} reason={reason} message={msg}".strip()))
        if reason in _IMAGE or "pull" in msg.lower():
            events = run("k8s_events", app_name=name, kind="Pod", name=pname, namespace=ns, limit=10)
            pulls = [e for e in events.get("events", []) if e.get("reason") in ("Failed", "BackOff", "ErrImagePull")
                     or "pull" in (e.get("message") or "").lower()]
            for e in pulls[:2]:
                ev.append(Evidence(source="k8s_event", reference=f"{pod['ref']} events", detail=e["message"],
                                   observed_at=e.get("last_seen")))
            image = (pod.get("images") or [None])[0]
            m = re.search(r'image "([^"]+)"', " ".join(e["message"] for e in pulls)) if pulls else None
            image = m.group(1) if m else image
            d.root_cause_category, d.confidence = "image_pull", "high" if pulls else "medium"
            d.probable_root_cause = (f"Pod cannot pull image {image or '(unknown)'}: "
                                     + (pulls[0]["message"][:250] if pulls else reason))
            d.suggested_next_steps = ["Check that the image tag exists in the registry and fix the tag in Git "
                                      "(kustomize images / Helm values).",
                                      "If the registry is private, verify imagePullSecrets on the pod's service account."]
            return d
        if reason == "CrashLoopBackOff" or "back-off restarting" in msg.lower() or reason in ("Error", "OOMKilled"):
            live = run("argocd_live_resource_status", app_name=name, kind="Pod", name=pname, namespace=ns)
            exit_info = ""
            for cs in (live.get("status") or {}).get("containerStatuses", []) or []:
                last = (cs.get("lastState") or {}).get("terminated") or {}
                if last:
                    exit_info = f"container {cs.get('name')} last terminated: reason={last.get('reason')} exitCode={last.get('exitCode')}"
                    ev.append(Evidence(source="live_resource", reference=f"{pod['ref']} status.containerStatuses",
                                       detail=exit_info, observed_at=last.get("finishedAt")))
                    if last.get("reason") == "OOMKilled":
                        d.root_cause_category = "oom_killed"
                    break
            logs = run("pod_logs", app_name=name, pod=pname, namespace=ns, previous=True, tail_lines=30)
            lines = [ln for ln in (logs.get("lines") or "").splitlines() if ln.strip() and not ln.startswith("[...")]
            if not lines or lines == ["(no log output)"]:
                logs = run("pod_logs", app_name=name, pod=pname, namespace=ns, previous=False, tail_lines=30)
                lines = [ln for ln in (logs.get("lines") or "").splitlines() if ln.strip() and not ln.startswith("[...")]
            interesting = [ln for ln in lines if re.search(r"(?i)(fatal|error|panic|exception|not set|missing|denied|refused)", ln)]
            for ln in (interesting or lines)[-3:]:
                if ln != "(no log output)":
                    ev.append(Evidence(source="container_log", reference=f"{pod['ref']} logs (previous container)",
                                       detail=ln))
            if d.root_cause_category != "oom_killed":
                d.root_cause_category = "crash_loop"
            d.confidence = "high" if (interesting and exit_info) else "medium"
            last_line = (interesting or lines or ["(no logs)"])[-1]
            d.probable_root_cause = (f"Container crashes on start and is in CrashLoopBackOff ({exit_info or 'exit code unknown'}); "
                                     f"last log line: {last_line[:200]}")
            d.suggested_next_steps = ["Fix the configuration/command the log line points to (in Git), then let the "
                                      "rollout replace the pod.",
                                      "Compare with the last healthy revision in the app history."]
            return d
        if reason in _CONFIG:
            d.root_cause_category, d.confidence = "container_config", "high"
            d.probable_root_cause = f"Container cannot be created: {msg[:300]}"
            d.suggested_next_steps = ["Create the missing ConfigMap/Secret key referenced in the message (via Git)."]
            return d
        if pod["health"] == "Progressing":
            events = run("k8s_events", app_name=name, kind="Pod", name=pname, namespace=ns, limit=10)
            sched = [e for e in events.get("events", []) if e.get("reason") in ("FailedScheduling", "Unhealthy", "FailedMount")]
            for e in sched[:2]:
                ev.append(Evidence(source="k8s_event", reference=f"{pod['ref']} events", detail=e["message"],
                                   observed_at=e.get("last_seen")))
            if sched:
                cat = {"FailedScheduling": "scheduling", "Unhealthy": "probe_failure"}.get(sched[0]["reason"], "resource_degraded")
                d.root_cause_category, d.confidence = cat, "high"  # type: ignore[assignment]
                d.probable_root_cause = f"{sched[0]['reason']}: {sched[0]['message'][:300]}"
                return d
    if d.probable_root_cause:
        return d
    for n in nodes[:3]:
        if n["ref"].startswith("Pod/"):
            continue
        ev.append(Evidence(source="resource_tree", reference=n["ref"], detail=f"health={n['health']} message={n.get('message')}"))
    if nodes:
        d.root_cause_category, d.confidence = "resource_degraded", "low"
        d.probable_root_cause = f"{nodes[0]['ref']} is {nodes[0]['health']}: {nodes[0].get('message')}"
    elif app["sync_status"] == "OutOfSync":
        d.root_cause_category, d.confidence = "drift", "medium"
        d.probable_root_cause = "Live state differs from Git (OutOfSync) while resources are healthy."
        d.suggested_next_steps = [f"Review `argocd app diff {name}` to see which fields drifted."]
    elif any(c["type"].endswith("Warning") for c in app.get("conditions", [])):
        c = next(c for c in app["conditions"] if c["type"].endswith("Warning"))
        d.verdict = "healthy" if d.verdict == "healthy" else d.verdict
        ev.append(Evidence(source="argocd_app_status", reference=f"Application {name} condition {c['type']}",
                           detail=c["message"] or ""))
        d.root_cause_category, d.confidence = "none", "medium"
        d.probable_root_cause = f"Warning only: {c['message']}"
    else:
        d.root_cause_category = "unknown"
        d.probable_root_cause = "No failing resource found in the tree; flags: " + ", ".join(app.get("problem_flags", []))
    return d


def run_triage(ctx: ObservabilityContext, scope: Scope) -> ArgoCDHealthReport:
    run = _Runner(ctx)
    access = run("argocd_access_check")
    listing = run("argocd_list_applications", project=scope.project, names=scope.apps or None)
    if "error" in listing or "refused" in listing:
        raise RuntimeError(f"cannot list applications: {listing}")
    apps = [diagnose_app(run, a) for a in listing.get("applications", [])]
    bad = [a for a in apps if a.verdict != "healthy"]
    warn = [a for a in apps if a.verdict == "healthy" and a.probable_root_cause]
    summary = (f"{len(apps)} application(s) inspected: {len(apps) - len(bad)} healthy, {len(bad)} need attention"
               + (": " + "; ".join(f"{a.app_name} ({a.root_cause_category})" for a in bad) if bad else "")
               + (f". {len(warn)} healthy app(s) carry warnings." if warn else "."))
    limitations = list(access.get("warnings", []))
    if listing.get("not_found"):
        limitations.append(f"applications not found: {listing['not_found']}")
    return ArgoCDHealthReport(scope=scope.describe(), summary=summary, apps=apps, limitations=limitations,
                              data_sources_used=[])
