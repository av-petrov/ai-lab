"""Cluster state + a read-only emulation of kube-scheduler Filter/Score/PostFilter.

Implemented filters (kube-scheduler plugin names):
  NodeUnschedulable, NodeName, TaintToleration, NodeAffinity (nodeSelector + required),
  NodePorts, NodeResourcesFit (all resources incl. pods, extended, hugepages, ephemeral-storage)
  + synthetic pod-ips and CSI attach limits, VolumeBinding/VolumeZone (PV nodeAffinity,
  SC allowedTopologies, local PV availability), VolumeRestrictions (RWO claims on one node),
  PodTopologySpread (DoNotSchedule, minDomains, nodeAffinityPolicy/nodeTaintsPolicy, matchLabelKeys),
  InterPodAffinity (required affinity, anti-affinity, symmetric anti-affinity of existing pods).
Scoring (only to pick a node among feasible ones): LeastAllocated/MostAllocated on cpu+memory,
  preferred node affinity, preferred pod (anti)affinity, PreferNoSchedule taints,
  ScheduleAnyway spread. Ties are broken by node name (the real scheduler picks randomly).
PostFilter: DefaultPreemption (victims = lower priority pods, minimal set with reprieve,
  candidate ranking by PDB violations / highest victim priority / number of victims).
"""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import HOSTNAME_LABEL, NodeInfo, PodShape, resolve_int_or_percent
from .quantity import CSI_VOLUMES, POD_IPS, fmt
from .selectors import (
    match_label_selector,
    match_node_selector_terms,
    toleration_tolerates,
    untolerated_taints,
)

UNSCHEDULABLE_TAINT = {"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}


# =========================================================================== quota
class QuotaLedger:
    """ResourceQuota admission emulation. Pending pods consume quota too."""

    def __init__(self, quotas: list[dict]):
        self.quotas = copy.deepcopy(quotas or [])  # [{namespace,name,hard:{k:int},used:{k:int},scopes,scope_selector}]

    def clone(self) -> "QuotaLedger":
        return QuotaLedger(self.quotas)

    @staticmethod
    def pod_usage(pod: PodShape) -> dict[str, int]:
        u: dict[str, int] = {"pods": 1, "count/pods": 1}
        for r, v in pod.requests.items():
            if r in ("pods",) or r == POD_IPS or r.startswith(CSI_VOLUMES):
                continue
            u[f"requests.{r}"] = v
            if r in ("cpu", "memory", "ephemeral-storage"):
                u[r] = v
        for r, v in pod.limits.items():
            u[f"limits.{r}"] = v
        return u

    @staticmethod
    def _applies(q: dict, pod: PodShape) -> tuple[bool, str | None]:
        for scope in q.get("scopes") or []:
            if scope == "BestEffort" and pod.qos != "BestEffort":
                return False, None
            if scope == "NotBestEffort" and pod.qos == "BestEffort":
                return False, None
            if scope in ("Terminating", "NotTerminating", "CrossNamespacePodAffinity"):
                return True, f"quota scope {scope} not evaluated; quota applied conservatively"
        for expr in (q.get("scope_selector") or {}).get("matchExpressions") or []:
            if expr.get("scopeName") == "PriorityClass":
                op, vals, pc = expr.get("operator"), expr.get("values") or [], pod.priority_class
                ok = (op == "In" and pc in vals) or (op == "NotIn" and pc not in vals) or \
                     (op == "Exists" and pc is not None) or (op == "DoesNotExist" and pc is None)
                if not ok:
                    return False, None
        return True, None

    def check(self, pod: PodShape, count: int = 1) -> list[str]:
        """Violations (empty == admitted)."""
        usage = self.pod_usage(pod)
        out = []
        for q in self.quotas:
            if q["namespace"] != pod.namespace:
                continue
            applies, _ = self._applies(q, pod)
            if not applies:
                continue
            for k, hard in q["hard"].items():
                need = usage.get(k, 0) * count
                if need and q["used"].get(k, 0) + need > hard:
                    out.append(
                        f"quota {q['name']}: {k} used {fmt(_res_of(k), q['used'].get(k, 0))} + "
                        f"{fmt(_res_of(k), need)} > hard {fmt(_res_of(k), hard)}"
                    )
            # requests.* with no request on the pod: k8s rejects pods lacking a request
            # for a resource constrained by the quota
            for k in q["hard"]:
                if k.startswith(("requests.", "limits.")) or k in ("cpu", "memory"):
                    if k not in usage and _res_of(k) in ("cpu", "memory"):
                        out.append(f"quota {q['name']} constrains {k} but the pod does not set it (rejected)")
        return out

    def charge(self, pod: PodShape, sign: int = 1) -> None:
        usage = self.pod_usage(pod)
        for q in self.quotas:
            if q["namespace"] != pod.namespace or not self._applies(q, pod)[0]:
                continue
            for k in q["hard"]:
                if k in usage:
                    q["used"][k] = q["used"].get(k, 0) + sign * usage[k]

    def unverified_notes(self, namespace: str) -> list[str]:
        notes = []
        for q in self.quotas:
            if q["namespace"] == namespace:
                for s in q.get("scopes") or []:
                    if s in ("Terminating", "NotTerminating", "CrossNamespacePodAffinity"):
                        notes.append(f"quota {q['name']} scope {s} not evaluated")
                for k in q["hard"]:
                    if k.startswith("count/") and k != "count/pods":
                        notes.append(f"quota {q['name']}: object count {k} not evaluated")
        return notes


def _res_of(quota_key: str) -> str:
    return quota_key.split(".", 1)[1] if quota_key.startswith(("requests.", "limits.")) else quota_key


# =========================================================================== results
@dataclass
class Placement:
    pod: str
    node: str | None
    reasons: dict[str, list[str]] = field(default_factory=dict)  # node -> failing filters
    victims: list[str] = field(default_factory=list)
    pdb_violations: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.node is not None

    def summary(self) -> str:
        """kubectl-style '0/5 nodes are available: 3 Insufficient cpu, ...'"""
        if self.ok:
            v = f" (preempting {len(self.victims)} pod(s))" if self.victims else ""
            return f"scheduled on {self.node}{v}"
        c = Counter()
        for rs in self.reasons.values():
            for r in rs:
                c[r.split(" (")[0]] += 1
        parts = ", ".join(f"{n} {r}" for r, n in sorted(c.items(), key=lambda x: (-x[1], x[0])))
        return f"0/{len(self.reasons)} nodes are available: {parts}"


# =========================================================================== state
class ClusterState:
    def __init__(self, nodes: list[NodeInfo], namespaces: dict[str, dict] | None = None,
                 pdbs: list[dict] | None = None, quotas: list[dict] | None = None,
                 local_pvs: list[dict] | None = None, scoring: str = "LeastAllocated"):
        self.nodes: dict[str, NodeInfo] = {n.name: n for n in nodes}
        self.namespaces = namespaces or {}
        self.pdbs = pdbs or []
        self.quota = QuotaLedger(quotas or [])
        self.local_pvs = copy.deepcopy(local_pvs or [])  # available local PVs: {name, node, storage_class, size}
        self.scoring = scoring
        self.pods: dict[str, PodShape] = {}
        self.pod_node: dict[str, str] = {}
        self.by_node: dict[str, dict[str, PodShape]] = {n: {} for n in self.nodes}
        self.used: dict[str, dict[str, int]] = {n: {} for n in self.nodes}
        self.ports: dict[str, set] = {n: set() for n in self.nodes}
        self.claim_node: dict[str, str] = {}

    # ------------------------------------------------------------------ mutation
    def clone(self) -> "ClusterState":
        s = ClusterState.__new__(ClusterState)
        s.nodes = dict(self.nodes)
        s.namespaces = self.namespaces
        s.pdbs = self.pdbs
        s.quota = self.quota.clone()
        s.local_pvs = copy.deepcopy(self.local_pvs)
        s.scoring = self.scoring
        s.pods = dict(self.pods)
        s.pod_node = dict(self.pod_node)
        s.by_node = {k: dict(v) for k, v in self.by_node.items()}
        s.used = {k: dict(v) for k, v in self.used.items()}
        s.ports = {k: set(v) for k, v in self.ports.items()}
        s.claim_node = dict(self.claim_node)
        return s

    def add_node(self, node: NodeInfo) -> None:
        self.nodes[node.name] = node
        self.by_node[node.name] = {}
        self.used[node.name] = {}
        self.ports[node.name] = set()

    def remove_node(self, name: str) -> list[PodShape]:
        pods = list(self.by_node.get(name, {}).values())
        for p in pods:
            self.remove_pod(p.key)
        self.nodes.pop(name, None)
        self.by_node.pop(name, None)
        self.used.pop(name, None)
        self.ports.pop(name, None)
        return pods

    def add_pod(self, pod: PodShape, node: str, charge_quota: bool = False) -> None:
        # PodShape objects are shared between cloned states: never mutate them here.
        self.pods[pod.key] = pod
        self.pod_node[pod.key] = node
        self.by_node[node][pod.key] = pod
        u = self.used[node]
        for r, v in pod.requests.items():
            u[r] = u.get(r, 0) + v
        for hp in pod.host_ports:
            self.ports[node].add(hp)
        for c in pod.rwo_claims:
            self.claim_node.setdefault(c, node)
        for need in pod.needs_local_pv:
            pv = self._find_local_pv(node, need)
            if pv is not None:
                self.local_pvs.remove(pv)
        if charge_quota:
            self.quota.charge(pod, +1)

    def remove_pod(self, key: str, refund_quota: bool = False) -> PodShape | None:
        pod = self.pods.pop(key, None)
        if pod is None:
            return None
        node = self.pod_node.pop(key)
        self.by_node[node].pop(key, None)
        u = self.used[node]
        for r, v in pod.requests.items():
            u[r] = u.get(r, 0) - v
        for hp in pod.host_ports:
            self.ports[node].discard(hp)
        for c in pod.rwo_claims:
            if self.claim_node.get(c) == node and not any(
                c in p.rwo_claims for p in self.by_node[node].values()
            ):
                self.claim_node.pop(c, None)
        if refund_quota:
            self.quota.charge(pod, -1)
        return pod

    # ------------------------------------------------------------------ helpers
    def free(self, node: str) -> dict[str, int]:
        n = self.nodes[node]
        return {r: a - self.used[node].get(r, 0) for r, a in n.allocatable.items()}

    def _term_namespaces(self, term: dict, owner_ns: str) -> set[str] | None:
        """None == all namespaces."""
        nss = set(term.get("namespaces") or [])
        sel = term.get("namespaceSelector")
        if sel is not None:
            if not sel.get("matchLabels") and not sel.get("matchExpressions"):
                return None
            nss |= {n for n, labels in self.namespaces.items() if match_label_selector(sel, labels)}
            return nss
        return nss or {owner_ns}

    def _term_selects(self, term: dict, owner: PodShape, target: PodShape) -> bool:
        nss = self._term_namespaces(term, owner.namespace)
        if nss is not None and target.namespace not in nss:
            return False
        sel = term.get("labelSelector")
        keys = term.get("matchLabelKeys") or []
        if keys and sel is not None:
            sel = copy.deepcopy(sel)
            sel.setdefault("matchExpressions", [])
            for k in keys:
                if k in owner.labels:
                    sel["matchExpressions"].append({"key": k, "operator": "In", "values": [owner.labels[k]]})
        return match_label_selector(sel, target.labels)

    def _domain_nodes(self, key: str, value: str) -> list[str]:
        return [n for n, info in self.nodes.items() if info.labels.get(key) == value]

    def node_level_ok(self, pod: PodShape, node: NodeInfo) -> bool:
        """Selector + required node affinity only (what NodeAffinity checks)."""
        for k, v in pod.node_selector.items():
            if node.labels.get(k) != v:
                return False
        return match_node_selector_terms(pod.required_node_terms, node.name, node.labels)

    def node_eligible(self, pod: PodShape, node: NodeInfo) -> bool:
        """Selector/affinity match and no untolerated NoSchedule/NoExecute taint (resources ignored)."""
        return self.node_level_ok(pod, node) and not untolerated_taints(pod.tolerations, node.taints)

    def _find_local_pv(self, node: str, need: dict) -> dict | None:
        for pv in self.local_pvs:
            if pv["node"] == node and pv["storage_class"] == need["storage_class"] and pv["size"] >= need["size"]:
                return pv
        return None

    # ------------------------------------------------------------------ filters
    def filter(self, pod: PodShape, node_name: str, ignore_resources: bool = False) -> list[str]:
        n = self.nodes[node_name]
        reasons: list[str] = []
        taints = list(n.taints)
        if not n.ready:
            return ["node(s) were not ready"]
        if n.unschedulable:
            if not any(toleration_tolerates(t, UNSCHEDULABLE_TAINT) for t in pod.tolerations):
                return ["node(s) were unschedulable"]
        if pod.pinned_node and pod.pinned_node != node_name:
            return ["node(s) didn't match the requested node name"]
        bad = untolerated_taints(pod.tolerations, taints)
        if bad:
            t = bad[0]
            return [f"node(s) had untolerated taint {{{t.get('key')}: {t.get('value', '')}}}"]
        if not self.node_level_ok(pod, n):
            return ["node(s) didn't match Pod's node affinity/selector"]
        # NodePorts
        for ip, proto, port in pod.host_ports:
            for uip, uproto, uport in self.ports[node_name]:
                if uport == port and uproto == proto and (ip == uip or "0.0.0.0" in (ip, uip)):
                    reasons.append(f"node(s) didn't have free ports for the requested pod ports ({port}/{proto})")
        # NodeResourcesFit
        if not ignore_resources:
            reasons.extend(self._resource_reasons(pod, node_name))
        # Volumes
        for terms in pod.volume_node_terms:
            if not match_node_selector_terms(terms, n.name, n.labels):
                reasons.append("node(s) had volume node affinity conflict")
                break
        if pod.local_pv_node and pod.local_pv_node != node_name:
            reasons.append("node(s) had volume node affinity conflict (local PV)")
        for c in pod.rwo_claims:
            owner = self.claim_node.get(c)
            if owner is not None and owner != node_name:
                reasons.append(f"ReadWriteOnce claim {c} is attached to {owner}")
        for need in pod.needs_local_pv:
            if self._find_local_pv(node_name, need) is None:
                reasons.append("node(s) didn't find available persistent volumes to bind (local)")
        if reasons:
            return reasons
        # PodTopologySpread
        reasons.extend(self._spread_reasons(pod, n))
        # InterPodAffinity
        reasons.extend(self._affinity_reasons(pod, n))
        return reasons

    def _resource_reasons(self, pod: PodShape, node_name: str) -> list[str]:
        n = self.nodes[node_name]
        out = []
        used = self.used[node_name]
        for r, need in sorted(pod.requests.items()):
            if need <= 0:
                continue
            if r not in n.allocatable:
                if r == POD_IPS or r.startswith(CSI_VOLUMES):
                    continue  # limit unknown -> reported as not verified, never as fit
                out.append(f"Insufficient {r} (node has none)")
                continue
            free = n.allocatable[r] - used.get(r, 0)
            if need > free:
                label = "Too many pods" if r == "pods" else (
                    "Insufficient pod IPs" if r == POD_IPS else f"Insufficient {r}")
                out.append(f"{label} (free {fmt(r, free)}, need {fmt(r, need)})")
        return out

    def unverified_resources(self, pod: PodShape) -> list[str]:
        out = set()
        for r, need in pod.requests.items():
            if need > 0 and (r == POD_IPS or r.startswith(CSI_VOLUMES)):
                missing = [n for n, i in self.nodes.items() if r not in i.allocatable]
                if missing:
                    out.add(f"{r}: limit unknown on {len(missing)} node(s)")
        return sorted(out)

    def _spread_reasons(self, pod: PodShape, node: NodeInfo) -> list[str]:
        out = []
        for c in pod.spread:
            if c.get("whenUnsatisfiable", "DoNotSchedule") != "DoNotSchedule":
                continue
            key = c["topologyKey"]
            if key not in node.labels:
                out.append("node(s) didn't match pod topology spread constraints (missing required label)")
                continue
            counts = self._spread_counts(pod, c)
            if not counts:
                continue
            min_count = min(counts.values())
            if c.get("minDomains") and len(counts) < int(c["minDomains"]):
                min_count = 0
            self_match = 1 if self._spread_selects(pod, c, pod) else 0
            skew = counts.get(node.labels[key], 0) + self_match - min_count
            if skew > int(c.get("maxSkew", 1)):
                out.append(f"node(s) didn't match pod topology spread constraints (skew {skew} > {c.get('maxSkew', 1)} on {key})")
        return out

    def _spread_selects(self, owner: PodShape, c: dict, target: PodShape) -> bool:
        if target.namespace != owner.namespace:
            return False
        sel = c.get("labelSelector")
        if c.get("matchLabelKeys") and sel is not None:
            sel = copy.deepcopy(sel)
            sel.setdefault("matchExpressions", [])
            for k in c["matchLabelKeys"]:
                if k in owner.labels:
                    sel["matchExpressions"].append({"key": k, "operator": "In", "values": [owner.labels[k]]})
        return match_label_selector(sel, target.labels)

    def _spread_counts(self, pod: PodShape, c: dict) -> dict[str, int]:
        key = c["topologyKey"]
        honor_affinity = c.get("nodeAffinityPolicy", "Honor") == "Honor"
        honor_taints = c.get("nodeTaintsPolicy", "Ignore") == "Honor"
        all_keys = [x["topologyKey"] for x in pod.spread if x.get("whenUnsatisfiable", "DoNotSchedule") == "DoNotSchedule"]
        counts: dict[str, int] = {}
        for name, n in self.nodes.items():
            if not n.ready or any(k not in n.labels for k in all_keys):
                continue
            if honor_affinity and not self.node_level_ok(pod, n):
                continue
            if honor_taints and untolerated_taints(pod.tolerations, n.taints):
                continue
            dom = n.labels[key]
            counts.setdefault(dom, 0)
            counts[dom] += sum(1 for q in self.by_node[name].values() if self._spread_selects(pod, c, q))
        return counts

    def _affinity_reasons(self, pod: PodShape, node: NodeInfo) -> list[str]:
        out = []
        # incoming pod: required affinity
        if pod.pod_affinity:
            any_match_anywhere = False
            ok_all = True
            for term in pod.pod_affinity:
                key = term["topologyKey"]
                matches_global = any(self._term_selects(term, pod, q) for q in self.pods.values())
                any_match_anywhere |= matches_global
                if key not in node.labels:
                    ok_all = False
                    continue
                dom = self._domain_nodes(key, node.labels[key])
                if not any(self._term_selects(term, pod, q) for d in dom for q in self.by_node[d].values()):
                    ok_all = False
            if not ok_all:
                self_ok = all(self._term_selects(t, pod, pod) for t in pod.pod_affinity)
                if any_match_anywhere or not self_ok:
                    out.append("node(s) didn't match pod affinity rules")
        # incoming pod: required anti-affinity
        for term in pod.pod_anti_affinity:
            key = term["topologyKey"]
            if key not in node.labels:
                continue
            dom = self._domain_nodes(key, node.labels[key])
            if any(self._term_selects(term, pod, q) for d in dom for q in self.by_node[d].values()):
                out.append("node(s) didn't match pod anti-affinity rules")
                break
        # existing pods: their anti-affinity against the incoming pod (symmetry)
        for q in self.pods.values():
            if not q.pod_anti_affinity:
                continue
            qnode = self.nodes.get(self.pod_node[q.key])
            if qnode is None:
                continue
            for term in q.pod_anti_affinity:
                key = term["topologyKey"]
                if key in node.labels and qnode.labels.get(key) == node.labels[key] and self._term_selects(term, q, pod):
                    out.append("node(s) didn't satisfy existing pods anti-affinity rules")
                    break
            if out and out[-1].startswith("node(s) didn't satisfy existing"):
                break
        return out

    # ------------------------------------------------------------------ scoring
    def _score(self, pod: PodShape, node_name: str) -> float:
        n = self.nodes[node_name]
        score = 0.0
        fr = []
        for r in ("cpu", "memory"):
            alloc = n.allocatable.get(r, 0)
            if alloc <= 0:
                continue
            req = self.used[node_name].get(r, 0) + max(pod.requests.get(r, 0), 100 if r == "cpu" else 200 * 2**20)
            frac = max(0.0, (alloc - req) / alloc)
            fr.append(frac if self.scoring == "LeastAllocated" else 1 - frac)
        score += 100 * (sum(fr) / len(fr) if fr else 0)
        for pref in pod.preferred_node_terms:
            if match_node_selector_terms([pref["preference"]], n.name, n.labels):
                score += pref.get("weight", 1)
        for w in pod.pod_affinity_preferred + [dict(x, weight=-x.get("weight", 1)) for x in pod.pod_anti_affinity_preferred]:
            term = w.get("podAffinityTerm") or {}
            key = term.get("topologyKey")
            if key and key in n.labels:
                dom = self._domain_nodes(key, n.labels[key])
                if any(self._term_selects(term, pod, q) for d in dom for q in self.by_node[d].values()):
                    score += w.get("weight", 1)
        score -= 10 * len([t for t in n.taints if t.get("effect") == "PreferNoSchedule"
                           and not any(toleration_tolerates(x, t) for x in pod.tolerations)])
        if not pod.spread and pod.workload_key:
            # cluster-default PodTopologySpread (hostname maxSkew 3 / zone maxSkew 5, ScheduleAnyway)
            same = sum(1 for q in self.by_node[node_name].values() if q.workload_key == pod.workload_key)
            score -= 15 * same
        for c in pod.spread:
            if c.get("whenUnsatisfiable") == "ScheduleAnyway" and c["topologyKey"] in n.labels:
                counts = self._spread_counts(pod, c)
                score -= 5 * counts.get(n.labels[c["topologyKey"]], 0)
        return score

    # ------------------------------------------------------------------ schedule
    def schedule(self, pod: PodShape, allow_preemption: bool = False, candidates: list[str] | None = None) -> Placement:
        names = sorted(candidates if candidates is not None else self.nodes)
        reasons: dict[str, list[str]] = {}
        feasible = []
        for name in names:
            r = self.filter(pod, name)
            if r:
                reasons[name] = r
            else:
                feasible.append(name)
        unverified = self.unverified_resources(pod) + list(pod.unverified)
        if feasible:
            best = max(feasible, key=lambda nm: (self._score(pod, nm), _neg(nm)))
            return Placement(pod.key, best, unverified=unverified)
        result = Placement(pod.key, None, reasons=reasons, unverified=unverified)
        if allow_preemption and pod.preemption_policy != "Never":
            pre = self._preempt(pod, names)
            if pre is not None:
                node, victims, viol = pre
                result.node, result.victims, result.pdb_violations = node, victims, viol
        return result

    def _preempt(self, pod: PodShape, names: list[str]):
        best = None
        for name in names:
            lower = sorted(
                (q for q in self.by_node[name].values() if q.priority < pod.priority),
                key=lambda q: (q.priority, q.key),
            )
            if not lower:
                continue
            trial = self.clone()
            for q in lower:
                trial.remove_pod(q.key)
            if trial.filter(pod, name):
                continue
            # reprieve: re-add victims from highest priority while the pod still fits
            victims = []
            for q in sorted(lower, key=lambda q: (-q.priority, q.key)):
                trial.add_pod(q, name)
                if trial.filter(pod, name):
                    trial.remove_pod(q.key)
                    victims.append(q)
            violations = self.pdb_violations([v.key for v in victims])
            rank = (len(violations), max(v.priority for v in victims), sum(v.priority for v in victims), len(victims), name)
            if best is None or rank < best[0]:
                best = (rank, name, [v.key for v in victims], violations)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def place_with_preemption(self, pod: PodShape, placement: Placement) -> list[PodShape]:
        evicted = [self.remove_pod(k) for k in placement.victims]
        self.add_pod(pod, placement.node)
        return [e for e in evicted if e is not None]

    # ------------------------------------------------------------------ PDB
    def pdb_status(self, pdb: dict) -> dict:
        matching = [p for p in self.pods.values() if p.namespace == pdb["namespace"]
                    and match_label_selector(pdb.get("selector"), p.labels)]
        healthy = len(matching)
        expected = max(pdb.get("expected_pods") or 0, healthy)
        if pdb.get("min_available") is not None:
            desired = resolve_int_or_percent(pdb["min_available"], expected, True, 0)
        elif pdb.get("max_unavailable") is not None:
            desired = expected - resolve_int_or_percent(pdb["max_unavailable"], expected, True, 0)
        else:
            desired = 0
        return {"name": f"{pdb['namespace']}/{pdb['name']}", "healthy": healthy, "desired": desired,
                "expected": expected, "allowed": max(0, healthy - desired)}

    def pdb_violations(self, removed_keys: list[str]) -> list[str]:
        out = []
        for pdb in self.pdbs:
            st = self.pdb_status(pdb)
            n = sum(1 for k in removed_keys if k in self.pods and self.pods[k].namespace == pdb["namespace"]
                    and match_label_selector(pdb.get("selector"), self.pods[k].labels))
            if n > st["allowed"]:
                out.append(f"{st['name']} (removes {n}, allowed {st['allowed']})")
        return out

    # ------------------------------------------------------------------ totals
    def total_requests(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for u in self.used.values():
            for r, v in u.items():
                out[r] += v
        return dict(out)


def _neg(name: str):
    # tie-break: alphabetically first node wins under max()
    return tuple(-ord(c) for c in name)


def hostname_of(node: NodeInfo) -> str:
    return node.labels.get(HOSTNAME_LABEL, node.name)
