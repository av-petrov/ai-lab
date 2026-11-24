"""Runtime context shared by the tools of one agent: backend, metrics, and a data-source ledger.

The ledger is filled by *code* (every tool call records which source it used or why a source was
unavailable), and ``finalize_report`` merges it into the report - so ``data_sources_used`` and
``limitations`` are facts, not something the LLM has to remember.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .backends import ArgoCDApiBackend, Backend, KubeBackend
from .kube_client import KubeReader
from .metrics import MetricsService
from .models import ArgoCDHealthReport
from .settings import ObservabilitySettings

log = logging.getLogger(__name__)


@dataclass
class SourceLedger:
    used: dict[str, int] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)
    seen_apps: set[str] = field(default_factory=set)
    app_facts: dict[str, dict] = field(default_factory=dict)  # name -> summarize_application() output
    outputs: list[str] = field(default_factory=list)  # tool outputs as the LLM saw them (evidence check)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def use(self, source: str) -> None:
        with self._lock:
            self.used[source] = self.used.get(source, 0) + 1

    def missing(self, source: str, reason: str) -> None:
        if reason.startswith("skipped"):  # not needed (a better source is used): not a limitation
            return
        with self._lock:
            self.unavailable.setdefault(source, reason)

    def record_output(self, text: str, cap: int = 400) -> None:
        with self._lock:
            if len(self.outputs) < cap:
                self.outputs.append(text)

    def refuse(self, msg: str) -> None:
        with self._lock:
            self.refusals.append(msg)


@dataclass
class ObservabilityContext:
    settings: ObservabilitySettings
    backend: Backend
    kube: KubeReader | None
    metrics: MetricsService
    ledger: SourceLedger = field(default_factory=SourceLedger)
    startup_warnings: list[str] = field(default_factory=list)
    _access: dict[str, Any] | None = None

    @property
    def mode(self) -> str:
        return self.backend.mode

    @property
    def primary_source(self) -> str:
        return "argocd-api" if self.mode == "argocd" else "kube-api"

    def access_report(self) -> dict[str, Any]:
        if self._access is None:
            rep = self.backend.access_report()
            rep["backend"] = self.backend.describe()
            det = self.metrics.detect()
            rep["metrics_sources"] = {k: {"available": v.available, "detail": v.detail} for k, v in det.items()}
            for k, v in det.items():
                if not v.available:
                    self.ledger.missing(k, v.detail)
            rep["warnings"] = self.startup_warnings + rep.get("warnings", [])
            self._access = rep
        return self._access


def _load_kube(settings: ObservabilitySettings) -> KubeReader:
    return KubeReader.from_settings(settings.kubeconfig, settings.kube_context, settings.http_timeout,
                                    settings.max_response_bytes)


def create_context(settings: ObservabilitySettings | None = None) -> ObservabilityContext:
    settings = settings or ObservabilitySettings()
    mode = settings.resolved_mode()
    warnings: list[str] = []
    kube: KubeReader | None = None
    if mode == "argocd":
        if not settings.has_argocd_api:
            raise ValueError("auth mode 'argocd' needs ARGOCD_SERVER and ARGOCD_AUTH_TOKEN")
        backend: Backend = ArgoCDApiBackend(
            settings.argocd_base_url or "", settings.argocd_auth_token.get_secret_value(),  # type: ignore[union-attr]
            verify=not settings.argocd_insecure, timeout=settings.http_timeout,
            max_bytes=settings.max_response_bytes)
        if settings.scrape_argocd_metrics:
            try:  # optional: only used for the /metrics service proxy and metrics-server
                kube = _load_kube(settings)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"No Kubernetes API access for metrics ({str(exc)[:100]})")
    else:
        kube = _load_kube(settings)
        backend = KubeBackend(kube, settings.argocd_namespace)
        if settings.auth_mode == "auto":
            warnings.append("auth mode auto: ARGOCD_SERVER/ARGOCD_AUTH_TOKEN not set, using the kube current-context")
    metrics = MetricsService(settings, kube=kube)
    return ObservabilityContext(settings=settings, backend=backend, kube=kube, metrics=metrics,
                                startup_warnings=warnings)


def finalize_report(report: ArgoCDHealthReport, ctx: ObservabilityContext, scope: str | None = None) -> ArgoCDHealthReport:
    """Overwrite code-owned fields and merge the ledger into the LLM's report."""
    report.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report.auth_mode = f"{ctx.mode} ({ctx.backend.describe()})"
    if scope:
        report.scope = scope
    # code-owned facts: status fields come from what the tools actually returned, not from the LLM
    from .backends.base import verdict_for

    for a in report.apps:
        f = ctx.ledger.app_facts.get(a.app_name)
        if not f:
            continue
        a.sync_status, a.health_status = f["sync_status"], f["health_status"]
        a.project = f.get("project") or a.project
        a.destination_namespace = f.get("destination_namespace") or a.destination_namespace
        a.revision = f.get("revision") or a.revision
        a.operation_phase = (f.get("last_operation") or {}).get("phase") or None
        a.verdict = verdict_for(a.sync_status, a.health_status)  # type: ignore[assignment]
    report.data_sources_used = [f"{k} ({v} calls)" for k, v in sorted(ctx.ledger.used.items())]
    lim = [f"{k} unavailable: {v}" for k, v in sorted(ctx.ledger.unavailable.items())]
    report.limitations = lim + [x for x in report.limitations if x not in lim]
    if ctx.ledger.refusals:
        report.limitations.append(f"{len(ctx.ledger.refusals)} request(s) refused by the read-only guard")
    for w in ctx.startup_warnings:
        if w not in report.limitations:
            report.limitations.append(w)
    return report
