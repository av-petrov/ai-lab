"""CrewAI wiring: agent factory, task factories, composition into another crew, guardrail, report."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

crewai = pytest.importorskip("crewai")

from crewai import Agent, Crew, Process, Task  # noqa: E402

from argocd_observability import (  # noqa: E402
    ArgoCDHealthReport,
    agent_context,
    build_argocd_observability_agent,
    diagnose_app_task,
    finalize_report,
    health_overview_task,
    render_markdown,
)
from argocd_observability.models import AppDiagnosis, Evidence  # noqa: E402
from argocd_observability.report import Scope, make_guardrail, unverified_evidence  # noqa: E402
from argocd_observability.triage import run_triage  # noqa: E402

from .conftest import argocd_backend, make_ctx  # noqa: E402


@pytest.fixture()
def ctx(broken_cluster_routes):
    backend, _ = argocd_backend(broken_cluster_routes)
    return make_ctx(backend)


def test_agent_factory(ctx):
    agent = build_argocd_observability_agent(context=ctx)
    assert isinstance(agent, Agent)
    assert agent.allow_delegation is False
    assert agent.max_iter == ctx.settings.max_iter
    assert {t.name for t in agent.tools} == {
        "argocd_access_check", "argocd_list_applications", "argocd_get_application", "argocd_resource_tree",
        "k8s_events", "argocd_live_resource_status", "pod_logs", "argocd_metrics"}
    assert "read-only" in agent.backstory
    assert agent_context(agent) is ctx
    # every tool has a typed schema and a real description
    for t in agent.tools:
        assert t.args_schema is not None and len(t.description) > 60


def test_task_factories(ctx):
    agent = build_argocd_observability_agent(context=ctx)
    t1 = diagnose_app_task(agent, "demo-cl")
    t2 = health_overview_task(agent, Scope(project="apps"))
    t3 = health_overview_task(agent, ["a", "b"])
    for t in (t1, t2, t3):
        assert t.output_pydantic is ArgoCDHealthReport
        assert "[[" not in t.description and "{" not in t.description
    assert '"demo-cl"' in t1.description
    assert "project apps" in t2.description and 'project="apps"' in t2.description
    assert 'names=["a", "b"]' in t3.description


def test_agent_plugs_into_another_crew(ctx):
    """The SRE crew can add the observer and a downstream agent that consumes its report."""
    observer = build_argocd_observability_agent(context=ctx)
    triage = Agent(role="Incident triage", goal="Prioritise incidents", backstory="On-call lead.",
                   allow_delegation=False)
    observe = health_overview_task(observer, "all")
    prioritise = Task(description="Rank the unhealthy apps from the Argo CD report by user impact.",
                      expected_output="A ranked list.", agent=triage, context=[observe])
    crew = Crew(agents=[observer, triage], tasks=[observe, prioritise], process=Process.sequential)
    assert crew.tasks[1].context == [observe]
    assert len(crew.agents) == 2


def _report(**app_kw):
    base = dict(app_name="demo-cl", sync_status="Synced", health_status="Degraded", verdict="degraded",
                probable_root_cause="crash", root_cause_category="crash_loop", confidence="high",
                evidence=[Evidence(source="container_log", reference="Pod demo-cl/web-abc-2 logs",
                                   detail="FATAL: required environment variable REDIS_URL is not set")])
    base.update(app_kw)
    return ArgoCDHealthReport(scope="app demo-cl", summary="s", apps=[AppDiagnosis(**base)])


def test_guardrail(ctx):
    ctx.ledger.seen_apps.update({"demo-cl"})
    g = make_guardrail(ctx, ["demo-cl"])
    ok, _ = g(SimpleNamespace(pydantic=_report()))
    assert ok
    ok, msg = g(SimpleNamespace(pydantic=_report(evidence=[])))
    assert not ok and "evidence" in msg
    ok, msg = g(SimpleNamespace(pydantic=_report(probable_root_cause=None)))
    assert not ok and "probable_root_cause" in msg
    ok, msg = g(SimpleNamespace(pydantic=_report(app_name="invented-app")))
    assert not ok and "invented-app" in msg
    ok, msg = g(SimpleNamespace(pydantic=None))
    assert not ok


def test_evidence_verification():
    rep = _report()
    assert unverified_evidence(rep, ['{"lines":"2026 FATAL: required environment variable REDIS_URL is not set"}']) == []
    assert unverified_evidence(rep, ['{"lines":"something else"}'])


def test_finalize_and_markdown(ctx):
    rep = run_triage(ctx, Scope(all=True))
    rep.data_sources_used = ["argocd-api via LLM"]
    rep = finalize_report(rep, ctx, "all applications")
    assert rep.auth_mode.startswith("argocd")
    assert rep.data_sources_used[0].startswith("argocd-api (")
    assert any(lim.startswith("prometheus unavailable") for lim in rep.limitations)
    md = render_markdown(rep)
    assert "## demo-ip" in md and "## demo-cl" in md and "Suggested next steps** (not executed)" in md
    assert "REDIS_URL is not set" in md
    # round-trips as JSON for downstream agents
    assert ArgoCDHealthReport.model_validate_json(rep.model_dump_json()).apps[0].app_name


def test_cli_check_and_no_llm(monkeypatch, capsys, broken_cluster_routes, tmp_path):
    from argocd_observability import cli

    backend, _ = argocd_backend(broken_cluster_routes)
    monkeypatch.setattr("argocd_observability.context.create_context", lambda s=None: make_ctx(backend))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--check"]) == 0
    assert '"read_permissions"' in capsys.readouterr().out
    rc = cli.main(["--app", "demo-cl", "--app", "good", "--no-llm", "--out-dir", str(tmp_path / "out")])
    assert rc == 1  # demo-cl needs attention
    out = capsys.readouterr().out
    assert "crash_loop" in out
    assert (tmp_path / "out" / "report.json").exists() and (tmp_path / "out" / "report.md").exists()


def test_finalize_overrides_llm_status_with_tool_facts(ctx):
    from argocd_observability.tools import build_tools

    tools = {t.name: t for t in build_tools(ctx)}
    tools["argocd_get_application"].run(app_name="never")
    rep = ArgoCDHealthReport(scope="x", summary="s", apps=[AppDiagnosis(
        app_name="never", sync_status="Synced", health_status="Degraded", verdict="degraded",
        probable_root_cause="?", root_cause_category="unknown")])
    rep = finalize_report(rep, ctx)
    a = rep.apps[0]
    assert (a.sync_status, a.health_status, a.verdict) == ("OutOfSync", "Missing", "missing")
    assert rep.data_sources_used == ["argocd-api (1 calls)"]


def _cli_env(monkeypatch, tmp_path, routes):
    from argocd_observability import cli

    backend, _ = argocd_backend(routes)
    ctx = make_ctx(backend)
    monkeypatch.setattr("argocd_observability.context.create_context", lambda s=None: ctx)

    def no_llm(*a, **k):
        raise AssertionError("the LLM crew must not run")

    monkeypatch.setattr("argocd_observability.crew.run_observability", no_llm)
    monkeypatch.chdir(tmp_path)
    return cli


def test_cli_fails_fast_when_no_requested_app_exists(monkeypatch, capsys, tmp_path, broken_cluster_routes):
    cli = _cli_env(monkeypatch, tmp_path, broken_cluster_routes)
    assert cli.main(["--app", "obs-demo-imagepull", "--app", "typo", "--no-save"]) == 2
    err = capsys.readouterr().err
    assert "none of the requested applications exist" in err and "obs-demo-imagepull" in err
    assert "existing: demo-cl" in err


def test_cli_fails_fast_on_empty_project(monkeypatch, capsys, tmp_path, broken_cluster_routes):
    broken_cluster_routes["/api/v1/applications"] = lambda p: {"items": []} if p.get("projects") else {"items": []}
    cli = _cli_env(monkeypatch, tmp_path, broken_cluster_routes)
    assert cli.main(["--all", "--project", "nope", "--no-save"]) == 2
    assert "no applications found in project 'nope'" in capsys.readouterr().err


def test_cli_skips_missing_apps_and_reports_them(monkeypatch, capsys, tmp_path, broken_cluster_routes):
    cli = _cli_env(monkeypatch, tmp_path, broken_cluster_routes)
    rc = cli.main(["--app", "demo-cl", "--app", "typo", "--no-llm", "--format", "json", "--no-save"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "skipped: typo" in captured.err
    rep = ArgoCDHealthReport.model_validate_json(captured.out)
    assert [a.app_name for a in rep.apps] == ["demo-cl"]
    assert any("not found and skipped: typo" in lim for lim in rep.limitations)
