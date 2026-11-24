"""Read-only Kubernetes access.

``ReadOnlyApiClient`` subclasses the official ``kubernetes.client.ApiClient`` and overrides
``request()`` - the single method every typed or raw API call funnels through - so the guard runs
for *any* code path that uses this client, including the generated ``CoreV1Api`` etc.

``KubeReader`` is the small surface the backends actually use (raw paths -> JSON/text).
Kubeconfig is loaded with ``persist_config=False``; the current context is used unless an explicit
override is configured. Nothing is written back to the kubeconfig.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from .guard import check_kube_request


class ReadOnlyApiClient(k8s_client.ApiClient):
    def request(self, method, url, query_params=None, headers=None, post_params=None, body=None,
                _preload_content=True, _request_timeout=None):
        check_kube_request(method, urlsplit(url).path, query_params)
        return super().request(method, url, query_params=query_params, headers=headers,
                               post_params=post_params, body=body, _preload_content=_preload_content,
                               _request_timeout=_request_timeout)


class KubeReader:
    def __init__(self, api_client: k8s_client.ApiClient, *, context_name: str | None = None,
                 timeout: float = 15.0, max_bytes: int = 20_000_000) -> None:
        if not isinstance(api_client, ReadOnlyApiClient):
            raise TypeError("KubeReader requires a ReadOnlyApiClient")
        self.api = api_client
        self.context_name = context_name
        self.timeout = timeout
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls, kubeconfig: str | None, context: str | None, timeout: float,
                      max_bytes: int) -> "KubeReader":
        cfg = k8s_client.Configuration()
        ctx_name: str | None
        use_incluster = (not kubeconfig and not context and os.environ.get("KUBERNETES_SERVICE_HOST")
                         and not os.path.exists(os.path.expanduser(os.environ.get("KUBECONFIG", "~/.kube/config"))))
        if use_incluster:
            k8s_config.load_incluster_config(client_configuration=cfg)
            ctx_name = "in-cluster"
        else:
            k8s_config.load_kube_config(config_file=kubeconfig, context=context, client_configuration=cfg,
                                        persist_config=False)
            if context:
                ctx_name = context
            else:
                _, active = k8s_config.list_kube_config_contexts(config_file=kubeconfig)
                ctx_name = (active or {}).get("name")
        cfg.retries = 0  # fail fast; callers report unavailable sources
        return cls(ReadOnlyApiClient(configuration=cfg), context_name=ctx_name, timeout=timeout, max_bytes=max_bytes)

    # ------------------------------------------------------------------ raw calls
    def _call(self, method: str, path: str, query: dict[str, Any] | None = None, body: Any = None,
              timeout: float | None = None) -> bytes:
        q = [(k, v) for k, v in (query or {}).items() if v is not None]
        resp = self.api.call_api(
            path, method, query_params=q, header_params={"Accept": "application/json, */*",
                                                         "Content-Type": "application/json"},
            body=body, auth_settings=["BearerToken"], _preload_content=False,
            _return_http_data_only=True, _request_timeout=timeout or self.timeout)
        data = resp.data if hasattr(resp, "data") else resp
        if isinstance(data, str):
            data = data.encode()
        if len(data) > self.max_bytes:
            raise RuntimeError(f"response for {path} exceeds {self.max_bytes} bytes")
        return data

    def get_json(self, path: str, query: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return json.loads(self._call("GET", path, query, timeout=timeout) or b"null")

    def get_text(self, path: str, query: dict[str, Any] | None = None, timeout: float | None = None) -> str:
        return self._call("GET", path, query, timeout=timeout).decode("utf-8", errors="replace")

    def can_i(self, verb: str, resource: str, group: str = "", namespace: str | None = None,
              subresource: str | None = None) -> tuple[bool, str]:
        attrs: dict[str, Any] = {"verb": verb, "resource": resource, "group": group}
        if namespace:
            attrs["namespace"] = namespace
        if subresource:
            attrs["subresource"] = subresource
        body = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": attrs}}
        res = json.loads(self._call("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", body=body))
        st = res.get("status", {})
        return bool(st.get("allowed")), st.get("reason", "")
