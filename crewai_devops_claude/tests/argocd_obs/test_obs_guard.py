"""The read-only guard rejects every mutating or sensitive call - at every layer."""

from __future__ import annotations

import json

import pytest
from kubernetes import client as k8s

from argocd_observability.guard import (
    ReadOnlyViolation,
    check_argocd_request,
    check_kube_request,
    check_prometheus_request,
)
from argocd_observability.http import GuardedHttpClient
from argocd_observability.kube_client import KubeReader, ReadOnlyApiClient

from .conftest import argocd_backend, make_ctx


# --------------------------------------------------------------------------- Argo CD API
@pytest.mark.parametrize("path,params", [
    ("/api/v1/applications", None),
    ("/api/v1/applications", {"projects": "apps", "selector": "team=x"}),
    ("/api/v1/applications/hello-test", None),
    ("/api/v1/applications/hello-test/resource-tree", None),
    ("/api/v1/applications/hello-test/managed-resources", None),
    ("/api/v1/applications/hello-test/events", {"resourceName": "p", "resourceNamespace": "n", "resourceUID": "u"}),
    ("/api/v1/applications/hello-test/logs", {"podName": "p", "tailLines": 100, "follow": "false"}),
    ("/api/v1/applications/hello-test/resource", {"kind": "Pod", "resourceName": "p", "namespace": "n"}),
    ("/api/v1/projects/apps", None),
    ("/api/v1/session/userinfo", None),
    ("/api/v1/account/can-i/applications/sync/*", None),
    ("/api/version", None),
])
def test_argocd_allowed_gets(path, params):
    check_argocd_request("GET", path, params)


@pytest.mark.parametrize("method,path,params", [
    ("POST", "/api/v1/applications/hello-test/sync", None),                 # sync
    ("PUT", "/api/v1/applications/hello-test/rollback", None),              # rollback
    ("POST", "/api/v1/applications/hello-test/rollback", None),
    ("DELETE", "/api/v1/applications/hello-test", None),                    # delete app
    ("DELETE", "/api/v1/applications/hello-test/operation", None),          # terminate operation
    ("PATCH", "/api/v1/applications/hello-test", None),                     # patch app
    ("PUT", "/api/v1/applications/hello-test/spec", None),
    ("POST", "/api/v1/applications/hello-test/resource/actions", None),     # resource actions (restart...)
    ("DELETE", "/api/v1/applications/hello-test/resource", None),           # delete resource
    ("POST", "/api/v1/applications/hello-test/resource", None),             # patch resource
    ("POST", "/api/v1/session", None),
    ("POST", "/api/v1/account/sre-observer/token", None),
    ("GET", "/terminal", {"pod": "x"}),                                     # exec / web terminal
    ("GET", "/api/v1/applications/hello-test/terminal", None),
    ("GET", "/api/v1/applications/hello-test", {"refresh": "hard"}),        # refresh=hard
    ("GET", "/api/v1/applications/hello-test", {"refresh": "normal"}),      # any refresh
    ("GET", "/api/v1/applications?refresh=hard", None),                     # refresh in the URL
    ("GET", "/api/v1/applications/hello-test/logs", {"follow": "true"}),    # unbounded stream
    ("GET", "/api/v1/applications/hello-test/resource", {"kind": "Secret", "resourceName": "s"}),
    ("GET", "/api/v1/applications/hello-test/manifests", None),             # not allow-listed
    ("GET", "/api/v1/clusters", None),                                      # cluster creds
    ("GET", "/api/v1/repositories", None),                                  # repo creds
    ("GET", "/api/v1/certificates", None),
])
def test_argocd_rejects(method, path, params):
    with pytest.raises(ReadOnlyViolation) as ei:
        check_argocd_request(method, path, params)
    assert "REFUSED" in str(ei.value)


def test_http_client_never_sends_mutations():
    _, sess = argocd_backend({})
    http = GuardedHttpClient("https://argocd.test", check_argocd_request, session=sess)
    for call in (lambda: http.post("/api/v1/applications/x/sync"),
                 lambda: http.delete("/api/v1/applications/x"),
                 lambda: http.patch("/api/v1/applications/x"),
                 lambda: http.put("/api/v1/applications/x/rollback"),
                 lambda: http.request("POST", "/api/v1/applications/x/sync"),
                 lambda: http.get_json("/api/v1/applications/x", {"refresh": "hard"})):
        with pytest.raises(ReadOnlyViolation):
            call()
    assert sess.calls == []  # nothing ever left the process


# --------------------------------------------------------------------------- Kubernetes API
@pytest.mark.parametrize("path,params", [
    ("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications", None),
    ("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/hello-test", None),
    ("/api/v1/namespaces/demo/pods", {"labelSelector": "a=b"}),
    ("/api/v1/namespaces/demo/pods/web-1", None),
    ("/api/v1/namespaces/demo/pods/web-1/log", {"tailLines": 200, "previous": "true"}),
    ("/api/v1/namespaces/demo/events", {"fieldSelector": "involvedObject.name=web-1"}),
    ("/api/v1/namespaces/demo/configmaps/app-config", None),
    ("/apis/apps/v1/namespaces/demo/deployments/web", None),
    ("/apis/apps/v1/namespaces/demo/replicasets", None),
    ("/apis/batch/v1/namespaces/demo/jobs", None),
    ("/apis/networking.k8s.io/v1/namespaces/demo/ingresses", None),
    ("/apis/metrics.k8s.io/v1beta1/namespaces/demo/pods", None),
    ("/api/v1/namespaces/argocd/pods/argocd-server-x:8083/proxy/metrics", None),
    ("/api/v1/namespaces/argocd/services/argocd-metrics:8082/proxy/metrics", None),
    ("/apis/apiregistration.k8s.io/v1/apiservices/v1beta1.metrics.k8s.io", None),
    ("/api/v1/namespaces", None),
    ("/api/v1/nodes", None),
    ("/version", None),
])
def test_kube_allowed_reads(path, params):
    check_kube_request("GET", path, params)


def test_kube_allows_only_ssar_post():
    check_kube_request("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews")
    with pytest.raises(ReadOnlyViolation):
        check_kube_request("POST", "/apis/authorization.k8s.io/v1/subjectaccessreviews")


@pytest.mark.parametrize("method,path,params", [
    ("GET", "/api/v1/namespaces/demo/secrets", None),                       # no Secrets
    ("GET", "/api/v1/namespaces/demo/secrets/db", None),
    ("GET", "/api/v1/secrets", None),
    ("POST", "/api/v1/namespaces/demo/pods/web-1/exec", None),             # exec
    ("GET", "/api/v1/namespaces/demo/pods/web-1/exec", {"command": "sh"}),
    ("GET", "/api/v1/namespaces/demo/pods/web-1/attach", None),
    ("POST", "/api/v1/namespaces/demo/pods/web-1/portforward", None),      # port-forward
    ("GET", "/api/v1/namespaces/demo/pods/web-1/portforward", None),
    ("GET", "/api/v1/namespaces/demo/pods/web-1:8080/proxy/admin", None),  # proxy to non-metrics
    ("GET", "/api/v1/namespaces/demo/services/web:80/proxy/", None),
    ("POST", "/api/v1/namespaces/demo/pods", None),                        # create
    ("PUT", "/apis/apps/v1/namespaces/demo/deployments/web", None),        # apply/replace
    ("PATCH", "/apis/apps/v1/namespaces/demo/deployments/web", None),      # patch
    ("PATCH", "/apis/apps/v1/namespaces/demo/deployments/web/scale", None),  # scale
    ("GET", "/apis/apps/v1/namespaces/demo/deployments/web/scale", None),
    ("DELETE", "/api/v1/namespaces/demo/pods/web-1", None),                # delete
    ("PATCH", "/api/v1/nodes/worker", None),                               # cordon
    ("POST", "/api/v1/namespaces/demo/pods/web-1/eviction", None),         # drain
    ("PATCH", "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/x", None),  # app sync via CR
    ("GET", "/api/v1/namespaces/demo/pods/web-1/log", {"follow": "true"}),
    ("GET", "/api/v1/namespaces/demo/serviceaccounts/default/token", None),
    ("GET", "/apis/rbac.authorization.k8s.io/v1/clusterroles", None),      # not allow-listed
    ("GET", "/api/v1/namespaces/demo/nodes", None),                        # cluster-scoped under ns
])
def test_kube_rejects(method, path, params):
    with pytest.raises(ReadOnlyViolation):
        check_kube_request(method, path, params)


def _offline_api_client() -> ReadOnlyApiClient:
    cfg = k8s.Configuration()
    cfg.host = "https://192.0.2.1:6443"  # TEST-NET-1: a request would hang/fail, not succeed
    cfg.retries = 0
    return ReadOnlyApiClient(configuration=cfg)


@pytest.mark.parametrize("call", [
    lambda c: k8s.CoreV1Api(c).delete_namespaced_pod("web", "demo"),
    lambda c: k8s.CoreV1Api(c).read_namespaced_secret("db", "demo"),
    lambda c: k8s.CoreV1Api(c).list_secret_for_all_namespaces(),
    lambda c: k8s.CoreV1Api(c).create_namespaced_pod("demo", {"metadata": {"name": "x"}}),
    lambda c: k8s.CoreV1Api(c).patch_node("worker", {"spec": {"unschedulable": True}}),
    lambda c: k8s.AppsV1Api(c).patch_namespaced_deployment_scale("web", "demo", {"spec": {"replicas": 0}}),
    lambda c: k8s.AppsV1Api(c).delete_namespaced_deployment("web", "demo"),
    lambda c: k8s.CustomObjectsApi(c).patch_namespaced_custom_object(
        "argoproj.io", "v1alpha1", "argocd", "applications", "x", {"operation": {"sync": {}}}),
    lambda c: k8s.CoreV1Api(c).connect_post_namespaced_pod_exec("web", "demo", command="sh"),
])
def test_typed_kubernetes_client_is_guarded_at_transport(call):
    """Even the official typed client cannot mutate: ReadOnlyApiClient.request() refuses first."""
    with pytest.raises(ReadOnlyViolation):
        call(_offline_api_client())


def test_kube_reader_requires_guarded_client():
    with pytest.raises(TypeError):
        KubeReader(k8s.ApiClient())


# --------------------------------------------------------------------------- Prometheus
def test_prometheus_guard():
    check_prometheus_request("GET", "/api/v1/query", {"query": "up"})
    check_prometheus_request("GET", "/prom/api/v1/query_range", None)
    for m, p in (("POST", "/api/v1/query"), ("POST", "/api/v1/admin/tsdb/delete_series"),
                 ("GET", "/api/v1/admin/tsdb/snapshot"), ("GET", "/-/reload"), ("GET", "/api/v1/targets")):
        with pytest.raises(ReadOnlyViolation):
            check_prometheus_request(m, p)


# --------------------------------------------------------------------------- tool layer
def test_tools_return_refusal_for_secrets(broken_cluster_routes):
    from argocd_observability.tools import build_tools

    backend, sess = argocd_backend(broken_cluster_routes)
    ctx = make_ctx(backend)
    tools = {t.name: t for t in build_tools(ctx)}
    out = json.loads(tools["argocd_live_resource_status"].run(app_name="demo-cl", kind="Secret", name="db",
                                                              namespace="demo-cl"))
    assert "refused" in out and "Secrets" in out["refused"]
    assert ctx.ledger.refusals
    assert all(c[0] == "GET" for c in sess.calls)


def test_tools_refuse_resources_outside_app_tree(broken_cluster_routes):
    from argocd_observability.tools import build_tools

    backend, _ = argocd_backend(broken_cluster_routes)
    tools = {t.name: t for t in build_tools(make_ctx(backend))}
    out = json.loads(tools["pod_logs"].run(app_name="demo-cl", pod="kube-apiserver-x", namespace="kube-system"))
    assert "not part of application" in out["error"]


def test_can_i_questions_allowed_but_exec_still_refused():
    for p in ("/api/v1/account/can-i/exec/create/*%2F*", "/api/v1/account/can-i/applications/sync/*%2F*",
              "/api/v1/account/can-i/applications/action%2F*/*%2F*"):
        check_argocd_request("GET", p)
    for p in ("/api/v1/applications/x/exec", "/terminal", "/api/v1/account/can-i/exec/create/*/extra",
              "/api/v1/account/exec"):
        with pytest.raises(ReadOnlyViolation):
            check_argocd_request("GET", p)


def test_access_check_uses_project_app_objects(broken_cluster_routes):
    from argocd_observability.tools import build_tools

    backend, sess = argocd_backend(broken_cluster_routes)
    out = json.loads({t.name: t for t in build_tools(make_ctx(backend))}["argocd_access_check"].run())
    assert out["read_permissions"] == {"get applications": True, "get logs": True, "get projects": True}
    assert out["write_or_sensitive_permissions"]["create exec"] is False
    paths = {c[1] for c in sess.calls}
    assert "/api/v1/account/can-i/applications/get/*%2F*" in paths
    assert "/api/v1/account/can-i/projects/get/*" in paths
