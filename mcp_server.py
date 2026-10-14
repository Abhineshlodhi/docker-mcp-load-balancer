"""MCP (Model Context Protocol) Server for Docker Load Balancer.

Exposes tools for controlling traffic split, monitoring status, safe rollouts,
container lifecycle, and retrieving controller audit logs.
All operations explicitly log actions with source 'mcp'.
"""

import os
import sys
from typing import Any, Dict, Optional

import httpx
from mcp.server.fastmcp import FastMCP

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://127.0.0.1:8000")

# Initialize FastMCP Server
mcp = FastMCP("docker-load-balancer")


def _format_error(prefix: str, exc: Exception) -> str:
    """Format HTTP errors or exceptions into clear error messages."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            err_data = exc.response.json()
            detail = err_data.get("detail", str(exc))
            return f"Error ({exc.response.status_code}): {detail}"
        except Exception:
            return f"Error ({exc.response.status_code}): {exc.response.text}"
    return f"{prefix}: {str(exc)}"


@mcp.tool()
async def get_cluster_status() -> Dict[str, Any]:
    """Get the complete cluster status.

    Returns the status of Container A and Container B (running, healthy, draining, stopped),
    the configured traffic split, the effective traffic split, in-flight requests,
    total requests served, active rollout status, and recent events.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{CONTROLLER_URL}/status", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error("Failed to retrieve status", exc)}


@mcp.tool()
async def set_traffic_split(a_percent: int, b_percent: int) -> Dict[str, Any]:
    """Change the traffic distribution split between Container A and Container B.

    Validation rules:
    - a_percent + b_percent must equal exactly 100.
    - Cannot send traffic (> 0%) to a stopped, unhealthy, or draining container.
    - Cannot change split while a rollout is in progress.
    Logs action with source 'mcp'.

    Args:
        a_percent: Percentage of traffic directed to Container A (0-100)
        b_percent: Percentage of traffic directed to Container B (0-100)
    """
    async with httpx.AsyncClient() as client:
        try:
            payload = {"A": a_percent, "B": b_percent, "source": "mcp"}
            resp = await client.post(f"{CONTROLLER_URL}/split", json=payload, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error("Traffic split rejected", exc)}


@mcp.tool()
async def start_container(container_name: str, timeout_seconds: float = 10.0) -> Dict[str, Any]:
    """Start a container and wait until it is verified healthy or times out.

    Logs action with source 'mcp'.

    Args:
        container_name: Container to start ('A' or 'B')
        timeout_seconds: Maximum time to wait for the health check to succeed (default 10s)
    """
    c_name = container_name.upper().strip()
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{CONTROLLER_URL}/containers/{c_name}/start?source=mcp&timeout_seconds={timeout_seconds}",
                timeout=timeout_seconds + 5.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error(f"Failed to start container {c_name}", exc)}


@mcp.tool()
async def stop_container(container_name: str, timeout_seconds: float = 10.0) -> Dict[str, Any]:
    """Drain and stop a container.

    Validation rules:
    - Cannot stop the last healthy container (rejected with 409).
    - Removes container from routing, waits for in-flight requests to complete.
    - If drain times out: reports error, keeps container running but sends no new requests.
    Logs action with source 'mcp'.

    Args:
        container_name: Container to stop ('A' or 'B')
        timeout_seconds: Maximum time to wait for in-flight requests to finish draining (default 10s)
    """
    c_name = container_name.upper().strip()
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{CONTROLLER_URL}/containers/{c_name}/stop?source=mcp&timeout_seconds={timeout_seconds}",
                timeout=timeout_seconds + 5.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error(f"Cannot stop container {c_name}", exc)}


@mcp.tool()
async def safe_rollout(
    source_container: str,
    target_container: str,
    timeout_seconds: float = 15.0,
    drain_timeout_seconds: float = 10.0
) -> Dict[str, Any]:
    """Execute a safe zero-downtime rollout from source container to target container.

    Execution Flow:
    1. Start target container -> Wait until healthy.
    2. If target fails health checks or times out:
       - Abort rollout!
       - Traffic stays 100% on the old container with zero dropped requests.
    3. If target becomes healthy:
       - Move 100% traffic to target container.
       - Drain old container.
       - Stop old container.
    Logs all steps with source 'mcp'.

    Args:
        source_container: Container currently serving traffic ('A' or 'B')
        target_container: Container to deploy traffic to ('A' or 'B')
        timeout_seconds: Time to wait for target container to become healthy (default 15s)
        drain_timeout_seconds: Time to wait for in-flight requests to finish (default 10s)
    """
    src = source_container.upper().strip()
    tgt = target_container.upper().strip()
    async with httpx.AsyncClient() as client:
        try:
            payload = {
                "source_container": src,
                "target_container": tgt,
                "timeout_seconds": timeout_seconds,
                "drain_timeout_seconds": drain_timeout_seconds,
                "source": "mcp",
            }
            resp = await client.post(
                f"{CONTROLLER_URL}/rollout",
                json=payload,
                timeout=timeout_seconds + drain_timeout_seconds + 5.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error(f"Rollout from {src} to {tgt} failed", exc)}


@mcp.tool()
async def get_recent_logs(limit: int = 25, source: Optional[str] = None) -> Dict[str, Any]:
    """Get the recent controller event audit logs.

    Args:
        limit: Number of events to retrieve (default 25)
        source: Optional filter by source ('mcp', 'dashboard', 'system')
    """
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CONTROLLER_URL}/logs?limit={limit}"
            if source:
                url += f"&source={source}"
            resp = await client.get(url, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error("Failed to retrieve logs", exc)}


@mcp.tool()
async def simulate_failure(container_name: str, unhealthy: bool = True) -> Dict[str, Any]:
    """Force a container into an unhealthy state to simulate failures or demo failed rollouts.

    Args:
        container_name: Target container ('A' or 'B')
        unhealthy: True to make unhealthy, False to restore healthy
    """
    c_name = container_name.upper().strip()
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{CONTROLLER_URL}/containers/{c_name}/unhealthy?unhealthy={'true' if unhealthy else 'false'}&source=mcp",
                timeout=5.0
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": _format_error(f"Failed to set container {c_name} health", exc)}


if __name__ == "__main__":
    # Standard stdio server for MCP clients
    mcp.run()
