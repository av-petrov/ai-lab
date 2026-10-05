#!/usr/bin/env python

import os

from pydantic import BaseModel

from crewai import Agent
from crewai.tools import tool
from crewai.flow import Flow, listen, start
from kubernetes import client, config


def get_api():
    config.load_kube_config()
    return client.CoreV1Api()


@tool("List pods")
def list_pods(namespace: str) -> str:
    """List pods and their current state in a Kubernetes namespace."""

    api = get_api()
    pods = api.list_namespaced_pod(namespace).items

    if not pods:
        return f"No pods found in namespace {namespace}"

    result = []

    for pod in pods:
        result.append(
            "\n".join(
                [
                    f"Name: {pod.metadata.name}",
                    f"Phase: {pod.status.phase}",
                    f"Node: {pod.spec.node_name or 'not scheduled'}",
                ]
            )
        )

    return "\n\n".join(result)


@tool("Get pod details")
def get_pod_details(namespace: str, pod_name: str) -> str:
    """Get scheduling state, conditions and resource requests for a pod."""

    api = get_api()

    pod = api.read_namespaced_pod(
        name=pod_name,
        namespace=namespace,
    )

    output = [
        f"Name: {pod.metadata.name}",
        f"Namespace: {pod.metadata.namespace}",
        f"Phase: {pod.status.phase}",
        f"Node: {pod.spec.node_name or 'not scheduled'}",
        "",
        "Containers:",
    ]

    for container in pod.spec.containers:
        requests = container.resources.requests or {}
        limits = container.resources.limits or {}

        output.extend(
            [
                f"- {container.name}",
                f"  CPU request: {requests.get('cpu', 'none')}",
                f"  Memory request: {requests.get('memory', 'none')}",
                f"  CPU limit: {limits.get('cpu', 'none')}",
                f"  Memory limit: {limits.get('memory', 'none')}",
            ]
        )

    output.append("")
    output.append("Conditions:")

    for condition in pod.status.conditions or []:
        output.append(
            f"- {condition.type}: {condition.status}; "
            f"reason={condition.reason or '-'}; "
            f"message={condition.message or '-'}"
        )

    return "\n".join(output)


@tool("Get Kubernetes events")
def get_events(namespace: str) -> str:
    """Get recent Kubernetes events for incident investigation."""

    api = get_api()
    events = api.list_namespaced_event(namespace).items

    if not events:
        return f"No events found in namespace {namespace}"

    def event_time(event):
        return (
            event.last_timestamp
            or event.event_time
            or event.metadata.creation_timestamp
        )

    events = sorted(events, key=event_time)

    result = []

    for event in events[-20:]:
        result.append(
            "\n".join(
                [
                    f"Object: {event.involved_object.kind}/{event.involved_object.name}",
                    f"Type: {event.type}",
                    f"Reason: {event.reason}",
                    f"Message: {event.message}",
                ]
            )
        )

    return "\n\n".join(result)


@tool("List Kubernetes nodes")
def list_nodes() -> str:
    """List nodes with readiness and allocatable compute resources."""

    api = get_api()
    nodes = api.list_node().items

    result = []

    for node in nodes:
        ready = "Unknown"

        for condition in node.status.conditions or []:
            if condition.type == "Ready":
                ready = condition.status

        allocatable = node.status.allocatable or {}

        result.append(
            "\n".join(
                [
                    f"Name: {node.metadata.name}",
                    f"Ready: {ready}",
                    f"CPU allocatable: {allocatable.get('cpu', 'unknown')}",
                    f"Memory allocatable: {allocatable.get('memory', 'unknown')}",
                ]
            )
        )

    return "\n\n".join(result)


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
        sre = Agent(
            role="Senior Kubernetes SRE",
            goal=(
                "Diagnose Kubernetes incidents using evidence "
                "collected directly from the cluster."
            ),
            backstory=(
                "You are an experienced Kubernetes SRE. "
                "You investigate incidents systematically and "
                "separate observed facts from hypotheses. "
                "You prefer gathering evidence through available tools "
                "instead of guessing."
            ),
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

        result = sre.kickoff(
            self.state.incident
            + """

Produce a concise incident report with:

1. Observed symptom
2. Evidence collected
3. Root cause
4. Why the workload cannot start
5. Recommended remediation

Explicitly distinguish facts observed through tools from your conclusions.
"""
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