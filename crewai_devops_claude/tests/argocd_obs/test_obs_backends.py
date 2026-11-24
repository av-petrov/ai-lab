"""Backends and tools against mocked Argo CD / Kubernetes responses; deterministic triage."""

from __future__ import annotations

import json

from argocd_observability.report import Scope
from argocd_observability.tools import build_tools
from argocd_observability.triage import run_triage

from .conftest import NOW, application, argocd_backend, kube_backend, make_ctx, pod


def _tools(ctx):
    return {t.name: t for t in build_tools(ctx)}


def test_list_and_flags(broken_cluster_routes):
    backend, sess = argocd_backend(broken_cluster_routes)
    out = json.loads(_tools(make_ctx(backend))["argocd_list_applications"].run(only_problematic=True))
    names = {a["name"]: a for a in out["applications"]}
    assert out["total"] == 4 and set(names) == {"demo-ip", "demo-cl", "never"}
    assert "never_synced_or_no_operation_history_and_autosync_disabled" in names["never"]["problem_flags"]
    assert all(m == "GET" for m, *_ in sess.calls)


def test_logs_are_tail_limited_and_redacted(broken_cluster_routes):
    from .conftest import FakeResponse

    lines = [{"result": {"content": f"line {i} password=hunter{i}", "timeStampStr": NOW}} for i in range(3000)]
    broken_cluster_routes["/api/v1/applications/demo-cl/logs"] = lambda p: FakeResponse(200, b"", lines=lines)
    backend, sess = argocd_backend(broken_cluster_routes)
    ctx = make_ctx(backend, log_tail_max=50)
    raw = _tools(ctx)["pod_logs"].run(app_name="demo-cl", pod="web-abc-2", namespace="demo-cl",
                                     tail_lines=5000, previous=True)
    out = json.loads(raw)
    assert "tail_lines" in out, raw
    assert out["tail_lines"] == 50
    assert "hunter" not in out["lines"]
    log_call = next(c for c in sess.calls if c[1].endswith("/logs"))
    assert log_call[2]["tailLines"] == 50 and log_call[2]["follow"] == "false" and log_call[2]["previous"] == "true"
    assert out["container"] == "web"


def test_events_use_tree_uid(broken_cluster_routes):
    backend, sess = argocd_backend(broken_cluster_routes)
    out = json.loads(_tools(make_ctx(backend))["k8s_events"].run(app_name="demo-ip", kind="Pod", name="web-abc-1",
                                                                 namespace="demo-ip"))
    assert out["events"][0]["type"] == "Warning" and "not found" in out["events"][0]["message"]
    call = next(c for c in sess.calls if c[1].endswith("/events"))
    assert call[2]["resourceUID"] == "uid-web-abc-1"


def test_tool_output_is_size_bounded(broken_cluster_routes):
    many = [application(f"app-{i}", "OutOfSync", "Degraded", health_msg="x" * 3000) for i in range(300)]
    broken_cluster_routes["/api/v1/applications"] = {"items": many}
    backend, _ = argocd_backend(broken_cluster_routes)
    ctx = make_ctx(backend, max_tool_output_chars=4000)
    out = _tools(ctx)["argocd_list_applications"].run()
    assert len(out) <= 4100
    json.loads(out)


def test_access_check_argocd(broken_cluster_routes):
    backend, _ = argocd_backend(broken_cluster_routes)
    out = json.loads(_tools(make_ctx(backend))["argocd_access_check"].run())
    assert out["read_permissions"] == {"get applications": True, "get logs": True, "get projects": True}
    assert not any(v is True for v in out["write_or_sensitive_permissions"].values())
    assert out["warnings"] == []
    assert out["metrics_sources"]["prometheus"]["available"] is False


def test_triage_finds_both_root_causes(broken_cluster_routes):
    backend, sess = argocd_backend(broken_cluster_routes)
    ctx = make_ctx(backend)
    rep = run_triage(ctx, Scope(all=True))
    by = {a.app_name: a for a in rep.apps}
    assert by["good"].verdict == "healthy"
    ip, cl, nv = by["demo-ip"], by["demo-cl"], by["never"]
    assert ip.root_cause_category == "image_pull" and ip.confidence == "high"
    assert any(e.source == "k8s_event" and "v0-nope: not found" in e.detail for e in ip.evidence)
    assert "gcr.io/x/web:v0-nope" in ip.probable_root_cause
    assert cl.root_cause_category == "crash_loop"
    assert any(e.source == "container_log" and "REDIS_URL is not set" in e.detail for e in cl.evidence)
    assert any(e.source == "live_resource" and "exitCode=1" in e.detail for e in cl.evidence)
    assert nv.root_cause_category == "never_synced"
    assert ctx.ledger.used["argocd-api"] > 5
    assert all(m == "GET" for m, *_ in sess.calls)


# --------------------------------------------------------------------------- kube backend
def _kube_routes():
    ns = "demo-ip"
    app = application("demo-ip", "Synced", "Progressing", ns=ns)
    bad = pod("web-abc-1", ns, owner_uid="rs-uid", waiting=("ErrImagePull", 'failed to pull "gcr.io/x/web:v0-nope"'))
    rs = {"metadata": {"name": "web-abc", "namespace": ns, "uid": "rs-uid",
                       "ownerReferences": [{"kind": "Deployment", "name": "web", "uid": "dep-uid"}]},
          "spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": 0}}
    old_rs = {"metadata": {"name": "web-old", "namespace": ns, "uid": "rs-old",
                           "ownerReferences": [{"kind": "Deployment", "name": "web", "uid": "dep-uid"}]},
              "spec": {"replicas": 0}, "status": {}}
    dep = {"metadata": {"name": "web", "namespace": ns, "uid": "dep-uid"},
           "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "gcr.io/x/web:v0-nope"}]}}},
           "status": {"conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable",
                                      "message": "Deployment does not have minimum availability."}]}}
    unrelated = pod("other", ns, owner_uid="someone-else")
    return {
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications": {"items": [app]},
        "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/demo-ip": app,
        f"/apis/apps/v1/namespaces/{ns}/deployments/web": dep,
        f"/apis/apps/v1/namespaces/{ns}/replicasets": {"items": [rs, old_rs]},
        f"/apis/batch/v1/namespaces/{ns}/jobs": {"items": []},
        f"/api/v1/namespaces/{ns}/pods": {"items": [bad, unrelated]},
        f"/api/v1/namespaces/{ns}/pods/web-abc-1": bad,
        f"/api/v1/namespaces/{ns}/events": {"items": [
            {"type": "Warning", "reason": "Failed", "message": 'Failed to pull image "gcr.io/x/web:v0-nope": not found',
             "lastTimestamp": NOW, "involvedObject": {"kind": "Pod", "namespace": ns, "name": "web-abc-1"}}]},
        f"/api/v1/namespaces/{ns}/pods/web-abc-1/log": "2026-11-24T05:00:00Z hello\n",
    }


def test_kube_backend_builds_tree_from_owner_refs():
    backend, reader = kube_backend(_kube_routes())
    tree = backend.resource_tree("demo-ip")
    refs = {f"{n['kind']}/{n['name']}" for n in tree["nodes"]}
    assert {"Deployment/web", "ReplicaSet/web-abc", "Pod/web-abc-1", "Service/web"} <= refs
    assert "Pod/other" not in refs and "ReplicaSet/web-old" not in refs
    p = next(n for n in tree["nodes"] if n["name"] == "web-abc-1")
    assert p["health"]["status"] == "Degraded" and p["info"][0]["value"] == "ErrImagePull"
    assert all(m == "GET" for m, *_ in reader.calls)


def test_kube_triage_and_access(monkeypatch):
    backend, reader = kube_backend(_kube_routes())
    ctx = make_ctx(backend)
    rep = run_triage(ctx, Scope(apps=["demo-ip"]))
    d = rep.apps[0]
    assert d.root_cause_category == "image_pull"
    assert any("v0-nope" in e.detail for e in d.evidence if e.source == "k8s_event")
    acc = ctx.access_report()
    assert acc["read_permissions"]["list pods"] is True
    assert acc["write_or_sensitive_permissions"]["get secrets"] is True  # fake answers yes to get/list
    assert any("write/sensitive" in w for w in acc["warnings"])
    # the only POST ever sent is the SelfSubjectAccessReview
    assert {p for m, p, _ in reader.calls if m != "GET"} == {"/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"}
