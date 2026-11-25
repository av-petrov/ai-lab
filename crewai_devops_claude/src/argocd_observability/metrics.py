"""Metrics: Prometheus (PromQL) -> Argo CD /metrics scrape via the API server proxy -> metrics-server.

Nothing here assumes Prometheus exists. ``MetricsService.detect()`` probes each source once (short
timeouts) and caches the outcome; unavailable sources are reported, never fatal.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .guard import check_prometheus_request
from .http import GuardedHttpClient


# --------------------------------------------------------------------------- text format parser
@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float
    timestamp_ms: int | None = None


_LABEL_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')
_NAME_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)")


def _unescape(v: str) -> str:
    return v.replace(r"\\", "\x00").replace(r"\"", '"').replace(r"\n", "\n").replace("\x00", "\\")


def _parse_value(tok: str) -> float:
    t = tok.strip()
    low = t.lower()
    if low in ("nan",):
        return math.nan
    if low in ("+inf", "inf"):
        return math.inf
    if low == "-inf":
        return -math.inf
    return float(t)


def parse_prometheus_text(text: str) -> list[Sample]:
    """Parse the Prometheus text exposition format (0.0.4). Malformed lines are skipped."""
    out: list[Sample] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAME_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        rest = line[m.end():]
        labels: dict[str, str] = {}
        if rest.startswith("{"):
            end = _find_label_end(rest)
            if end < 0:
                continue
            body = rest[1:end]
            pos = 0
            while pos < len(body):
                lm = _LABEL_RE.match(body, pos)
                if not lm:
                    break
                labels[lm.group(1)] = _unescape(lm.group(2))
                pos = lm.end()
            rest = rest[end + 1:]
        parts = rest.split()
        if not parts:
            continue
        try:
            value = _parse_value(parts[0])
            ts = int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            continue
        out.append(Sample(name, labels, value, ts))
    return out


def _find_label_end(s: str) -> int:
    in_str = esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif ch == "}" and not in_str:
            return i
    return -1


def _fmt(v: float) -> float | str:
    if math.isnan(v) or math.isinf(v):
        return str(v)
    return round(v, 4)


def summarize_argocd_metrics(samples: list[Sample], component: str, app: str | None = None) -> dict[str, Any]:
    """Pick the Argo CD series that matter for diagnosis and aggregate them compactly."""
    by_name: dict[str, list[Sample]] = {}
    for s in samples:
        by_name.setdefault(s.name, []).append(s)

    def app_filter(ss: list[Sample]) -> list[Sample]:
        return [s for s in ss if app is None or s.labels.get("name") == app]

    def sum_by(name: str, key: str, ss: list[Sample] | None = None) -> dict[str, float | str]:
        acc: dict[str, float] = {}
        for s in (ss if ss is not None else by_name.get(name, [])):
            k = s.labels.get(key, "")
            acc[k] = acc.get(k, 0.0) + s.value
        return {k: _fmt(v) for k, v in sorted(acc.items())}

    def hist_avg(base: str, ss_filter=None) -> dict[str, Any] | None:
        cnt = [s for s in by_name.get(base + "_count", []) if ss_filter is None or ss_filter(s)]
        sm = [s for s in by_name.get(base + "_sum", []) if ss_filter is None or ss_filter(s)]
        c, t = sum(s.value for s in cnt), sum(s.value for s in sm)
        if not cnt:
            return None
        return {"count": _fmt(c), "avg_seconds": _fmt(t / c) if c else None}

    out: dict[str, Any] = {"component": component, "series_total": len(samples)}
    if component == "application-controller":
        info = app_filter(by_name.get("argocd_app_info", []))
        out["argocd_app_info"] = [
            {k: s.labels.get(k) for k in ("name", "project", "health_status", "sync_status", "autosync_enabled",
                                          "operation", "dest_namespace", "repo") if s.labels.get(k) is not None}
            for s in info][:50]
        sync = app_filter(by_name.get("argocd_app_sync_total", []))
        out["argocd_app_sync_total_by_phase"] = sum_by("", "phase", sync)
        k8s_req = app_filter(by_name.get("argocd_app_k8s_request_total", []))
        if k8s_req:
            out["argocd_app_k8s_request_total_by_response_code"] = sum_by("", "response_code", k8s_req)
        rec = hist_avg("argocd_app_reconcile")
        if rec:
            out["argocd_app_reconcile"] = rec
        for name in ("argocd_cluster_connection_status", "argocd_kubectl_exec_pending", "workqueue_depth",
                     "argocd_app_orphaned_resources_count"):
            if name in by_name:
                ss = by_name[name] if name != "argocd_app_orphaned_resources_count" else app_filter(by_name[name])
                out[name] = [{**{k: v for k, v in s.labels.items() if k in ("name", "server", "project")},
                              "value": _fmt(s.value)} for s in ss][:20]
        cond = app_filter(by_name.get("argocd_app_condition", []))
        if cond:
            out["argocd_app_condition"] = [{"name": s.labels.get("name"), "condition": s.labels.get("condition"),
                                            "value": _fmt(s.value)} for s in cond if s.value][:20]
    elif component == "repo-server":
        out["argocd_git_request_total_by_type"] = sum_by("argocd_git_request_total", "request_type")
        g = hist_avg("argocd_git_request_duration_seconds")
        if g:
            out["argocd_git_request_duration_seconds"] = g
        for name in ("argocd_repo_pending_request_total", "argocd_redis_request_total"):
            if name in by_name:
                out[name] = _fmt(sum(s.value for s in by_name[name]))
        fetch_fail = [s for s in by_name.get("argocd_git_fetch_fail_total", [])]
        if fetch_fail:
            out["argocd_git_fetch_fail_total"] = _fmt(sum(s.value for s in fetch_fail))
    elif component == "server":
        grpc = by_name.get("grpc_server_handled_total", [])
        out["grpc_server_handled_total_by_code"] = sum_by("", "grpc_code", grpc)
        for name in ("argocd_redis_request_total",):
            if name in by_name:
                out[name] = _fmt(sum(s.value for s in by_name[name]))
    # process health, common to all Argo CD components
    for name in ("process_resident_memory_bytes", "go_goroutines", "process_cpu_seconds_total"):
        if name in by_name:
            out[name] = _fmt(by_name[name][0].value)
    return out


# --------------------------------------------------------------------------- sources
ARGOCD_METRIC_TARGETS = {
    # component             (service, port, pod label app.kubernetes.io/name)
    "application-controller": ("argocd-metrics", 8082, "argocd-application-controller"),
    "server": ("argocd-server-metrics", 8083, "argocd-server"),
    "repo-server": ("argocd-repo-server", 8084, "argocd-repo-server"),
}


@dataclass
class SourceStatus:
    available: bool
    detail: str


@dataclass
class MetricsService:
    settings: Any
    kube: Any | None = None          # KubeReader or None
    prom: GuardedHttpClient | None = None
    _detected: dict[str, SourceStatus] | None = field(default=None, init=False)
    _scrape_cache: dict[str, list[Sample]] = field(default_factory=dict, init=False)
    _scrape_paths: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.settings.prometheus_url and self.prom is None:
            self.prom = GuardedHttpClient(self.settings.prometheus_url, check_prometheus_request,
                                          verify=not self.settings.prometheus_insecure,
                                          timeout=self.settings.metrics_timeout,
                                          max_bytes=self.settings.max_response_bytes)

    # ------------------------------------------------------------------ detection
    def detect(self) -> dict[str, SourceStatus]:
        if self._detected is not None:
            return self._detected
        det: dict[str, SourceStatus] = {}
        if self.prom is None:
            det["prometheus"] = SourceStatus(False, "PROMETHEUS_URL not set")
        else:
            try:
                info = self.prom.get_json("/api/v1/status/buildinfo")
                det["prometheus"] = SourceStatus(True, f"Prometheus {info.get('data', {}).get('version', '?')}")
            except Exception as exc:  # noqa: BLE001
                det["prometheus"] = SourceStatus(False, f"PROMETHEUS_URL unreachable: {_short(exc)}")
        if det["prometheus"].available:
            det["argocd_metrics_scrape"] = SourceStatus(False, "skipped: Prometheus already provides Argo CD metrics")
        elif not self.settings.scrape_argocd_metrics:
            det["argocd_metrics_scrape"] = SourceStatus(False, "disabled (ARGOCD_OBS_SCRAPE_METRICS=false)")
        elif self.kube is None:
            det["argocd_metrics_scrape"] = SourceStatus(False, "no Kubernetes API access for the service proxy")
        else:
            ok, bad, timed_out = [], [], False
            for comp in ARGOCD_METRIC_TARGETS:
                if timed_out and not ok:
                    # all components share the API server -> pod path; one timeout is enough evidence
                    bad.append(f"{comp}: skipped after timeout")
                    continue
                try:
                    self._scrape(comp)
                    ok.append(comp)
                except Exception as exc:  # noqa: BLE001
                    reason = _short(exc)
                    timed_out = timed_out or reason == "timed out"
                    bad.append(f"{comp}: {reason}")
            detail = "; ".join(([f"ok: {', '.join(ok)}"] if ok else []) + ([f"failed: {'; '.join(bad)}"] if bad else []))
            if timed_out:
                detail += (" (API server -> pod metrics port timed out: a NetworkPolicy probably blocks it; "
                           "see deploy/cilium/allow-apiserver-metrics.yaml)")
            det["argocd_metrics_scrape"] = SourceStatus(bool(ok), detail)
        if self.kube is None:
            det["metrics_server"] = SourceStatus(False, "no Kubernetes API access")
        else:
            try:
                svc = self.kube.get_json("/apis/apiregistration.k8s.io/v1/apiservices/v1beta1.metrics.k8s.io")
                conds = {c.get("type"): c.get("status") for c in svc.get("status", {}).get("conditions", [])}
                det["metrics_server"] = SourceStatus(conds.get("Available") == "True",
                                                     f"APIService v1beta1.metrics.k8s.io Available={conds.get('Available')}")
            except Exception as exc:  # noqa: BLE001
                det["metrics_server"] = SourceStatus(False, f"metrics-server not installed ({_short(exc)})")
        self._detected = det
        return det

    # ------------------------------------------------------------------ Argo CD scrape
    def _scrape(self, component: str) -> list[Sample]:
        if component in self._scrape_cache:
            return self._scrape_cache[component]
        svc, port, label = ARGOCD_METRIC_TARGETS[component]
        ns = self.settings.argocd_namespace
        timeout = self.settings.metrics_timeout
        candidates = []
        if component in self._scrape_paths:
            candidates.append(self._scrape_paths[component])
        else:
            try:
                s = self.kube.get_json(f"/api/v1/namespaces/{ns}/services/{svc}")
                if any(p.get("port") == port for p in s.get("spec", {}).get("ports", [])):
                    candidates.append(f"/api/v1/namespaces/{ns}/services/{svc}:{port}/proxy/metrics")
            except Exception:  # noqa: BLE001  service missing -> try the pods directly
                pass
            pods = self.kube.get_json(f"/api/v1/namespaces/{ns}/pods",
                                      {"labelSelector": f"app.kubernetes.io/name={label}"})
            for p in pods.get("items", [])[:1]:
                if p.get("status", {}).get("phase") == "Running":
                    candidates.append(f"/api/v1/namespaces/{ns}/pods/{p['metadata']['name']}:{port}/proxy/metrics")
        if not candidates:
            raise RuntimeError(f"no {svc}:{port} service and no running {label} pod")
        last: Exception | None = None
        for path in candidates:
            try:
                text = self.kube.get_text(path, timeout=timeout)
                samples = parse_prometheus_text(text)
                self._scrape_cache[component] = samples
                self._scrape_paths[component] = path
                return samples
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError(f"scrape failed via API proxy ({_short(last)}); a NetworkPolicy may block "
                           f"API server -> pod:{port} traffic")

    def argocd_component(self, component: str, app: str | None = None) -> dict[str, Any]:
        st = self.detect()
        if st["prometheus"].available:
            return self._prom_component(component, app)
        if not st["argocd_metrics_scrape"].available:
            return {"available": False, "reason": st["argocd_metrics_scrape"].detail}
        try:
            summary = summarize_argocd_metrics(self._scrape(component), component, app)
            return {"available": True, "source": f"scrape {self._scrape_paths.get(component)}", **summary}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": _short(exc)}

    # ------------------------------------------------------------------ Prometheus
    def promql(self, query: str, range_minutes: int | None = None, step_seconds: int = 60) -> dict[str, Any]:
        if self.prom is None or not self.detect()["prometheus"].available:
            return {"available": False, "reason": self.detect()["prometheus"].detail}
        if len(query) > 800:
            return {"available": True, "error": "query too long (max 800 chars)"}
        if range_minutes:
            import time

            end = time.time()
            range_minutes = min(range_minutes, 180)
            step = max(step_seconds, 30)
            data = self.prom.get_json("/api/v1/query_range", {"query": query, "start": end - range_minutes * 60,
                                                              "end": end, "step": step})
        else:
            data = self.prom.get_json("/api/v1/query", {"query": query})
        res = data.get("data", {}).get("result", [])
        out = []
        for r in res[:30]:
            item: dict[str, Any] = {"metric": r.get("metric", {})}
            if "value" in r:
                item["value"] = r["value"][1]
            if "values" in r:
                vals = r["values"]
                item["points"] = len(vals)
                item["first"], item["last"] = vals[0][1], vals[-1][1]
                nums = [float(v[1]) for v in vals if v[1] not in ("NaN",)]
                if nums:
                    item["min"], item["max"] = round(min(nums), 4), round(max(nums), 4)
            out.append(item)
        return {"available": True, "source": "prometheus", "query": query, "series": len(res), "result": out}

    def _prom_component(self, component: str, app: str | None) -> dict[str, Any]:
        sel = f'{{name="{app}"}}' if app else ""
        queries = {
            "application-controller": {
                "argocd_app_info": f"argocd_app_info{sel}",
                "sync_total_by_phase_1h": f"sum by (phase) (increase(argocd_app_sync_total{sel}[1h]))",
                "reconcile_p95_seconds_15m":
                    "histogram_quantile(0.95, sum by (le) (rate(argocd_app_reconcile_bucket[15m])))",
                "workqueue_depth": 'sum by (name) (workqueue_depth{job=~".*argocd.*"})',
            },
            "repo-server": {
                "git_requests_by_type_1h": "sum by (request_type) (increase(argocd_git_request_total[1h]))",
                "git_request_p95_seconds_15m":
                    "histogram_quantile(0.95, sum by (le) (rate(argocd_git_request_duration_seconds_bucket[15m])))",
                "pending_requests": "sum(argocd_repo_pending_request_total)",
            },
            "server": {
                "grpc_errors_by_code_1h": 'sum by (grpc_code) (increase(grpc_server_handled_total{grpc_code!="OK"}[1h]))',
            },
        }[component]
        return {"available": True, "source": "prometheus", "component": component,
                "results": {k: self.promql(q) .get("result") for k, q in queries.items()}}

    # ------------------------------------------------------------------ pod usage
    def pod_usage(self, namespace: str, pod_prefix: str | None = None) -> dict[str, Any]:
        st = self.detect()
        if st["prometheus"].available:
            pod_re = f',pod=~"{re.escape(pod_prefix)}.*"' if pod_prefix else ""
            return {
                "source": "prometheus",
                "cpu_cores_5m": self.promql(
                    f'sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="{namespace}",container!=""{pod_re}}}[5m]))'
                ).get("result"),
                "memory_working_set_bytes": self.promql(
                    f'sum by (pod) (container_memory_working_set_bytes{{namespace="{namespace}",container!=""{pod_re}}})'
                ).get("result"),
            }
        if st["metrics_server"].available and self.kube is not None:
            data = self.kube.get_json(f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
            items = [i for i in data.get("items", []) if not pod_prefix or i["metadata"]["name"].startswith(pod_prefix)]
            return {"source": "metrics-server", "pods": [
                {"pod": i["metadata"]["name"], "window": i.get("window"),
                 "containers": {c["name"]: c.get("usage", {}) for c in i.get("containers", [])}} for i in items[:30]]}
        return {"available": False,
                "reason": f"no pod usage source: prometheus ({st['prometheus'].detail}); "
                          f"metrics-server ({st['metrics_server'].detail})"}


def _short(exc: Exception | None, n: int = 160) -> str:
    if exc is None:
        return "unknown error"
    text = str(exc).replace("\n", " ")
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return "timed out"
    if "max retries exceeded" in low:
        m = re.search(r"Caused by (\w+)\((.*)", text)
        return f"unreachable ({m.group(1)})" if m else "unreachable"
    status = getattr(exc, "status", None)
    if status:
        return f"HTTP {status} {getattr(exc, 'reason', '')}".strip()
    return f"{exc.__class__.__name__}: {text}"[:n]
