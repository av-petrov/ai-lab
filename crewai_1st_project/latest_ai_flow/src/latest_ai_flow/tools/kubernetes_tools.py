from crewai.tools import tool
from kubernetes import client, config

from latest_ai_flow.sanitizer import pseudonymize


def get_api() -> client.CoreV1Api:
    config.load_kube_config()
    return client.CoreV1Api()


def anonymize_node_name(node_name: str | None) -> str:
    """Return a stable pseudonym for a Kubernetes node name."""

    if not node_name:
        return "not scheduled"

    return pseudonymize(node_name, "node")


def get_node_replacements(
    api: client.CoreV1Api,
) -> dict[str, str]:
    """Build real-node-name -> pseudonym mapping."""

    return {
        node.metadata.name: anonymize_node_name(node.metadata.name)
        for node in api.list_node().items
        if node.metadata.name
    }


def sanitize_node_references(
    value: str | None,
    replacements: dict[str, str],
) -> str:
    """Replace known Kubernetes node names inside free-form text."""

    if not value:
        return "-"

    sanitized = value

    # Longer values first in case one hostname is a prefix of another.
    for real_name in sorted(replacements, key=len, reverse=True):
        sanitized = sanitized.replace(
            real_name,
            replacements[real_name],
        )

    return sanitized


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
                    f"Node: {anonymize_node_name(pod.spec.node_name)}",
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

    node_replacements = get_node_replacements(api)

    output = [
        f"Name: {pod.metadata.name}",
        f"Namespace: {pod.metadata.namespace}",
        f"Phase: {pod.status.phase}",
        f"Node: {anonymize_node_name(pod.spec.node_name)}",
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
            f"message={sanitize_node_references(condition.message, node_replacements)}"
        )

    return "\n".join(output)


@tool("Get Kubernetes events")
def get_events(namespace: str) -> str:
    """Get recent Kubernetes events for incident investigation."""

    api = get_api()

    events = api.list_namespaced_event(namespace).items
    node_replacements = get_node_replacements(api)

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
        object_name = event.involved_object.name

        if event.involved_object.kind == "Node":
            object_name = anonymize_node_name(object_name)

        result.append(
            "\n".join(
                [
                    f"Object: {event.involved_object.kind}/{object_name}",
                    f"Type: {event.type}",
                    f"Reason: {event.reason}",
                    f"Message: "
                    f"{sanitize_node_references(event.message, node_replacements)}",
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
                    f"Name: {anonymize_node_name(node.metadata.name)}",
                    f"Ready: {ready}",
                    f"CPU allocatable: "
                    f"{allocatable.get('cpu', 'unknown')}",
                    f"Memory allocatable: "
                    f"{allocatable.get('memory', 'unknown')}",
                ]
            )
        )

    return "\n\n".join(result)