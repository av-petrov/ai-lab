"""Fixtures: mocked Argo CD API / Kubernetes API responses (no cluster, no LLM)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import pytest

from argocd_observability.backends import ArgoCDApiBackend, KubeBackend
from argocd_observability.context import ObservabilityContext
from argocd_observability.guard import check_argocd_request
from argocd_observability.http import GuardedHttpClient
from argocd_observability.kube_client import KubeReader, ReadOnlyApiClient
from argocd_observability.metrics import MetricsService
from argocd_observability.settings import ObservabilitySettings

NOW = "2026-11-24T05:00:00Z"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("ARGOCD_SERVER", "ARGOCD_AUTH_TOKEN", "PROMETHEUS_URL", "ARGOCD_OBS_AUTH_MODE", "MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-000000")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


def settings(**kw: Any) -> ObservabilitySettings:
    return ObservabilitySettings(_env_file=None, scrape_argocd_metrics=False, **kw)


# --------------------------------------------------------------------------- builders
def application(name: str, sync: str = "Synced", health: str = "Healthy", *, ns: str | None = None,
                health_msg: str | None = None, op: dict | None = None, automated: bool = True,
                conditions: list | None = None, resources: list | None = None) -> dict:
    ns = ns or name
    return {
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"project": "default", "destination": {"server": "https://kubernetes.default.svc", "namespace": ns},
                 "source": {"repoURL": "https://github.com/argoproj/argocd-example-apps.git", "path": "kustomize-guestbook",
                            "targetRevision": "master"},
                 "syncPolicy": {"automated": {"prune": True}} if automated else {}},
        "status": {
            "sync": {"status": sync, "revision": "abc123def456"},
            "health": {"status": health, **({"message": health_msg} if health_msg else {}),
                       "lastTransitionTime": NOW},
            "operationState": op if op is not None else ({"phase": "Succeeded", "message": "successfully synced",
                                                          "startedAt": NOW, "finishedAt": NOW,
                                                          "syncResult": {"revision": "abc123def456", "resources": []}}
                                                         if automated else None),
            "conditions": conditions or [],
            "resources": resources if resources is not None else [
                {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": ns, "name": "web",
                 "status": "Synced", "health": {"status": health}},
                {"version": "v1", "kind": "Service", "namespace": ns, "name": "web", "status": "Synced",
                 "health": {"status": "Healthy"}},
            ],
            "reconciledAt": NOW,
        },
    }


def pod(name: str, ns: str, *, owner_uid: str = "rs-uid", waiting: tuple[str, str] | None = None,
        last_exit: tuple[str, int] | None = None, ready: bool = False, env: list | None = None) -> dict:
    cs: dict[str, Any] = {"name": "web", "image": "busybox:1.36", "ready": ready, "restartCount": 5 if last_exit else 0}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting[0], "message": waiting[1]}}
    else:
        cs["state"] = {"running": {"startedAt": NOW}}
    if last_exit:
        cs["lastState"] = {"terminated": {"reason": last_exit[0], "exitCode": last_exit[1], "finishedAt": NOW}}
    return {
        "metadata": {"name": name, "namespace": ns, "uid": f"uid-{name}", "creationTimestamp": NOW,
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "web-abc", "uid": owner_uid}]},
        "spec": {"nodeName": "worker", "containers": [{"name": "web", "image": "busybox:1.36",
                                                      "env": env or [{"name": "DB_PASSWORD", "value": "hunter2"}]}]},
        "status": {"phase": "Running" if not waiting else "Pending", "containerStatuses": [cs]},
    }


def tree_for(ns: str, pod_obj: dict, pod_health: str, reason: str, msg: str) -> dict:
    return {"nodes": [
        {"kind": "Deployment", "group": "apps", "version": "v1", "namespace": ns, "name": "web", "uid": "dep-uid",
         "health": {"status": "Progressing", "message": "Deployment exceeded its progress deadline"}},
        {"kind": "ReplicaSet", "group": "apps", "version": "v1", "namespace": ns, "name": "web-abc", "uid": "rs-uid",
         "parentRefs": [{"kind": "Deployment", "name": "web", "namespace": ns, "uid": "dep-uid"}],
         "health": {"status": "Progressing"}},
        {"kind": "Pod", "version": "v1", "namespace": ns, "name": pod_obj["metadata"]["name"],
         "uid": pod_obj["metadata"]["uid"],
         "parentRefs": [{"kind": "ReplicaSet", "name": "web-abc", "namespace": ns, "uid": "rs-uid"}],
         "health": {"status": pod_health, "message": msg},
         "info": [{"name": "Status Reason", "value": reason}, {"name": "Containers", "value": "0/1"}],
         "images": ["busybox:1.36"]},
        {"kind": "Service", "version": "v1", "namespace": ns, "name": "web", "uid": "svc-uid",
         "health": {"status": "Healthy"}},
    ]}


# --------------------------------------------------------------------------- fake HTTP for Argo CD
class FakeResponse:
    def __init__(self, status: int, body: Any, lines: list[dict] | None = None) -> None:
        self.status_code = status
        self._raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self._lines = [json.dumps(x).encode() for x in (lines or [])]
        self.text = self._raw.decode()

    def iter_content(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i:i + n]

    def iter_lines(self):
        yield from self._lines

    def close(self) -> None:
        pass


class FakeSession:
    """Routes GETs by path; records every call. Any non-GET method raises AssertionError."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, url: str, params=None, **_: Any) -> FakeResponse:
        path = urlsplit(url).path
        self.calls.append(("GET", path, params))
        if path not in self.routes:
            return FakeResponse(404, {"error": "not found"})
        val = self.routes[path]
        if callable(val):
            val = val(params or {})
        if isinstance(val, FakeResponse):
            return val
        return FakeResponse(200, val)

    def __getattr__(self, item: str):
        if item in ("post", "put", "patch", "delete", "request", "head", "options"):
            raise AssertionError(f"session.{item} must never be called")
        raise AttributeError(item)


def argocd_backend(routes: dict[str, Any]) -> tuple[ArgoCDApiBackend, FakeSession]:
    sess = FakeSession(routes)
    http = GuardedHttpClient("https://argocd.test", check_argocd_request, session=sess)
    return ArgoCDApiBackend("https://argocd.test", "tok", http=http), sess


def make_ctx(backend, **kw) -> ObservabilityContext:
    s = settings(**kw)
    return ObservabilityContext(settings=s, backend=backend, kube=None, metrics=MetricsService(s, kube=None))


@pytest.fixture()
def broken_cluster_routes() -> dict[str, Any]:
    """Argo CD API responses for: healthy app, ImagePullBackOff app, CrashLoopBackOff app, never-synced app."""
    ip_pod = pod("web-abc-1", "demo-ip", waiting=("ImagePullBackOff", 'Back-off pulling image "gcr.io/x/web:v0-nope"'))
    cl_pod = pod("web-abc-2", "demo-cl", waiting=("CrashLoopBackOff", "back-off 5m0s restarting failed container=web"),
                 last_exit=("Error", 1))
    apps = {
        "good": application("good"),
        "demo-ip": application("demo-ip", "Synced", "Progressing", op={"phase": "Succeeded", "message": "ok",
                                                                         "startedAt": NOW, "finishedAt": NOW}),
        "demo-cl": application("demo-cl", "Synced", "Degraded"),
        "never": application("never", "OutOfSync", "Missing", automated=False, op={}, resources=[
            {"group": "apps", "version": "v1", "kind": "Deployment", "namespace": "never", "name": "hello",
             "status": "OutOfSync", "health": {"status": "Missing"}}]),
    }
    apps["never"]["status"].pop("operationState")
    routes: dict[str, Any] = {"/api/v1/applications": {"items": list(apps.values())}}
    for n, a in apps.items():
        routes[f"/api/v1/applications/{n}"] = a
        routes[f"/api/v1/applications/{n}/resource-tree"] = {"nodes": []}
    routes["/api/v1/applications/demo-ip/resource-tree"] = tree_for(
        "demo-ip", ip_pod, "Progressing", "ImagePullBackOff", 'Back-off pulling image "gcr.io/x/web:v0-nope"')
    routes["/api/v1/applications/demo-cl/resource-tree"] = tree_for(
        "demo-cl", cl_pod, "Degraded", "CrashLoopBackOff", "back-off 5m0s restarting failed container=web")

    def resource(params):
        p = {"web-abc-1": ip_pod, "web-abc-2": cl_pod}.get(params.get("resourceName"))
        return {"manifest": json.dumps(p)} if p else FakeResponse(404, {"error": "nope"})

    routes["/api/v1/applications/demo-ip/resource"] = resource
    routes["/api/v1/applications/demo-cl/resource"] = resource
    routes["/api/v1/applications/demo-ip/events"] = {"items": [
        {"type": "Warning", "reason": "Failed", "count": 4, "lastTimestamp": NOW,
         "message": 'Failed to pull image "gcr.io/x/web:v0-nope": rpc error: code = NotFound desc = '
                    'gcr.io/x/web:v0-nope: not found',
         "involvedObject": {"kind": "Pod", "namespace": "demo-ip", "name": "web-abc-1"}},
        {"type": "Normal", "reason": "Pulling", "count": 4, "lastTimestamp": NOW,
         "message": 'Pulling image "gcr.io/x/web:v0-nope"',
         "involvedObject": {"kind": "Pod", "namespace": "demo-ip", "name": "web-abc-1"}},
    ]}
    routes["/api/v1/applications/demo-cl/events"] = {"items": [
        {"type": "Warning", "reason": "BackOff", "count": 20, "lastTimestamp": NOW,
         "message": "Back-off restarting failed container web in pod web-abc-2",
         "involvedObject": {"kind": "Pod", "namespace": "demo-cl", "name": "web-abc-2"}}]}
    routes["/api/v1/applications/demo-cl/logs"] = FakeResponse(200, b"", lines=[
        {"result": {"content": "guestbook-ui: loading config", "timeStampStr": NOW, "podName": "web-abc-2"}},
        {"result": {"content": "FATAL: required environment variable REDIS_URL is not set", "timeStampStr": NOW,
                    "podName": "web-abc-2"}},
        {"result": {"content": "", "last": True}},
    ])
    routes["/api/v1/session/userinfo"] = {"loggedIn": True, "username": "sre-observer", "iss": "argocd"}
    routes["/api/version"] = {"Version": "v3.6.2"}
    obj = {"applications": "*%2F*", "logs": "*%2F*", "exec": "*%2F*", "projects": "*"}
    for res, act in (("applications", "get"), ("logs", "get"), ("projects", "get")):
        routes[f"/api/v1/account/can-i/{res}/{act}/{obj[res]}"] = {"value": "yes"}
    for res, act in (("applications", "sync"), ("applications", "update"), ("applications", "delete"),
                     ("applications", "action%2F*"), ("exec", "create")):
        routes[f"/api/v1/account/can-i/{res}/{act}/{obj[res]}"] = {"value": "no"}
    return routes


# --------------------------------------------------------------------------- fake kube reader
class FakeKubeReader(KubeReader):
    def __init__(self, routes: dict[str, Any]) -> None:
        cfg = __import__("kubernetes").client.Configuration()
        cfg.host = "https://192.0.2.1:6443"  # TEST-NET, never contacted
        super().__init__(ReadOnlyApiClient(configuration=cfg), context_name="fake")
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def _call(self, method, path, query=None, body=None, timeout=None):  # type: ignore[override]
        from argocd_observability.guard import check_kube_request

        check_kube_request(method, path, query)  # same guard as the real transport
        self.calls.append((method, path, query))
        if method == "POST":
            return json.dumps({"status": {"allowed": body["spec"]["resourceAttributes"]["verb"] in ("get", "list")}}).encode()
        val = self.routes.get(path)
        if val is None:
            raise RuntimeError(f"404 {path}")
        if callable(val):
            val = val(query or {})
        return val.encode() if isinstance(val, str) else json.dumps(val).encode()


def kube_backend(routes: dict[str, Any]) -> tuple[KubeBackend, FakeKubeReader]:
    r = FakeKubeReader(routes)
    return KubeBackend(r, "argocd"), r
