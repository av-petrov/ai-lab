"""Kubernetes resource quantities -> integers.

Internal units (all integers, so sums are exact):
  cpu                -> millicores
  memory, storage,
  ephemeral-storage,
  hugepages-*        -> bytes
  everything else    -> plain count (pods, nvidia.com/gpu, synthetic pod-ips ...)

No dependency on the kubernetes client, so the engine runs (and is tested)
without a cluster.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal(10**3),
    "M": Decimal(10**6),
    "G": Decimal(10**9),
    "T": Decimal(10**12),
    "P": Decimal(10**15),
    "E": Decimal(10**18),
}

# Synthetic resources the engine tracks next to the API ones.
POD_IPS = "pod-ips"            # one per non-hostNetwork pod
CSI_VOLUMES = "attachable-volumes"  # per-node CSI attach limit (summed over drivers)


def parse_decimal(value) -> Decimal:
    if value is None:
        raise ValueError("empty quantity")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        raise ValueError("empty quantity")
    for suffix, mult in _BINARY.items():
        if s.endswith(suffix):
            return Decimal(s[: -len(suffix)]) * mult
    try:  # plain numbers and exponent form ("1.5", "1e3")
        d = Decimal(s)
        if d.is_finite():
            return d
    except InvalidOperation:
        pass
    suffix = s[-1] if s[-1] in _DECIMAL and not s[-1].isdigit() else ""
    number = s[: -len(suffix)] if suffix else s
    try:
        return Decimal(number) * _DECIMAL[suffix]
    except InvalidOperation as exc:
        raise ValueError(f"bad quantity {value!r}") from exc


def to_internal(resource: str, value) -> int:
    """Convert an API quantity to the engine integer unit (rounding up, like the scheduler)."""
    d = parse_decimal(value)
    if resource == "cpu":
        d = d * 1000
    return int(math.ceil(d))


def parse_resource_list(rl: dict | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in (rl or {}).items():
        out[k] = to_internal(k, v)
    return out


def fmt(resource: str, value: int | None) -> str:
    """Human readable form of an internal integer."""
    if value is None:
        return "n/a"
    if resource == "cpu":
        return f"{value}m" if value % 1000 else f"{value // 1000}"
    if resource == "memory" or resource.endswith("storage") or resource.startswith("hugepages-"):
        for unit, mult in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
            if abs(value) >= mult:
                q = value / mult
                return f"{q:.2f}".rstrip("0").rstrip(".") + unit
        return str(value)
    return str(value)


def fmt_list(rl: dict[str, int]) -> dict[str, str]:
    return {k: fmt(k, v) for k, v in sorted(rl.items())}


def add(a: dict[str, int], b: dict[str, int], sign: int = 1) -> dict[str, int]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + sign * v
    return out


def scale(a: dict[str, int], n: int) -> dict[str, int]:
    return {k: v * n for k, v in a.items()}
