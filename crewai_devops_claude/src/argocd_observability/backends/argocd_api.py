"""Mode 2: Argo CD API server with a read-only local account (``role:readonly``)."""

from __future__ import annotations

import json
from typing import Any

from ..guard import check_argocd_request
from ..http import GuardedHttpClient
from .base import Backend

CAN_I = [  # (resource, action)
    ("applications", "get"), ("logs", "get"), ("projects", "get"),
    ("applications", "sync"), ("applications", "update"), ("applications", "delete"),
    ("applications", "action/*"), ("exec", "create"),
]
# RBAC object per resource: app-scoped resources are "<project>/<app>", so ask about "*/*"
# (a bare "*" never matches a "*/*" policy and would report a false "no").
CAN_I_OBJECT = {"applications": "*/*", "logs": "*/*", "exec": "*/*", "projects": "*"}
READ_ACTIONS = {("applications", "get"), ("logs", "get"), ("projects", "get")}


class ArgoCDApiBackend(Backend):
    mode = "argocd"

    def __init__(self, base_url: str, token: str, *, verify: bool = True, timeout: float = 15.0,
                 max_bytes: int = 20_000_000, http: GuardedHttpClient | None = None) -> None:
        super().__init__()
        self.base_url = base_url
        self.http = http or GuardedHttpClient(base_url, check_argocd_request,
                                              headers={"Authorization": f"Bearer {token}"},
                                              verify=verify, timeout=timeout, max_bytes=max_bytes)

    def describe(self) -> str:
        return f"Argo CD API {self.base_url}"

    def list_applications(self, project: str | None = None, selector: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if project:
            params["projects"] = project
        if selector:
            params["selector"] = selector
        return (self.http.get_json("/api/v1/applications", params or None) or {}).get("items") or []

    def get_application(self, name: str) -> dict:
        return self.http.get_json(f"/api/v1/applications/{name}")

    def _fetch_tree(self, name: str) -> dict:
        return self.http.get_json(f"/api/v1/applications/{name}/resource-tree") or {"nodes": []}

    def _fetch_live(self, app: str, node: dict) -> dict:
        params = {"resourceName": node["name"], "namespace": node.get("namespace") or "", "kind": node["kind"],
                  "group": node.get("group") or "", "version": node.get("version") or "v1"}
        data = self.http.get_json(f"/api/v1/applications/{app}/resource", params)
        manifest = data.get("manifest") if isinstance(data, dict) else None
        return json.loads(manifest) if isinstance(manifest, str) else (manifest or {})

    def _fetch_events(self, app: str, node: dict | None) -> list[dict]:
        params = None
        if node is not None:
            params = {"resourceName": node["name"], "resourceNamespace": node.get("namespace") or "",
                      "resourceUID": node.get("uid") or ""}
        return (self.http.get_json(f"/api/v1/applications/{app}/events", params) or {}).get("items") or []

    def _fetch_logs(self, app: str, pod: dict, container: str, tail: int, previous: bool) -> str:
        md = pod["metadata"]
        params = {"namespace": md["namespace"], "podName": md["name"], "container": container,
                  "tailLines": tail, "previous": str(previous).lower(), "follow": "false"}
        lines: list[str] = []
        for msg in self.http.iter_json_lines(f"/api/v1/applications/{app}/logs", params, max_lines=tail + 10):
            if "error" in msg:
                raise RuntimeError(f"Argo CD log stream error: {msg['error'].get('message', msg['error'])}")
            res = msg.get("result", {}) or {}
            if res.get("last"):
                break
            content = res.get("content", "")
            ts = res.get("timeStampStr") or ""
            lines.append(f"{ts} {content}".strip())
        return "\n".join(lines)

    def access_report(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode}
        try:
            ui = self.http.get_json("/api/v1/session/userinfo")
            info["identity"] = f"Argo CD user '{ui.get('username')}' (loggedIn={ui.get('loggedIn')}, iss={ui.get('iss')})"
        except Exception as exc:  # noqa: BLE001
            info["identity"] = f"unknown ({str(exc)[:80]})"
        try:
            info["server_version"] = (self.http.get_json("/api/version") or {}).get("Version")
        except Exception:  # noqa: BLE001
            pass
        perms: dict[str, Any] = {}
        for res, act in CAN_I:
            try:
                obj = CAN_I_OBJECT.get(res, "*").replace("/", "%2F")
                ans = self.http.get_json(f"/api/v1/account/can-i/{res}/{act.replace('/', '%2F')}/{obj}")
                perms[f"{act} {res}"] = ans.get("value") == "yes"
            except Exception as exc:  # noqa: BLE001
                perms[f"{act} {res}"] = f"unknown ({str(exc)[:60]})"
        reads = {k: v for k, v in perms.items() if tuple(reversed(k.split(" ", 1))) in READ_ACTIONS}
        writes = {k: v for k, v in perms.items() if k not in reads}
        warnings = []
        if any(v is True for v in writes.values()):
            warnings.append("The Argo CD account can " + ", ".join(k for k, v in writes.items() if v is True) +
                            ". Bind it to role:readonly only (deploy/argocd-readonly-account). "
                            "The agent stays read-only regardless: only allow-listed GET endpoints are called.")
        missing = [k for k, v in reads.items() if v is not True]
        if missing:
            warnings.append("Missing Argo CD permissions: " + ", ".join(missing))
        info.update(read_permissions=reads, write_or_sensitive_permissions=writes, warnings=warnings)
        return info
