"""Tests for the deterministic engine: each test pins one correctness rule from the spec."""

import copy
import json
from pathlib import Path

import pytest

from k8s_capacity.engine.admission import apply_limitranges, search_remediations, simulate_admission
from k8s_capacity.engine.audit import run_audit
from k8s_capacity.engine.capacity import capacity_model
from k8s_capacity.engine.failure import simulate_node_failure
from k8s_capacity.engine.model import effective_pod_resources, resolve_int_or_percent
from k8s_capacity.engine.quantity import to_internal
from k8s_capacity.engine.rollout import reserve_analysis, static_deadlocks
from k8s_capacity.engine.snapshot import load_snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "demo_cluster.json"
API_ANTI = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
    {"labelSelector": {"matchLabels": {"app": "api"}}, "topologyKey": "kubernetes.io/hostname"}]}}


@pytest.fixture()
def snap():
    return json.loads(FIXTURE.read_text())


@pytest.fixture()
def ctx(snap):
    return load_snapshot(snap)


# ------------------------------------------------------------------ quantities / requests
def test_quantities():
    assert to_internal("cpu", "1500m") == 1500
    assert to_internal("cpu", "2") == 2000
    assert to_internal("cpu", "0.25") == 250
    assert to_internal("memory", "1Gi") == 2**30
    assert to_internal("memory", "1G") == 10**9
    assert to_internal("memory", "128974848") == 128974848


def test_effective_request_init_sidecar_overhead():
    spec = {
        "initContainers": [
            {"name": "migrate", "resources": {"requests": {"cpu": "2", "memory": "1Gi"}}},
            {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}},
        ],
        "containers": [{"name": "app", "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}}}],
    }
    req, _, _ = effective_pod_resources(spec, {"cpu": 50, "memory": 2**20})
    # cpu: max(init 2000, app 500 + sidecar 100) + overhead 50
    assert req["cpu"] == 2050
    # memory: max(1Gi, 512Mi + 64Mi) + 1Mi
    assert req["memory"] == 2**30 + 2**20


def test_limit_without_request_defaults_request():
    req, lim, qos = effective_pod_resources({"containers": [{"name": "a", "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}]})
    assert req["cpu"] == 1000 and qos == "Guaranteed"


def test_surge_rounding():
    assert resolve_int_or_percent("25%", 3, True, None) == 1     # maxSurge rounds up
    assert resolve_int_or_percent("25%", 3, False, None) == 0    # maxUnavailable rounds down


# ------------------------------------------------------------------ capacity model
def test_free_counts_daemonsets_and_besteffort_hidden(ctx):
    cap = capacity_model(ctx)
    w1 = next(n for n in cap["nodes"] if n["node"] == "w1")
    # 3800 - (exporter 100 + coredns 100 + api 500 + web 1000) = 2100; BestEffort debug adds 0
    assert w1["free_raw"]["cpu"] == 2100
    assert "dev/debug" in cap["hidden_risks"]["besteffort_pods"]
    assert any(r["pod"] == "shop/worker-66b-0" for r in cap["hidden_risks"]["memory_usage_above_requests"])


def test_fragmentation_metric(ctx):
    cap = capacity_model(ctx, [{"name": "big", "requests": {"cpu": "1500m", "memory": "1Gi"}}])
    general = cap["pools"]["general"]["reference_pods"]["big"]
    # free cpu 2100 + 1300 + 1000 = 4400 -> ideal 2, per node only w1 fits one
    assert general["fits_if_perfectly_packed"] == 2
    assert general["fits_per_node_sum"] == 1
    assert general["fragmentation_pct"] == 50.0


# ------------------------------------------------------------------ placement
def test_sum_is_enough_but_does_not_fit(ctx):
    spec = {"name": "checkout", "namespace": "shop", "replicas": 2,
            "containers": [{"name": "app", "requests": {"cpu": "1500m", "memory": "2Gi"}}]}
    r = simulate_admission(ctx, spec)
    sp = next(c for c in r["checks"] if c["check"] == "steady_state_placement")
    assert sp["status"] == "FAIL"
    assert sp["details"]["cluster_free_sum_would_suggest"]["naive_sum_says_fits"] is True
    assert r["verdict"] == "DOES_NOT_FIT"


def test_taint_blocks_without_toleration_and_pool_remediation(ctx):
    spec = {"name": "batchy", "namespace": "shop", "replicas": 1,
            "containers": [{"name": "app", "requests": {"cpu": "3", "memory": "8Gi"}}],
            "strategy": {"max_surge": 0, "max_unavailable": 1}}
    r = simulate_admission(ctx, spec)
    assert r["verdict"] == "DOES_NOT_FIT"
    # highmem fits steady state, but the only highmem node is then N-1 critical -> condition, not FITS
    rem = search_remediations(ctx, spec, {"require_n_minus_1": False})
    pool = next(o for o in rem["options"] if o["option"] == "pool")
    assert "highmem" in pool["change"] and pool["verdict"] == "FITS_WITH_CONDITIONS"


def test_quota_rejects(ctx):
    spec = {"name": "huge", "namespace": "shop", "replicas": 6,
            "containers": [{"name": "app", "requests": {"cpu": "1", "memory": "1Gi"}}]}
    r = simulate_admission(ctx, spec)
    q = next(c for c in r["checks"] if c["check"] == "namespace_quota")
    assert q["status"] == "FAIL" and "requests.cpu" in q["summary"]


def test_limitrange_defaults_and_max(ctx):
    spec, applied, viol = apply_limitranges(ctx, "shop", {"containers": [
        {"name": "a"}, {"name": "b", "resources": {"limits": {"memory": "16Gi"}}}]})
    a = spec["containers"][0]["resources"]
    assert a["requests"]["cpu"] == "100m" and a["limits"]["memory"] == "512Mi"
    # explicit limit -> request = limit (API defaulting), then max violated
    assert spec["containers"][1]["resources"]["requests"]["memory"] == "16Gi"
    assert any("LimitRange max" in v for v in viol)


def test_topology_spread_do_not_schedule(ctx):
    tsc = [{"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone", "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {"matchLabels": {"app": "z"}}}]
    spec = {"name": "z", "namespace": "shop", "replicas": 4, "labels": {"app": "z"},
            "containers": [{"name": "c", "requests": {"cpu": "100m", "memory": "64Mi"}}],
            "topology_spread_constraints": tsc}
    r = simulate_admission(ctx, spec)
    per_node = next(c for c in r["checks"] if c["check"] == "steady_state_placement")["details"]["placement_per_node"]
    # zones a/b/c; zone-a has w1 and tainted g1 (counted as a domain, nodeTaintsPolicy=Ignore)
    assert sum(per_node.values()) == 4 and max(per_node.values()) == 2


# ------------------------------------------------------------------ rolling update
def test_anti_affinity_deadlock_static_and_simulated(ctx):
    api = ctx.workloads["Deployment/shop/api"]
    assert static_deadlocks(ctx, ctx.state, api)
    res = reserve_analysis(ctx, ctx.state, "sequential", include=["Deployment/shop/api"])
    assert res["groups_infeasible"] and "anti-affinity" in res["groups_infeasible"][0]["stuck"][0]["reason"]


def test_resize_rollout_keeps_old_requests_and_remediation(ctx):
    spec = {"name": "api", "namespace": "shop", "change_type": "resize", "replicas": 3, "labels": {"app": "api"},
            "containers": [{"name": "main", "requests": {"cpu": "700m", "memory": "1Gi"}}], "affinity": API_ANTI,
            "strategy": {"max_surge": 1, "max_unavailable": 0}}
    r = simulate_admission(ctx, spec)
    checks = {c["check"]: c["status"] for c in r["checks"]}
    assert checks["steady_state_placement"] == "PASS" and checks["app_rollout"] == "FAIL"
    rem = search_remediations(ctx, spec)
    assert rem["options"][0]["option"] == "strategy" and rem["options"][0]["verdict"] == "FITS"


def test_resize_needs_room_for_new_requests_while_old_exist(snap):
    # make shop/web maxUnavailable 0: new 2-core pods must fit while both old 1-core pods still run
    for w in snap["workloads"]:
        if w["metadata"]["name"] == "web":
            w["spec"]["strategy"]["rollingUpdate"] = {"maxSurge": 1, "maxUnavailable": 0}
    ctx = load_snapshot(snap)
    spec = {"name": "web", "namespace": "shop", "change_type": "resize", "replicas": 2, "labels": {"app": "web"},
            "containers": [{"name": "main", "requests": {"cpu": "1800m", "memory": "2Gi"}}]}
    r = simulate_admission(ctx, spec)
    roll = next(c for c in r["checks"] if c["check"] == "app_rollout")
    assert roll["status"] in ("FAIL", "WARN")


def test_parallel_reserve_is_sum_sequential_is_max(ctx):
    seq = reserve_analysis(ctx, ctx.state, "sequential")["required_reserve_raw"]
    par = reserve_analysis(ctx, ctx.state, "all")["required_reserve_raw"]
    assert par["cpu"] > seq["cpu"]


def test_quota_charged_for_surge(snap):
    snap["quotas"][0]["hard"]["requests.cpu"] = "4900m"  # used 4700m; web surge needs 1000m
    ctx = load_snapshot(snap)
    res = reserve_analysis(ctx, ctx.state, "sequential", include=["Deployment/shop/web"])
    w = res["per_workload"][0]
    assert w["quota_blocked"]
    # replicas 2: maxSurge 25% -> 1 (round up), maxUnavailable 25% -> 0 (round down):
    # the surge pod is rejected by the quota and nothing may be deleted -> stuck
    assert w["status"] == "STUCK" and "quota" in w["stuck_reason"]


# ------------------------------------------------------------------ N-1
def test_node_failure_rules(ctx):
    w3 = simulate_node_failure(ctx, ctx.state, "w3")
    assert w3["daemonset_pods_not_rescheduled"] == 1
    assert w3["unmovable_local_pv_pods"] == ["data/postgres-0"]
    assert any(p["pod"].startswith("shop/api") for p in w3["pending_after_failure"])
    w1 = simulate_node_failure(ctx, ctx.state, "w1")
    assert w1["lost_pods"] == ["dev/debug"]
    assert w1["pdb_blocking_voluntary_drain"]


def test_preemption_reported_separately(snap):
    # give batch/report a high-priority neighbour that only fits by evicting it
    snap["priority_classes"].append({"name": "high", "value": 1000})
    snap["pods"].append({"metadata": {"name": "pay-0", "namespace": "shop", "labels": {"app": "pay"},
                                      "ownerReferences": [{"kind": "ReplicaSet", "name": "pay-1", "controller": True}]},
                         "spec": {"nodeName": "w1", "priorityClassName": "high",
                                  "containers": [{"name": "c", "resources": {"requests": {"cpu": "1800m", "memory": "1Gi"}}}]},
                         "status": {"phase": "Running"}})
    ctx = load_snapshot(snap)
    res = simulate_node_failure(ctx, ctx.state, "w1")
    pre = res["rescheduled_with_preemption"]
    assert pre and pre[0]["pod"] == "shop/pay-0"
    assert any("batch/report" in v for v in pre[0]["victims"])


def test_autoscaler_recovery(ctx):
    asc = {"enabled": True, "node_groups": [{"name": "hm", "pool": "highmem", "min": 1, "max": 2, "current": 1,
                                             "provisioning_minutes": 6}]}
    res = simulate_node_failure(ctx, ctx.state, "g1", autoscaler=asc)
    assert res["autoscaler"]["nodes_needed"] == 1 and res["autoscaler"]["provisioning_minutes"] == 6


# ------------------------------------------------------------------ audit + verdict wiring
def test_audit_risks_sorted_and_traceable(ctx):
    a = run_audit(ctx, {"update_concurrency": "sequential"})
    sev = [r["severity"] for r in a["risks"]]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    assert sev == sorted(sev, key=order.get)
    assert all(r["evidence"] for r in a["risks"])
    assert a["summary"]["n_minus_1"]["overall"] == "DEGRADED"


def test_small_app_fits(ctx):
    spec = {"name": "cart", "namespace": "shop", "replicas": 2,
            "containers": [{"name": "app", "requests": {"cpu": "200m", "memory": "256Mi"}}]}
    assert simulate_admission(ctx, spec)["verdict"] == "FITS"


def test_snapshot_not_mutated_by_simulations(ctx):
    before = copy.deepcopy(ctx.state.used)
    simulate_admission(ctx, {"name": "x", "namespace": "shop", "replicas": 3,
                             "containers": [{"name": "a", "requests": {"cpu": "1", "memory": "1Gi"}}]})
    simulate_node_failure(ctx, ctx.state, "w2")
    assert ctx.state.used == before


def test_missing_target_for_scale_is_reported(ctx):
    r = simulate_admission(ctx, {"name": "nope", "namespace": "shop", "change_type": "scale", "replicas": 2})
    assert r["verdict"] == "DOES_NOT_FIT" and "Deployment/shop/api" in r["conditions"][0]
    assert search_remediations(ctx, {"name": "nope", "namespace": "shop", "change_type": "scale", "replicas": 2})["options"] == []


def test_besteffort_fit_is_only_a_warning(ctx):
    r = simulate_admission(ctx, {"name": "be", "namespace": "dev", "replicas": 1, "containers": [{"name": "a"}]})
    sp = next(c for c in r["checks"] if c["check"] == "steady_state_placement")
    assert sp["status"] == "WARN" and r["verdict"] == "FITS_WITH_CONDITIONS"
