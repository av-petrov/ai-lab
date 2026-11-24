"""Task factories. Each returns a ``crewai.Task`` with ``output_pydantic=ArgoCDHealthReport``.

Descriptions are rendered here (``[[placeholders]]``), so the tasks need no kickoff ``inputs`` and can
be mixed into any crew. Downstream tasks can use them as ``context=[...]`` and read
``task.output.pydantic`` (an ``ArgoCDHealthReport``).
"""

from __future__ import annotations

from typing import Any

from crewai import Agent, Task

from .agent import agent_context, load_yaml
from .models import ArgoCDHealthReport
from .report import Scope, make_guardrail


def _render(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace(f"[[{k}]]", v)
    return text.strip()


def _ctx_or_none(agent: Agent):
    try:
        return agent_context(agent)
    except ValueError:
        return None


def health_overview_task(agent: Agent, scope: Scope | str | list[str] | None = None, *,
                         context: list[Task] | None = None, **task_overrides: Any) -> Task:
    """Status of many apps (``scope``: Scope, project-less 'all', an app name, or a list of names)."""
    if scope is None or scope == "all":
        scope = Scope(all=True)
    elif isinstance(scope, str):
        scope = Scope(apps=[scope])
    elif isinstance(scope, list):
        scope = Scope(apps=scope)
    spec = load_yaml("tasks.yaml")["health_overview"]
    values = {"scope_description": scope.describe(), "list_hint": scope.list_hint()}
    return Task(
        name="argocd_health_overview",
        description=_render(spec["description"], values),
        expected_output=_render(spec["expected_output"], values),
        agent=agent,
        context=context,
        output_pydantic=ArgoCDHealthReport,
        guardrail=make_guardrail(_ctx_or_none(agent), scope.apps or None),
        guardrail_max_retries=2,
        **task_overrides,
    )


def diagnose_app_task(agent: Agent, app_name: str, *, context: list[Task] | None = None,
                      **task_overrides: Any) -> Task:
    """Deep diagnosis of one app."""
    spec = load_yaml("tasks.yaml")["diagnose_app"]
    values = {"app_name": app_name}
    return Task(
        name=f"argocd_diagnose_{app_name}",
        description=_render(spec["description"], values),
        expected_output=_render(spec["expected_output"], values),
        agent=agent,
        context=context,
        output_pydantic=ArgoCDHealthReport,
        guardrail=make_guardrail(_ctx_or_none(agent), [app_name]),
        guardrail_max_retries=2,
        **task_overrides,
    )
