"""Read-only guard: the single choke point for every outbound request.

Every HTTP call to Argo CD, the Kubernetes API or Prometheus goes through one of the
``check_*`` functions below *before* any bytes leave the process:

* Argo CD API  -> ``check_argocd_request``   (GET only, path allow-list)
* Kubernetes   -> ``check_kube_request``     (GET only on an allow-list of read paths, plus the
                                              SelfSubjectAccessReview POST, which only *asks* the
                                              API server a question and changes nothing)
* Prometheus   -> ``check_prometheus_request`` (GET only on the query endpoints)

Anything not explicitly allowed raises :class:`ReadOnlyViolation`. The lists are allow-lists,
so a new or unknown endpoint is refused by default. Secrets are refused everywhere.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit


class ReadOnlyViolation(PermissionError):
    """Raised when a caller (usually the LLM through a tool) requests a non read-only operation."""

    def __init__(self, target: str, method: str, path: str, reason: str) -> None:
        self.target, self.method, self.path, self.reason = target, method.upper(), path, reason
        super().__init__(
            f"REFUSED by read-only guard [{target}] {self.method} {path} -> {reason}. "
            "This agent is strictly read-only; it never syncs, patches, deletes, execs or reads Secrets."
        )


_NAME = r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"
_SEG = r"[^/]+"

# --------------------------------------------------------------------------- Argo CD API
_ARGOCD_GET_ALLOW = [
    r"/api/version",
    r"/api/v1/version",
    r"/api/v1/settings",
    r"/api/v1/session/userinfo",
    rf"/api/v1/account/can-i/{_SEG}/{_SEG}/{_SEG}",
    r"/api/v1/applications",
    rf"/api/v1/applications/{_NAME}",
    rf"/api/v1/applications/{_NAME}/resource-tree",
    rf"/api/v1/applications/{_NAME}/managed-resources",
    rf"/api/v1/applications/{_NAME}/events",
    rf"/api/v1/applications/{_NAME}/logs",
    rf"/api/v1/applications/{_NAME}/resource",
    rf"/api/v1/applications/{_NAME}/revisions/{_SEG}/metadata",
    r"/api/v1/projects",
    rf"/api/v1/projects/{_NAME}",
]
_ARGOCD_GET_RE = [re.compile(f"^{p}$") for p in _ARGOCD_GET_ALLOW]

# Query parameters that turn an otherwise harmless GET into a state change or an unbounded stream.
#   refresh=normal|hard  -> triggers reconciliation (hard: also invalidates the manifest cache)
#   follow=true          -> unbounded log stream
_ARGOCD_FORBIDDEN_PARAMS = {"refresh": "refresh triggers a reconcile (state change)"}


def _params(path: str, params: Mapping[str, object] | Iterable[tuple[str, object]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    query = urlsplit(path).query
    if query:
        out.update(dict(parse_qsl(query)))
    if params:
        items = params.items() if isinstance(params, Mapping) else params
        for k, v in items:
            out[str(k)] = str(v)
    return out


_CAN_I_RE = re.compile(rf"^/api/v1/account/can-i/{_SEG}/{_SEG}/{_SEG}$")


def check_argocd_request(method: str, path: str, params=None) -> None:
    clean = urlsplit(path).path.rstrip("/") or "/"
    if method.upper() != "GET":
        raise ReadOnlyViolation("argocd", method, clean, "only GET requests are allowed")
    # account/can-i only *asks* whether an action is allowed (e.g. can-i/exec/create/*): a question, not an exec
    is_can_i = _CAN_I_RE.match(clean) is not None
    if not is_can_i and ("/terminal" in clean or "/exec" in clean):
        raise ReadOnlyViolation("argocd", method, clean, "terminal/exec is never allowed")
    if not any(r.match(clean) for r in _ARGOCD_GET_RE):
        raise ReadOnlyViolation("argocd", method, clean, "endpoint is not on the read-only allow-list")
    q = _params(path, params)
    for key, why in _ARGOCD_FORBIDDEN_PARAMS.items():
        if key in q and q[key] not in ("", "false"):
            raise ReadOnlyViolation("argocd", method, clean, f"query parameter '{key}' is forbidden ({why})")
    if q.get("follow", "false").lower() == "true":
        raise ReadOnlyViolation("argocd", method, clean, "follow=true (unbounded stream) is forbidden")
    if clean.endswith("/resource") and q.get("kind", "") == "Secret":
        raise ReadOnlyViolation("argocd", method, clean, "reading Secrets is forbidden")


# --------------------------------------------------------------------------- Kubernetes API
_NS = rf"/namespaces/{_NAME}"
_KUBE_RESOURCES = {
    # group/version prefix          -> readable plurals
    "/api/v1": {"pods", "events", "services", "endpoints", "persistentvolumeclaims",
                "replicationcontrollers", "namespaces", "nodes", "serviceaccounts", "limitranges", "configmaps",
                "resourcequotas"},
    "/apis/apps/v1": {"deployments", "replicasets", "statefulsets", "daemonsets"},
    "/apis/batch/v1": {"jobs", "cronjobs"},
    "/apis/networking.k8s.io/v1": {"ingresses", "networkpolicies"},
    "/apis/autoscaling/v2": {"horizontalpodautoscalers"},
    "/apis/policy/v1": {"poddisruptionbudgets"},
    "/apis/events.k8s.io/v1": {"events"},
    "/apis/argoproj.io/v1alpha1": {"applications", "appprojects", "applicationsets", "rollouts"},
    "/apis/metrics.k8s.io/v1beta1": {"pods", "nodes"},
    "/apis/cilium.io/v2": {"ciliumnetworkpolicies", "ciliumclusterwidenetworkpolicies"},
}
_CLUSTER_SCOPED = {"namespaces", "nodes", "ciliumclusterwidenetworkpolicies"}
_KUBE_GET_EXTRA = [
    re.compile(r"^/version$"),
    re.compile(r"^/api$"),
    re.compile(r"^/apis$"),
    re.compile(r"^/apis/apiregistration\.k8s\.io/v1/apiservices(/[^/]+)?$"),
    # pod logs
    re.compile(rf"^/api/v1{_NS}/pods/{_NAME}/log$"),
    # Prometheus metrics endpoints through the API server proxy: only the /metrics path, numeric port
    re.compile(rf"^/api/v1{_NS}/(pods|services)/{_NAME}:[0-9]{{2,5}}/proxy/metrics$"),
]
# The only non-GET call: a SelfSubjectAccessReview is a question to the authorizer, it persists nothing.
_KUBE_POST_ALLOW = [re.compile(r"^/apis/authorization\.k8s\.io/v1/selfsubjectaccessreviews$")]
_KUBE_FORBIDDEN_FRAGMENTS = ("/secrets", "/exec", "/attach", "/portforward", "/eviction", "/binding",
                             "/ephemeralcontainers", "/token", "/scale")


def _kube_resource_path_ok(path: str) -> bool:
    for prefix, plurals in _KUBE_RESOURCES.items():
        if not path.startswith(prefix + "/"):
            continue
        rest = path[len(prefix):]
        m = re.match(rf"^(?:{_NS})?/(?P<plural>[a-z0-9]+)(?:/{_NAME})?(?:/status)?$", rest)
        if m and m.group("plural") in plurals:
            namespaced = rest.startswith("/namespaces/") and m.group("plural") != "namespaces"
            if m.group("plural") in _CLUSTER_SCOPED and namespaced:
                return False
            return True
        # GET /api/v1/namespaces/<name>
        if prefix == "/api/v1" and re.match(rf"^/namespaces/{_NAME}$", rest):
            return True
    return False


def check_kube_request(method: str, url_or_path: str, params=None) -> None:
    parts = urlsplit(url_or_path)
    path = parts.path.rstrip("/") or "/"
    m = method.upper()
    low = path.lower()
    if any(f in low for f in _KUBE_FORBIDDEN_FRAGMENTS):
        raise ReadOnlyViolation("kubernetes", m, path, "secrets/exec/attach/port-forward/scale/eviction are forbidden")
    if m == "POST":
        if any(r.match(path) for r in _KUBE_POST_ALLOW):
            return
        raise ReadOnlyViolation("kubernetes", m, path, "only SelfSubjectAccessReview may be POSTed")
    if m != "GET":
        raise ReadOnlyViolation("kubernetes", m, path, "only read verbs (get/list/watch/logs) are allowed")
    q = _params(url_or_path, params)
    if q.get("follow", "false").lower() == "true":
        raise ReadOnlyViolation("kubernetes", m, path, "follow=true (unbounded log stream) is forbidden")
    if "/proxy" in path and not any(r.match(path) for r in _KUBE_GET_EXTRA):
        raise ReadOnlyViolation("kubernetes", m, path, "API server proxy is limited to /metrics endpoints")
    if any(r.match(path) for r in _KUBE_GET_EXTRA) or _kube_resource_path_ok(path):
        return
    raise ReadOnlyViolation("kubernetes", m, path, "resource is not on the read-only allow-list")


# --------------------------------------------------------------------------- Prometheus
_PROM_GET_ALLOW = [re.compile(p) for p in (r"^(/.*)?/api/v1/query$", r"^(/.*)?/api/v1/query_range$",
                                            r"^(/.*)?/api/v1/status/buildinfo$")]
_PROM_FORBIDDEN = ("/admin/", "/write", "/-/")


def check_prometheus_request(method: str, path: str, params=None) -> None:
    clean = urlsplit(path).path.rstrip("/")
    if method.upper() != "GET":
        raise ReadOnlyViolation("prometheus", method, clean, "only GET requests are allowed")
    if any(f in clean for f in _PROM_FORBIDDEN):
        raise ReadOnlyViolation("prometheus", method, clean, "admin/write endpoints are forbidden")
    if not any(r.match(clean) for r in _PROM_GET_ALLOW):
        raise ReadOnlyViolation("prometheus", method, clean, "endpoint is not on the read-only allow-list")


# --------------------------------------------------------------------------- tool-level helpers
def refuse_secret_kind(kind: str | None) -> None:
    if (kind or "").lower() in ("secret", "secrets", "sealedsecret"):
        raise ReadOnlyViolation("tool", "GET", f"kind={kind}", "reading Secrets is forbidden")
