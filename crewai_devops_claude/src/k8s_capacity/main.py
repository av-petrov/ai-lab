"""CLI entry points.

  k8s-capacity audit     --config examples/run_audit.yaml [--snapshot FILE] [--no-llm]
  k8s-capacity admission --config examples/run_admission.yaml [--spec FILE | --manifest FILE] [--no-llm]
  k8s-capacity collect   --config examples/run_audit.yaml --out snapshot.json
  k8s-capacity doctor    [--kube-context kind-dev01]   (checks .env, LLM, tool calling, cluster access)

`crewai run` calls `run()`, which reads CAPACITY_MODE (audit|admission) and CAPACITY_CONFIG.
--no-llm produces the same report.md/report.json from code only (no narrative), e.g. for CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from . import runtime
from .models import AdmissionSpec, RunConfig
from .report import build_admission_report, build_audit_report, render_admission_md, render_audit_md


def load_config(path: str | None, mode: str, **overrides) -> RunConfig:
    data = yaml.safe_load(Path(path).read_text()) if path else {}
    data = data or {}
    data["mode"] = mode
    for k, v in overrides.items():
        if v is not None:
            data[k] = v
    return RunConfig.model_validate(data)


def build_inputs(cfg: RunConfig) -> dict[str, str]:
    asc = cfg.autoscaler
    inputs = {
        "cluster_name": cfg.cluster_name,
        "environment_summary": (
            f"ipam mode {cfg.ipam.get('mode')}, pool label {cfg.pool_label or 'auto-detected'}, "
            f"scoring {cfg.scoring}, usage source {'prometheus' if cfg.prometheus_url else 'metrics-server or none'}"),
        "update_concurrency": cfg.update_concurrency,
        "require_n_minus_1": "mandatory" if cfg.require_n_minus_1 else "desirable",
        "autoscaler_summary": (f"{asc.kind}: " + ", ".join(
            f"{g.name} (pool {g.pool}, {g.current}/{g.max} nodes, ~{g.provisioning_minutes} min)" for g in asc.node_groups)
            if asc.enabled else "none, the node count is fixed"),
        "app_name": "", "namespace": "", "change_type": "", "replicas": "", "spec_summary": "",
    }
    if cfg.admission:
        s = cfg.admission
        inputs.update({
            "app_name": s.name, "namespace": s.namespace, "change_type": s.change_type,
            "replicas": str(s.replicas if s.replicas is not None else "unchanged"),
            "spec_summary": summarize_spec(s),
        })
    return inputs


def summarize_spec(s: AdmissionSpec) -> str:
    if s.manifest_yaml:
        return "full manifest provided in the run config (read by simulate_admission)"
    parts = [f"{s.kind}"]
    for c in s.containers:
        parts.append(f"container {c.name} requests " + ", ".join(f"{k} {v}" for k, v in c.requests.items())
                     + ("; limits " + ", ".join(f"{k} {v}" for k, v in c.limits.items()) if c.limits else ""))
    for c in s.init_containers:
        parts.append(f"{'sidecar' if c.sidecar else 'init'} {c.name} requests "
                     + ", ".join(f"{k} {v}" for k, v in c.requests.items()))
    if s.strategy:
        parts.append(f"strategy {s.strategy.type} maxSurge {s.strategy.max_surge} maxUnavailable {s.strategy.max_unavailable}")
    if s.node_selector:
        parts.append("nodeSelector " + ", ".join(f"{k}={v}" for k, v in s.node_selector.items()))
    if s.tolerations:
        parts.append(f"{len(s.tolerations)} toleration(s)")
    if s.affinity:
        parts.append("affinity: " + ", ".join(s.affinity.keys()))
    if s.topology_spread_constraints:
        parts.append(f"{len(s.topology_spread_constraints)} topologySpreadConstraint(s)")
    for p in s.pvcs:
        parts.append(f"PVC {p.name} {p.size} class {p.storage_class or 'default'} {','.join(p.access_modes)}")
    if s.priority_class_name:
        parts.append(f"priorityClass {s.priority_class_name}")
    return "; ".join(parts)


WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def admission_spec_from_args(config: str | None, spec_file: str | None, manifest: str | None,
                             change_type: str | None) -> dict:
    """AdmissionSpec dict: run config `admission:` < --spec file < --manifest; --change-type overrides."""
    base = yaml.safe_load(Path(config).read_text()) if config else {}
    spec = (base or {}).get("admission") or {}
    if spec_file:
        spec = yaml.safe_load(Path(spec_file).read_text())
    if manifest:
        text = Path(manifest).read_text()
        doc = next((d for d in yaml.safe_load_all(text) if d and d.get("kind") in WORKLOAD_KINDS), None)
        if doc is None:
            raise SystemExit(f"{manifest}: no {'/'.join(WORKLOAD_KINDS)} found")
        spec = {"name": doc["metadata"]["name"], "namespace": doc["metadata"].get("namespace", "default"),
                "kind": doc["kind"], "replicas": (doc.get("spec") or {}).get("replicas"),
                "change_type": spec.get("change_type", "new"), "manifest_yaml": text}
    if change_type:
        spec["change_type"] = change_type
    return spec


REPORTERS = {
    "audit": (build_audit_report, render_audit_md),
    "admission": (build_admission_report, render_admission_md),
}


def execute(cfg: RunConfig, use_llm: bool = True, out_base: str = "runs") -> Path:
    run = runtime.init_run(cfg, out_base)
    narrative = None
    if use_llm:
        from .crew import CapacityCrew

        result = CapacityCrew(mode=cfg.mode).crew().kickoff(inputs=build_inputs(cfg))
        narrative = result.pydantic
    else:
        if not runtime.snapshot_path().exists():
            from .tools.capacity_tools import CollectClusterSnapshotTool
            CollectClusterSnapshotTool()._run()
        if cfg.mode == "admission" and cfg.admission:
            from .tools.capacity_tools import AdmissionSimulatorTool, RemediationSearchTool
            if json.loads(AdmissionSimulatorTool()._run()).get("verdict") != "FITS":
                RemediationSearchTool()._run()
    build, render = REPORTERS[cfg.mode]
    rep = build(narrative)
    (run / "report.json").write_text(rep.model_dump_json(indent=2))
    (run / "report.md").write_text(render(rep))
    return run


def _load_env() -> None:
    """CrewAI does not read .env by itself: load it from the current directory (existing env wins)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env", override=False)


def doctor(kube_context: str | None) -> int:
    """Pre-flight checks before a full LLM run. Read-only, one tiny LLM call."""
    from .crew import _llm

    ok = True

    def line(status: str, what: str, detail: str = "") -> None:
        print(f"[{status:4}] {what}{(': ' + detail) if detail else ''}")

    # 1. env: crew-wide LLM profiles (sre_common.llm), legacy CREWAI_LLM* when no profile is set
    from sre_common.llm import defined_profiles, load_env, resolve_profile

    env = load_env()
    line("INFO", "SRE_LLM_PROFILE", env.get("SRE_LLM_PROFILE") or "not set (legacy CREWAI_LLM* variables)")
    if defined_profiles(env):
        line("INFO", "defined profiles", ", ".join(defined_profiles(env)))
    lead = None
    for var in ("CREWAI_LLM_ANALYST", "CREWAI_LLM_LEAD"):
        role = var.removeprefix("CREWAI_LLM_").lower()
        try:
            p = resolve_profile(f"k8s_capacity_{role}", legacy_model_vars=(var, "CREWAI_LLM"))
        except ValueError as exc:
            line("FAIL", f"LLM for {role}", str(exc))
            return 1
        line("OK" if p else "WARN", f"LLM for {role}", p.describe() if p else "not set -> CrewAI default model")
        lead = p if role == "lead" else lead
    key = (lead.api_key if lead else None) or os.getenv("OPENAI_API_KEY")
    line("OK" if key else "FAIL", "API key", "set" if key else "not set (profile _API_KEY or OPENAI_API_KEY)")
    ok &= bool(key)

    # 2. plain call
    llm = _llm("CREWAI_LLM_LEAD")
    if llm is None:
        from crewai import LLM
        llm = LLM(model="gpt-4o-mini")
    try:
        answer = llm.call("Reply with exactly: OK")
        line("OK" if "OK" in str(answer) else "WARN", f"LLM call ({llm.model})", str(answer)[:60])
    except Exception as exc:  # noqa: BLE001
        line("FAIL", f"LLM call ({llm.model})", f"{exc.__class__.__name__}: {str(exc)[:200]}")
        return 1

    # 3. tool calling (agents cannot work without it)
    try:
        supports = llm.supports_function_calling()
        tool = {"type": "function", "function": {
            "name": "add_cpu", "description": "Add two CPU amounts in millicores.",
            "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                           "required": ["a", "b"]}}}
        res = llm.call("Use the add_cpu tool to add 1500 and 2500 millicores.", tools=[tool],
                       available_functions={"add_cpu": lambda a, b: f"TOOL_RESULT={a + b}"})
        worked = "4000" in str(res)
        line("OK" if worked else "FAIL", "tool calling", f"supports={supports}, result={str(res)[:80]}")
        ok &= worked
    except Exception as exc:  # noqa: BLE001
        line("FAIL", "tool calling", f"{exc.__class__.__name__}: {str(exc)[:200]}")
        ok = False

    # 4. cluster (read-only)
    if kube_context:
        try:
            from kubernetes import client, config
            config.load_kube_config(context=kube_context)
            nodes = client.CoreV1Api().list_node().items
            line("OK", f"cluster {kube_context}", f"{len(nodes)} node(s) readable")
        except Exception as exc:  # noqa: BLE001
            line("FAIL", f"cluster {kube_context}", f"{exc.__class__.__name__}: {str(exc)[:200]}")
            ok = False
    print("ready for a full run" if ok else "fix the FAIL items first")
    return 0 if ok else 1


def cli(argv: list[str] | None = None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="k8s-capacity", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check .env, LLM, tool calling and cluster access")
    d.add_argument("--kube-context", help="also check read access to this kube context")
    for name in ("audit", "admission", "collect"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="run config YAML (RunConfig)")
        p.add_argument("--snapshot", help="offline: use this snapshot instead of the live cluster")
        p.add_argument("--no-llm", action="store_true", help="deterministic report only")
        p.add_argument("--runs-dir", default="runs")
        if name == "admission":
            p.add_argument("--spec", help="AdmissionSpec YAML/JSON")
            p.add_argument("--manifest", help="Kubernetes manifest YAML (Deployment/StatefulSet/DaemonSet [+PVC])")
            p.add_argument("--change-type", choices=["new", "resize", "scale"])
        if name == "collect":
            p.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "doctor":
        return doctor(a.kube_context)

    if a.cmd == "collect":
        from .tools.collector import collect_snapshot
        snap = collect_snapshot(**load_config(a.config, "audit").collector_kwargs())
        Path(a.out).write_text(json.dumps(snap, indent=1))
        print(f"snapshot written to {a.out}")
        return 0

    overrides = {"snapshot_file": a.snapshot}
    if a.cmd == "admission":
        overrides["admission"] = admission_spec_from_args(a.config, a.spec, a.manifest, a.change_type)
    cfg = load_config(a.config, a.cmd, **overrides)
    run = execute(cfg, use_llm=not a.no_llm, out_base=a.runs_dir)
    print(f"report: {run / 'report.md'}\njson:   {run / 'report.json'}")
    print((run / "report.md").read_text())
    return 0


def run() -> None:
    """Entry point for `crewai run`."""
    mode = os.getenv("CAPACITY_MODE", "audit")
    args = [mode, "--config", os.getenv("CAPACITY_CONFIG", f"examples/run_{mode}.yaml")]
    if os.getenv("CAPACITY_SNAPSHOT"):
        args += ["--snapshot", os.environ["CAPACITY_SNAPSHOT"]]
    sys.exit(cli(args))


if __name__ == "__main__":
    sys.exit(cli())
