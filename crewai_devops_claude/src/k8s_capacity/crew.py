"""Kubernetes capacity crew: one CrewBase class, two modes (audit / admission).

Process.sequential, not hierarchical:
  * the data flow is a fixed pipeline (snapshot -> capacity -> rollout/N-1 -> verdict); a manager
    LLM would only add a non-deterministic routing step and extra tokens;
  * every number comes from tools, so there is nothing for a manager to arbitrate;
  * task `context` passes the previous outputs explicitly, which keeps the trace auditable.
"""

from __future__ import annotations

from crewai import LLM, Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from .models import AdmissionNarrative, AuditNarrative
from .report import admission_guardrail, audit_guardrail
from .tools.capacity_tools import (
    AdmissionSimulatorTool,
    AuditRiskTool,
    CapacityModelTool,
    CollectClusterSnapshotTool,
    NodeFailureTool,
    PlacementSimulatorTool,
    RemediationSearchTool,
    RolloutReserveTool,
)

MODE_TASKS = {
    "audit": ["audit_capacity_baseline", "audit_rollout_reserve", "audit_node_failure", "audit_report"],
    "admission": ["admission_baseline", "admission_simulation", "admission_verdict"],
}


def _llm(env_var: str) -> LLM | None:
    """Crew-wide LLM profile (sre_common.llm): SRE_LLM_PROFILE, or per role SRE_LLM_PROFILE_K8S_CAPACITY_ANALYST /
    SRE_LLM_PROFILE_K8S_CAPACITY_LEAD. Without a profile the legacy variables still work (CREWAI_LLM_ANALYST /
    CREWAI_LLM_LEAD, then CREWAI_LLM, endpoint from OPENAI_API_BASE). None -> CrewAI default model."""
    from sre_common.llm import build_llm

    role = env_var.removeprefix("CREWAI_LLM_").lower() or "default"
    return build_llm(f"k8s_capacity_{role}", legacy_model_vars=(env_var, "CREWAI_LLM"), default_temperature=0.1)


def _agent_kwargs(env_var: str) -> dict:
    llm = _llm(env_var)
    return {"llm": llm} if llm is not None else {}


@CrewBase
class CapacityCrew:
    """Read-only capacity analysis of a Kubernetes cluster."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, mode: str = "audit") -> None:
        if mode not in MODE_TASKS:
            raise ValueError(f"mode must be one of {list(MODE_TASKS)}")
        self.mode = mode

    # ------------------------------------------------------------------ agents
    @agent
    def capacity_modeler(self) -> Agent:
        return Agent(
            config=self.agents_config["capacity_modeler"],  # type: ignore[index]
            tools=[CollectClusterSnapshotTool(), CapacityModelTool(), PlacementSimulatorTool()],
            **_agent_kwargs("CREWAI_LLM_ANALYST"),
        )

    @agent
    def resilience_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["resilience_analyst"],  # type: ignore[index]
            tools=[RolloutReserveTool(), NodeFailureTool(), AdmissionSimulatorTool(), PlacementSimulatorTool()],
            **_agent_kwargs("CREWAI_LLM_ANALYST"),
        )

    @agent
    def capacity_lead(self) -> Agent:
        return Agent(
            config=self.agents_config["capacity_lead"],  # type: ignore[index]
            tools=[AuditRiskTool(), RemediationSearchTool(), PlacementSimulatorTool()],
            **_agent_kwargs("CREWAI_LLM_LEAD"),
        )

    # ------------------------------------------------------------------ audit tasks
    @task
    def audit_capacity_baseline(self) -> Task:
        return Task(config=self.tasks_config["audit_capacity_baseline"])  # type: ignore[index]

    @task
    def audit_rollout_reserve(self) -> Task:
        return Task(config=self.tasks_config["audit_rollout_reserve"])  # type: ignore[index]

    @task
    def audit_node_failure(self) -> Task:
        return Task(config=self.tasks_config["audit_node_failure"])  # type: ignore[index]

    @task
    def audit_report(self) -> Task:
        return Task(
            config=self.tasks_config["audit_report"],  # type: ignore[index]
            output_pydantic=AuditNarrative,
            guardrail=audit_guardrail,
            guardrail_max_retries=2,
        )

    # ------------------------------------------------------------------ admission tasks
    @task
    def admission_baseline(self) -> Task:
        return Task(config=self.tasks_config["admission_baseline"])  # type: ignore[index]

    @task
    def admission_simulation(self) -> Task:
        return Task(config=self.tasks_config["admission_simulation"])  # type: ignore[index]

    @task
    def admission_verdict(self) -> Task:
        return Task(
            config=self.tasks_config["admission_verdict"],  # type: ignore[index]
            output_pydantic=AdmissionNarrative,
            guardrail=admission_guardrail,
            guardrail_max_retries=2,
        )

    # ------------------------------------------------------------------ crew
    @crew
    def crew(self) -> Crew:
        wanted = MODE_TASKS[self.mode]
        tasks = sorted((t for t in self.tasks if t.name in wanted), key=lambda t: wanted.index(t.name))
        roles = {t.agent.role for t in tasks if t.agent}
        return Crew(
            name=f"k8s-capacity-{self.mode}",
            agents=[a for a in self.agents if a.role in roles],
            tasks=tasks,
            process=Process.sequential,
            verbose=True,
        )
