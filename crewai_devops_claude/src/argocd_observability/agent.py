"""Reusable agent factory: ``build_argocd_observability_agent(config) -> crewai.Agent``.

Drop the returned agent into any Crew. Its tools carry their own runtime context (backend, metrics,
data-source ledger); fetch it with :func:`agent_context` to finalize reports.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from crewai import LLM, Agent

from .context import ObservabilityContext, create_context
from .settings import ObservabilitySettings
from .tools import build_tools, tool_context

CONFIG_DIR = Path(__file__).parent / "config"


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text())


AGENT_NAME = "argocd_observability"   # per-agent LLM override: SRE_LLM_PROFILE_ARGOCD_OBSERVABILITY


def build_llm(settings: ObservabilitySettings) -> LLM | None:
    """The crew-wide LLM profile for this agent (sre_common.llm); None -> CrewAI's default model."""
    from sre_common.llm import build_llm as build_crew_llm

    return build_crew_llm(AGENT_NAME, settings.llm_profile, default_temperature=settings.temperature)


def build_argocd_observability_agent(
    config: ObservabilitySettings | None = None,
    *,
    context: ObservabilityContext | None = None,
    llm: Any = None,
    verbose: bool = False,
    **agent_overrides: Any,
) -> Agent:
    """Create the read-only Argo CD observer agent.

    Args:
        config: settings (default: from env / .env).
        context: pre-built runtime context (e.g. to share one backend between agents or for tests).
        llm: explicit LLM (overrides the crew-wide profile from sre_common.llm).
        agent_overrides: any other ``crewai.Agent`` field (e.g. ``max_iter``, ``step_callback``).
    """
    settings = config or (context.settings if context else ObservabilitySettings())
    ctx = context or create_context(settings)
    spec = load_yaml("agents.yaml")["argocd_observability"]
    kwargs: dict[str, Any] = dict(
        role=spec["role"].strip(),
        goal=spec["goal"].strip(),
        backstory=spec["backstory"].strip(),
        tools=build_tools(ctx),
        allow_delegation=False,
        max_iter=settings.max_iter,
        max_execution_time=settings.max_execution_time,
        respect_context_window=True,
        verbose=verbose,
    )
    chosen_llm = llm if llm is not None else build_llm(settings)
    if chosen_llm is not None:
        kwargs["llm"] = chosen_llm
    kwargs.update(agent_overrides)
    return Agent(**kwargs)


def agent_context(agent: Agent) -> ObservabilityContext:
    ctx = tool_context(list(agent.tools or []))
    if ctx is None:
        raise ValueError("agent was not built by build_argocd_observability_agent")
    return ctx
