"""A tiny fake Kubernetes MCP server for the external_tools federation smoke.

Stands in for a real kubernetes MCP. Exposes the tool names the
external_tools_k8s demo references, returning canned but plausible data
so an agent can walk an incident. Run as a stdio MCP server.
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("kubernetes")


@mcp.tool
def list_pods(namespace: str = "default") -> dict:
    """List pods in a namespace with their status."""
    return {
        "namespace": namespace,
        "pods": [
            {"name": "checkout-api-7f9c-aa", "status": "CrashLoopBackOff", "restarts": 12},
            {"name": "checkout-api-7f9c-bb", "status": "CrashLoopBackOff", "restarts": 11},
            {"name": "checkout-api-7f9c-cc", "status": "Running", "restarts": 0},
            {"name": "payments-api-55d-aa", "status": "Running", "restarts": 0},
        ],
    }


@mcp.tool
def list_events(namespace: str = "default") -> dict:
    """Recent cluster events."""
    return {
        "events": [
            {
                "type": "Warning",
                "reason": "BackOff",
                "object": "pod/checkout-api-7f9c-aa",
                "message": "Back-off restarting failed container",
                "count": 12,
            },
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "object": "pod/checkout-api-7f9c-bb",
                "message": "Liveness probe failed: connection refused on :8080",
                "count": 9,
            },
        ],
    }


@mcp.tool
def get_pod_logs(pod: str, tail: int = 50) -> dict:
    """Tail logs for a pod."""
    return {
        "pod": pod,
        "logs": (
            "FATAL: could not connect to redis at redis:6379: connection refused\n"
            "startup probe initiated... redis dependency unavailable\n"
            "exiting with code 1"
        ),
    }


@mcp.tool
def describe_pod(pod: str) -> dict:
    """Describe a pod (spec + recent state)."""
    return {
        "pod": pod,
        "image": "checkout-api:2026.5.1",
        "last_state": {"terminated": {"reason": "Error", "exit_code": 1}},
        "env": [{"name": "REDIS_URL", "value": "redis:6379"}],
    }


@mcp.tool
def top_pods(namespace: str = "default") -> dict:
    """Resource usage per pod."""
    return {"pods": [{"name": "checkout-api-7f9c-cc", "cpu": "12m", "mem": "64Mi"}]}


@mcp.tool
def rollout_restart(deployment: str) -> dict:
    """Restart a deployment's pods."""
    return {"deployment": deployment, "status": "rollout restarted", "revision": 9}


@mcp.tool
def scale_deployment(deployment: str, replicas: int) -> dict:
    """Scale a deployment."""
    return {"deployment": deployment, "replicas": replicas, "status": "scaled"}


@mcp.tool
def cordon_node(node: str) -> dict:
    """Cordon a node."""
    return {"node": node, "status": "cordoned"}


@mcp.tool
def get_deployment(deployment: str) -> dict:
    """Get a deployment's current status."""
    return {
        "deployment": deployment,
        "replicas": {"desired": 3, "ready": 3, "available": 3},
        "status": "healthy",
    }


if __name__ == "__main__":
    mcp.run()
