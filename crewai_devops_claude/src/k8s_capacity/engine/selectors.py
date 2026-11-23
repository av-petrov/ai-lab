"""Label selectors, node selector terms and taint/toleration matching.

Semantics follow k8s.io/apimachinery labels and k8s.io/component-helpers
(nodeaffinity, taints) as closely as needed for the scheduler filters.
"""

from __future__ import annotations


def match_label_selector(selector: dict | None, labels: dict) -> bool:
    """metav1.LabelSelector. None selects nothing, {} selects everything."""
    if selector is None:
        return False
    labels = labels or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and key in labels and labels[key] in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _match_node_requirement(expr: dict, value: str | None, present: bool) -> bool:
    op, values = expr["operator"], expr.get("values") or []
    if op == "In":
        return present and value in values
    if op == "NotIn":
        return not present or value not in values
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    if op in ("Gt", "Lt"):
        if not present:
            return False
        try:
            lhs, rhs = int(value), int(values[0])
        except (TypeError, ValueError, IndexError):
            return False
        return lhs > rhs if op == "Gt" else lhs < rhs
    return False


def match_node_selector_term(term: dict, node_name: str, node_labels: dict) -> bool:
    """A NodeSelectorTerm: all matchExpressions and matchFields must hold.
    An empty term matches nothing (API semantics)."""
    exprs = term.get("matchExpressions") or []
    fields = term.get("matchFields") or []
    if not exprs and not fields:
        return False
    for e in exprs:
        if not _match_node_requirement(e, node_labels.get(e["key"]), e["key"] in node_labels):
            return False
    for f in fields:
        if f["key"] != "metadata.name":
            return False
        if not _match_node_requirement(f, node_name, True):
            return False
    return True


def match_node_selector_terms(terms: list[dict] | None, node_name: str, node_labels: dict) -> bool:
    """NodeSelector: terms are ORed. None means no constraint."""
    if terms is None:
        return True
    return any(match_node_selector_term(t, node_name, node_labels) for t in terms)


def toleration_tolerates(tol: dict, taint: dict) -> bool:
    if tol.get("effect") and tol["effect"] != taint.get("effect"):
        return False
    op = tol.get("operator") or "Equal"
    key = tol.get("key") or ""
    if not key:
        return op == "Exists"  # empty key + Exists tolerates everything
    if key != taint.get("key"):
        return False
    if op == "Exists":
        return True
    return (tol.get("value") or "") == (taint.get("value") or "")


def untolerated_taints(tolerations: list[dict], taints: list[dict], effects=("NoSchedule", "NoExecute")) -> list[dict]:
    return [
        t for t in taints or []
        if t.get("effect") in effects and not any(toleration_tolerates(tol, t) for tol in tolerations or [])
    ]
