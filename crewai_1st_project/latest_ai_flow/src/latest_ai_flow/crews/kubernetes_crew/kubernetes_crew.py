import os

from crewai import Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from latest_ai_flow.tools.kubernetes_tools import (
    get_events,
    get_pod_details,
    list_nodes,
    list_pods,
)


@CrewBase
class KubernetesCrew:
    """Crew for investigating Kubernetes incidents."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def sre(self) -> Agent:
        return Agent(
            config=self.agents_config["sre"],
            tools=[
                list_pods,
                get_pod_details,
                get_events,
                list_nodes,
            ],
            llm=os.getenv(
                "CREWAI_LLM",
                "openai/gpt-4o-mini",
            ),
            verbose=True,
        )

    @task
    def investigate_incident(self) -> Task:
        return Task(
            config=self.tasks_config["investigate_incident"],
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )