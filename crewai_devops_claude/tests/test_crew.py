"""Crew wiring, YAML interpolation, tools via the BaseTool interface and guardrails (no LLM calls)."""

import json
import re
from types import SimpleNamespace

import pytest

crewai = pytest.importorskip("crewai")

from k8s_capacity import runtime  # noqa: E402
from k8s_capacity.main import build_inputs, execute, load_config  # noqa: E402
from k8s_capacity.models import AdmissionNarrative, AuditReport, CheckExplanation  # noqa: E402
from k8s_capacity.report import admission_guardrail  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CREWAI_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.chdir(tmp_path)
    yield


def _cfg(mode, root):
    path = root / "examples" / f"run_{mode}.yaml"
    cfg = load_config(str(path), mode, snapshot_file=str(root / "tests" / "fixtures" / "demo_cluster.json"))
    return cfg


@pytest.fixture()
def root():
    from pathlib import Path
    return Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode,tasks", [
    ("audit", ["audit_capacity_baseline", "audit_rollout_reserve", "audit_node_failure", "audit_report"]),
    ("admission", ["admission_baseline", "admission_simulation", "admission_verdict"]),
])
def test_crew_builds_and_interpolates(mode, tasks, root, tmp_path):
    from k8s_capacity.crew import CapacityCrew

    cfg = _cfg(mode, root)
    runtime.init_run(cfg, tmp_path / "runs")
    crew = CapacityCrew(mode=mode).crew()
    assert [t.name for t in crew.tasks] == tasks
    crew._interpolate_inputs(build_inputs(cfg))
    for t in crew.tasks:
        assert not re.findall(r"\{[A-Za-z_]+\}", t.description + t.expected_output), t.name
    assert crew.tasks[-1].output_pydantic is not None


def test_no_llm_end_to_end(root, tmp_path):
    run = execute(_cfg("audit", root), use_llm=False, out_base=str(tmp_path / "runs"))
    rep = AuditReport.model_validate_json((run / "report.json").read_text())
    assert rep.computed.n_minus_1["overall"] == "DEGRADED"
    assert "## Риски" in (run / "report.md").read_text()


def test_admission_guardrail_blocks_verdict_override(root, tmp_path):
    from k8s_capacity.tools.capacity_tools import AdmissionSimulatorTool

    runtime.init_run(_cfg("admission", root), tmp_path / "runs")
    computed = json.loads(AdmissionSimulatorTool().run())
    checks = [CheckExplanation(check=c["check"], status=c["status"], explanation="...") for c in computed["checks"]]
    wrong = AdmissionNarrative(verdict="FITS", verdict_rationale="x", checks=checks, confidence="high", assumptions=[])
    ok, msg = admission_guardrail(SimpleNamespace(pydantic=wrong))
    assert not ok and "computed verdict" in msg
    right = wrong.model_copy(update={"verdict": computed["verdict"]})
    ok, _ = admission_guardrail(SimpleNamespace(pydantic=right))
    assert ok
