"""Generate tests/fixtures/demo_cluster.json: a small invented cluster (4 nodes).

Pool "general": w1, w2, w3 (3800m / 14.5Gi allocatable, zones a/b/c)
Pool "highmem": g1 (7800m / 60Gi, tainted dedicated=highmem:NoSchedule)

Built-in traps:
  * shop/api: 3 replicas, required anti-affinity per hostname, maxSurge=1, maxUnavailable=0
    on exactly 3 eligible nodes -> rolling-update deadlock and N-1 loss.
  * data/postgres: StatefulSet on a local PV on w3 -> cannot move on failure.
  * batch/report: low PriorityClass -> preemption victim.
  * dev/debug: bare BestEffort pod -> invisible to the scheduler, lost on node failure.
  * PDB shop/api minAvailable=3 -> blocks drain.
  * free CPU summed over general = 4.4 cores, but only w1 has >= 1.5 cores free.

Run:  python tests/fixtures/make_demo_cluster.py
"""

import json
from pathlib import Path

GI = "Gi"


def node(name, pool, zone, cpu, mem, cap_cpu, cap_mem, cidr, taints=None, itype="m5.xlarge"):
    return {
        "name": name,
        "labels": {"kubernetes.io/hostname": name, "node.kubernetes.io/pool": pool,
                   "topology.kubernetes.io/zone": zone, "node.kubernetes.io/instance-type": itype},
        "taints": taints or [],
        "unschedulable": False,
        "ready": True,
        "capacity": {"cpu": cap_cpu, "memory": cap_mem, "pods": "110", "ephemeral-storage": "100Gi"},
        "allocatable": {"cpu": cpu, "memory": mem, "pods": "110", "ephemeral-storage": "90Gi"},
        "pod_cidrs": [cidr],
        "csi_limits": {"ebs.csi.aws.com": 25},
        "instance_type": itype,
    }


def pod(ns, name, node_name, requests, *, labels=None, owner=None, limits=None, affinity=None,
        tolerations=None, priority_class=None, volumes=None, ports=None):
    c = {"name": "main", "resources": {}}
    if requests:
        c["resources"]["requests"] = requests
    if limits:
        c["resources"]["limits"] = limits
    if ports:
        c["ports"] = ports
    spec = {"nodeName": node_name, "containers": [c]}
    if affinity:
        spec["affinity"] = affinity
    if tolerations:
        spec["tolerations"] = tolerations
    if priority_class:
        spec["priorityClassName"] = priority_class
    if volumes:
        spec["volumes"] = volumes
    md = {"name": name, "namespace": ns, "labels": labels or {}}
    if owner:
        md["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"metadata": md, "spec": spec, "status": {"phase": "Running"}}


API_ANTI = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
    {"labelSelector": {"matchLabels": {"app": "api"}}, "topologyKey": "kubernetes.io/hostname"}]}}
TOL_ALL = [{"operator": "Exists"}]
HM_TOL = [{"key": "dedicated", "operator": "Equal", "value": "highmem", "effect": "NoSchedule"}]


def deployment(ns, name, replicas, requests, labels, surge, unav, affinity=None, tolerations=None, pc=None):
    tspec = {"containers": [{"name": "main", "resources": {"requests": requests}}]}
    if affinity:
        tspec["affinity"] = affinity
    if tolerations:
        tspec["tolerations"] = tolerations
    if pc:
        tspec["priorityClassName"] = pc
    return {"kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"replicas": replicas, "selector": {"matchLabels": labels},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": surge, "maxUnavailable": unav}},
                     "template": {"metadata": {"labels": labels}, "spec": tspec}}}


def build():
    nodes = [
        node("w1", "general", "zone-a", "3800m", "14.5Gi", "4", "16Gi", "10.244.1.0/24"),
        node("w2", "general", "zone-b", "3800m", "14.5Gi", "4", "16Gi", "10.244.2.0/24"),
        node("w3", "general", "zone-c", "3800m", "14.5Gi", "4", "16Gi", "10.244.3.0/24"),
        node("g1", "highmem", "zone-a", "7800m", "60Gi", "8", "64Gi", "10.244.4.0/24",
             taints=[{"key": "dedicated", "value": "highmem", "effect": "NoSchedule"}], itype="r5.2xlarge"),
    ]
    pods = []
    for n in ("w1", "w2", "w3", "g1"):
        pods.append(pod("monitoring", f"node-exporter-{n}", n, {"cpu": "100m", "memory": "128Mi"},
                        labels={"app": "node-exporter"}, owner=("DaemonSet", "node-exporter"),
                        tolerations=TOL_ALL, priority_class="system-node-critical",
                        ports=[{"containerPort": 9100, "hostPort": 9100, "protocol": "TCP"}]))
    for i, n in enumerate(("w1", "w2")):
        pods.append(pod("kube-system", f"coredns-5d78c-{i}", n, {"cpu": "100m", "memory": "70Mi"},
                        labels={"k8s-app": "kube-dns"}, owner=("ReplicaSet", "coredns-5d78c"),
                        limits={"memory": "170Mi"}, priority_class="system-cluster-critical"))
    for i, n in enumerate(("w1", "w2", "w3")):
        pods.append(pod("shop", f"api-7f9c-{i}", n, {"cpu": "500m", "memory": "1Gi"},
                        labels={"app": "api"}, owner=("ReplicaSet", "api-7f9c"), affinity=API_ANTI,
                        limits={"memory": "1Gi"}))
    for i, n in enumerate(("w1", "w2")):
        pods.append(pod("shop", f"web-55d-{i}", n, {"cpu": "1", "memory": "2Gi"},
                        labels={"app": "web"}, owner=("ReplicaSet", "web-55d"), limits={"memory": "4Gi"}))
    for i in range(2):
        pods.append(pod("shop", f"worker-66b-{i}", "w3", {"cpu": "600m", "memory": "1Gi"},
                        labels={"app": "worker"}, owner=("ReplicaSet", "worker-66b"), limits={"memory": "2Gi"}))
    pods.append(pod("data", "postgres-0", "w3", {"cpu": "1", "memory": "4Gi"}, labels={"app": "postgres"},
                    owner=("StatefulSet", "postgres"), limits={"memory": "4Gi"},
                    volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "data-postgres-0"}}]))
    pods.append(pod("batch", "report-9d1-0", "w2", {"cpu": "800m", "memory": "2Gi"}, labels={"app": "report"},
                    owner=("ReplicaSet", "report-9d1"), priority_class="low"))
    pods.append(pod("dev", "debug", "w1", None, labels={"run": "debug"}))
    pods.append(pod("analytics", "spark-2c4-0", "g1", {"cpu": "4", "memory": "32Gi"}, labels={"app": "spark"},
                    owner=("ReplicaSet", "spark-2c4"), tolerations=HM_TOL, limits={"memory": "32Gi"}))

    workloads = [
        {"kind": "DaemonSet", "metadata": {"name": "node-exporter", "namespace": "monitoring"},
         "spec": {"selector": {"matchLabels": {"app": "node-exporter"}},
                  "updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}},
                  "template": {"metadata": {"labels": {"app": "node-exporter"}}, "spec": {
                      "containers": [{"name": "main", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
                                      "ports": [{"containerPort": 9100, "hostPort": 9100}]}],
                      "tolerations": TOL_ALL, "priorityClassName": "system-node-critical"}}}},
        deployment("kube-system", "coredns", 2, {"cpu": "100m", "memory": "70Mi"}, {"k8s-app": "kube-dns"}, "25%", 1,
                   pc="system-cluster-critical"),
        deployment("shop", "api", 3, {"cpu": "500m", "memory": "1Gi"}, {"app": "api"}, 1, 0, affinity=API_ANTI),
        deployment("shop", "web", 2, {"cpu": "1", "memory": "2Gi"}, {"app": "web"}, "25%", "25%"),
        deployment("shop", "worker", 2, {"cpu": "600m", "memory": "1Gi"}, {"app": "worker"}, "25%", "25%"),
        deployment("batch", "report", 1, {"cpu": "800m", "memory": "2Gi"}, {"app": "report"}, 1, 0, pc="low"),
        deployment("analytics", "spark", 1, {"cpu": "4", "memory": "32Gi"}, {"app": "spark"}, 1, 0, tolerations=HM_TOL),
        {"kind": "StatefulSet", "metadata": {"name": "postgres", "namespace": "data"},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "postgres"}},
                  "updateStrategy": {"type": "RollingUpdate"},
                  "template": {"metadata": {"labels": {"app": "postgres"}}, "spec": {
                      "containers": [{"name": "main", "resources": {"requests": {"cpu": "1", "memory": "4Gi"},
                                                                    "limits": {"memory": "4Gi"}}}]}},
                  "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {
                      "storageClassName": "local-ssd", "accessModes": ["ReadWriteOnce"],
                      "resources": {"requests": {"storage": "100Gi"}}}}]}},
    ]
    replicasets = [
        {"namespace": "kube-system", "name": "coredns-5d78c", "owner_kind": "Deployment", "owner_name": "coredns"},
        {"namespace": "shop", "name": "api-7f9c", "owner_kind": "Deployment", "owner_name": "api"},
        {"namespace": "shop", "name": "web-55d", "owner_kind": "Deployment", "owner_name": "web"},
        {"namespace": "shop", "name": "worker-66b", "owner_kind": "Deployment", "owner_name": "worker"},
        {"namespace": "batch", "name": "report-9d1", "owner_kind": "Deployment", "owner_name": "report"},
        {"namespace": "analytics", "name": "spark-2c4", "owner_kind": "Deployment", "owner_name": "spark"},
    ]
    return {
        "schema": "k8s-capacity-snapshot/v1",
        "meta": {"cluster": "demo", "collected_at": "2026-11-19T08:00:00Z", "kubernetes_version": "v1.31.4",
                 "sources": ["fixture"], "pool_label": "node.kubernetes.io/pool",
                 "ipam": {"mode": "podcidr", "reserved_per_cidr": 3}},
        "namespaces": [{"name": n, "labels": {"kubernetes.io/metadata.name": n}}
                       for n in ("kube-system", "monitoring", "shop", "data", "batch", "dev", "analytics")],
        "nodes": nodes,
        "pods": pods,
        "workloads": workloads,
        "replicasets": replicasets,
        "pdbs": [{"namespace": "shop", "name": "api", "selector": {"matchLabels": {"app": "api"}},
                  "min_available": 3, "expected_pods": 3}],
        "quotas": [{"namespace": "shop", "name": "shop-quota",
                    "hard": {"requests.cpu": "10", "requests.memory": "24Gi", "pods": "20"},
                    "used": {"requests.cpu": "4700m", "requests.memory": "9Gi", "pods": "7"}}],
        "limitranges": [{"namespace": "shop", "name": "defaults", "limits": [
            {"type": "Container", "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
             "default": {"memory": "512Mi"}, "max": {"memory": "8Gi"}}]}],
        "priority_classes": [
            {"name": "system-node-critical", "value": 2000001000},
            {"name": "system-cluster-critical", "value": 2000000000},
            {"name": "low", "value": -10, "preemption_policy": "Never"},
        ],
        "runtime_classes": [],
        "storage_classes": [
            {"name": "gp3", "provisioner": "ebs.csi.aws.com", "volume_binding_mode": "WaitForFirstConsumer", "default": True},
            {"name": "local-ssd", "provisioner": "kubernetes.io/no-provisioner", "volume_binding_mode": "WaitForFirstConsumer"},
        ],
        "pvs": [{"name": "local-pv-w3", "storage_class": "local-ssd", "capacity": "200Gi", "local": True,
                 "claim": "data/data-postgres-0", "phase": "Bound",
                 "node_affinity_terms": [{"matchExpressions": [
                     {"key": "kubernetes.io/hostname", "operator": "In", "values": ["w3"]}]}]}],
        "pvcs": [{"namespace": "data", "name": "data-postgres-0", "storage_class": "local-ssd",
                  "volume_name": "local-pv-w3", "size": "100Gi", "access_modes": ["ReadWriteOnce"], "phase": "Bound"}],
        "usage": {"source": "fixture", "window": "7d p95", "pods": {
            "shop/worker-66b-0": {"cpu": "450m", "memory": "1400Mi"},
            "shop/worker-66b-1": {"cpu": "420m", "memory": "1350Mi"},
            "shop/web-55d-0": {"cpu": "300m", "memory": "900Mi"},
            "analytics/spark-2c4-0": {"cpu": "400m", "memory": "6Gi"},
        }},
        "data_gaps": [],
    }


if __name__ == "__main__":
    out = Path(__file__).with_name("demo_cluster.json")
    out.write_text(json.dumps(build(), indent=1))
    print(f"wrote {out}")
