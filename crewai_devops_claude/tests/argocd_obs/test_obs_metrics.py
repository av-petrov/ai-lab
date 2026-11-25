"""Prometheus text format parsing and Argo CD metric summaries."""

from __future__ import annotations

import math

from argocd_observability.metrics import MetricsService, parse_prometheus_text, summarize_argocd_metrics

from .conftest import settings

CONTROLLER = r'''
# HELP argocd_app_info Information about application.
# TYPE argocd_app_info gauge
argocd_app_info{autosync_enabled="true",dest_namespace="demo-cl",dest_server="https://kubernetes.default.svc",health_status="Degraded",name="demo-cl",namespace="argocd",operation="",project="default",repo="https://github.com/argoproj/argocd-example-apps",sync_status="Synced"} 1
argocd_app_info{autosync_enabled="false",dest_namespace="hello",health_status="Missing",name="hello-test",namespace="argocd",project="apps",sync_status="OutOfSync"} 1
# TYPE argocd_app_sync_total counter
argocd_app_sync_total{dest_server="in-cluster",name="demo-cl",namespace="argocd",phase="Succeeded",project="default"} 3
argocd_app_sync_total{dest_server="in-cluster",name="demo-cl",namespace="argocd",phase="Failed",project="default"} 2
argocd_app_sync_total{dest_server="in-cluster",name="other",namespace="argocd",phase="Failed",project="default"} 7
# TYPE argocd_app_reconcile histogram
argocd_app_reconcile_bucket{dest_server="in-cluster",namespace="argocd",le="0.25"} 10
argocd_app_reconcile_bucket{dest_server="in-cluster",namespace="argocd",le="+Inf"} 12
argocd_app_reconcile_sum{dest_server="in-cluster",namespace="argocd"} 6
argocd_app_reconcile_count{dest_server="in-cluster",namespace="argocd"} 12
workqueue_depth{name="app_reconciliation_queue"} 0
weird_metric{msg="quote \" and backslash \\ and brace } inside",path="/a\nb"} NaN 1732420000000
inf_metric +Inf
neg_inf -Inf
process_resident_memory_bytes 1.2345e+08
this is not a metric line
broken{unterminated="x 1
'''


def test_parse_text_format():
    samples = parse_prometheus_text(CONTROLLER)
    names = [s.name for s in samples]
    assert names.count("argocd_app_info") == 2
    assert "broken" not in names
    weird = next(s for s in samples if s.name == "weird_metric")
    assert weird.labels["msg"] == 'quote " and backslash \\ and brace } inside'
    assert weird.labels["path"] == "/a\nb"
    assert math.isnan(weird.value) and weird.timestamp_ms == 1732420000000
    assert next(s for s in samples if s.name == "inf_metric").value == math.inf
    assert next(s for s in samples if s.name == "neg_inf").value == -math.inf
    assert next(s for s in samples if s.name == "process_resident_memory_bytes").value == 1.2345e8
    bucket = next(s for s in samples if s.name == "argocd_app_reconcile_bucket" and s.labels["le"] == "+Inf")
    assert bucket.value == 12


def test_summarize_controller_for_app():
    s = summarize_argocd_metrics(parse_prometheus_text(CONTROLLER), "application-controller", app="demo-cl")
    assert s["argocd_app_info"] == [{"name": "demo-cl", "project": "default", "health_status": "Degraded",
                                     "sync_status": "Synced", "autosync_enabled": "true", "operation": "",
                                     "dest_namespace": "demo-cl",
                                     "repo": "https://github.com/argoproj/argocd-example-apps"}]
    assert s["argocd_app_sync_total_by_phase"] == {"Failed": 2.0, "Succeeded": 3.0}
    assert s["argocd_app_reconcile"] == {"count": 12.0, "avg_seconds": 0.5}
    assert s["process_resident_memory_bytes"] == 123450000.0


def test_metrics_unavailable_is_reported_not_raised():
    m = MetricsService(settings(), kube=None)
    det = m.detect()
    assert not det["prometheus"].available and "PROMETHEUS_URL not set" in det["prometheus"].detail
    assert not det["argocd_metrics_scrape"].available
    assert not det["metrics_server"].available
    assert m.argocd_component("application-controller")["available"] is False
    assert m.pod_usage("demo")["available"] is False
    assert m.promql("up")["available"] is False


def test_prometheus_preferred_and_scrape_skipped():
    from argocd_observability.context import SourceLedger
    from argocd_observability.settings import ObservabilitySettings
    from argocd_observability.guard import check_prometheus_request
    from argocd_observability.http import GuardedHttpClient

    from .conftest import FakeSession

    def query(params):
        return {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"name": params["query"][:10]}, "value": [1732420000, "1"]}]}}

    sess = FakeSession({"/api/v1/status/buildinfo": {"data": {"version": "3.16.0"}}, "/api/v1/query": query})
    prom = GuardedHttpClient("http://prom.test", check_prometheus_request, session=sess)
    s = ObservabilitySettings(_env_file=None, prometheus_url="http://prom.test")
    m = MetricsService(s, kube=None, prom=prom)
    det = m.detect()
    assert det["prometheus"].available and det["argocd_metrics_scrape"].detail.startswith("skipped")
    out = m.argocd_component("application-controller", "demo-cl")
    assert out["source"] == "prometheus" and out["results"]["argocd_app_info"]
    assert {c[1] for c in sess.calls} == {"/api/v1/status/buildinfo", "/api/v1/query"}
    led = SourceLedger()
    led.missing("argocd_metrics_scrape", det["argocd_metrics_scrape"].detail)
    assert led.unavailable == {}
