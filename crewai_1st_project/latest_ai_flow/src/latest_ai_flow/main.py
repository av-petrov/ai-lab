#!/usr/bin/env python

import os

from pydantic import BaseModel

from latest_ai_flow.crews.kubernetes_crew.kubernetes_crew import (
    KubernetesCrew,
)

from crewai.flow import Flow, listen, start

from latest_ai_flow.tools.kubernetes_tools import (
    get_events,
    get_pod_details,
    list_nodes,
    list_pods,
)

class IncidentState(BaseModel):
    incident: str = ""
    analysis: str = ""


class KubernetesIncidentFlow(Flow[IncidentState]):

    @start()
    def receive_incident(self):
        self.state.incident = """
A workload in namespace crewai-demo is not starting.

Investigate the Kubernetes cluster and determine the root cause.

Use the Kubernetes tools to collect evidence.
Do not assume the cause before investigating.
"""

        print("\n=== INCIDENT ===")
        print(self.state.incident)

    @listen(receive_incident)
    def investigate(self):
        result = KubernetesCrew().crew().kickoff(
            inputs={
                "incident": self.state.incident,
            }
        )

        self.state.analysis = result.raw

        return result.raw

    @listen(investigate)
    def report(self, analysis):
        print("\n=== INCIDENT REPORT ===")
        print(analysis)

        return analysis


def kickoff():
    KubernetesIncidentFlow().kickoff()


def plot():
    KubernetesIncidentFlow().plot()


if __name__ == "__main__":
    kickoff()