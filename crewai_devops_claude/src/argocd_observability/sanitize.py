"""Redaction and size limits applied to everything before it reaches the LLM."""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "***REDACTED***"

# keys whose *values* are always secret (dict keys, env var names, annotation names ...)
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|api[-_]?key|apikey|access[-_]?key|private[-_]?key|"
    r"client[-_]?secret|credential|auth(orization)?|bearer|cookie|session[-_]?id|dsn|"
    r"connection[-_]?string|signing[-_]?key|encryption[-_]?key|ssh[-_]?key|webhook[-_]?url)",
    re.IGNORECASE,
)
# keys that look sensitive but are harmless status fields we need for diagnosis
_SAFE_KEYS = {"tokenRequests", "automountServiceAccountToken", "serviceAccountName", "serviceAccount",
              "secretName", "secretRef", "secretKeyRef", "imagePullSecrets", "authorization_mode",
              "author", "authors"}

_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "-----PRIVATE KEY " + REDACTED + "-----"),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}"), r"\1 " + REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), REDACTED),
    # well-known token formats
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), REDACTED),
    # credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{0,15}://)([^/\s:@]{1,128}):([^/\s@]{1,256})@"),
     r"\1\2:" + REDACTED + "@"),
    # key=value / key: value / "key": "value" pairs with a sensitive key.
    # All quantifiers are bounded and the key is anchored at a non-identifier char: linear time even on
    # multi-KB tokens (an unbounded version backtracks catastrophically).
    (re.compile(
        r"(?i)(?<![A-Za-z0-9_.-])([\"']?[A-Za-z0-9_.-]{0,40}(?:pass(?:word|wd)?|pwd|secret|token|api[-_]?key|apikey|"
        r"access[-_]?key|private[-_]?key|client[-_]?secret|credential|auth)[A-Za-z0-9_.-]{0,40}[\"']?[ \t]{0,4}[:=][ \t]{0,4})"
        r"(\"[^\"\n]{0,512}\"|'[^'\n]{0,512}'|[^\s,;&}\]]{1,512})"),
     r"\1" + REDACTED),
]


def redact_text(text: str) -> str:
    if not text:
        return text
    for pat, repl in _TEXT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_sensitive_key(key: str) -> bool:
    return key not in _SAFE_KEYS and bool(_SENSITIVE_KEY.search(key))


def redact(obj: Any) -> Any:
    """Recursively redact a JSON-like structure.

    * values under sensitive keys are replaced;
    * Kubernetes env entries ``{"name": "DB_PASSWORD", "value": "..."}`` have their value replaced;
    * every string is passed through :func:`redact_text`;
    * ``kubectl.kubernetes.io/last-applied-configuration`` and ``managedFields`` are dropped (noise,
      and the former can carry full Secret manifests).
    """
    if isinstance(obj, dict):
        if "name" in obj and "value" in obj and isinstance(obj.get("name"), str) and _is_sensitive_key(obj["name"]):
            return {**{k: redact(v) for k, v in obj.items() if k != "value"}, "value": REDACTED}
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in ("managedFields",) or ks == "kubectl.kubernetes.io/last-applied-configuration":
                continue
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and _is_sensitive_key(ks):
                out[k] = REDACTED
            elif ks in ("data", "stringData") and isinstance(v, dict):
                out[k] = {dk: REDACTED for dk in v}
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# --------------------------------------------------------------------------- truncation
def truncate_text(text: str, max_chars: int, *, keep: str = "tail") -> str:
    """Cut ``text`` to ``max_chars``; keep the tail (logs: the newest lines matter) or head."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    marker = f"[... {dropped} chars truncated ...]\n"
    if keep == "head":
        return text[:max_chars] + "\n" + marker.strip()
    return marker + text[-max_chars:]


def tail_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if n <= 0 or len(lines) <= n:
        return "\n".join(lines)
    return f"[... {len(lines) - n} earlier lines omitted ...]\n" + "\n".join(lines[-n:])


def shrink(obj: Any, max_list: int = 25, max_str: int = 600, _depth: int = 0, max_depth: int = 8) -> Any:
    """Bound lists, strings and nesting depth of a JSON-like structure."""
    if _depth > max_depth:
        return "[... nested too deep, omitted ...]"
    if isinstance(obj, dict):
        return {k: shrink(v, max_list, max_str, _depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [shrink(v, max_list, max_str, _depth + 1, max_depth) for v in obj[:max_list]]
        if len(obj) > max_list:
            out.append(f"[... {len(obj) - max_list} more items omitted ...]")
        return out
    if isinstance(obj, str) and len(obj) > max_str:
        return obj[:max_str] + f"[... {len(obj) - max_str} chars truncated]"
    return obj


def to_llm_json(obj: Any, max_chars: int, max_list: int = 25, max_str: int = 600) -> str:
    """Redact, then shrink until the JSON fits ``max_chars`` (hard cut as last resort)."""
    data = redact(obj)
    text = ""
    for ml, ms in ((max_list, max_str), (max(5, max_list // 2), max(200, max_str // 2)), (5, 160), (3, 80)):
        text = json.dumps(shrink(data, ml, ms), ensure_ascii=False, default=str, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
    return truncate_text(text, max_chars, keep="head")
