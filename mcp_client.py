"""MCP Client CLI & Automation Helper.

Can be run directly to interact with the Load Balancer using MCP:
  python mcp_client.py status
  python mcp_client.py split 80 20
  python mcp_client.py rollout A B
  python mcp_client.py stop A
  python mcp_client.py start A
  python mcp_client.py fail B
  python mcp_client.py logs
"""

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

# Default controller address
CONTROLLER_URL = "http://127.0.0.1:8000"


async def check_status():
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{CONTROLLER_URL}/status")
        data = resp.json()
        print(json.dumps(data, indent=2))


async def set_split(a: int, b: int):
    async with httpx.AsyncClient() as client:
        payload = {"A": a, "B": b, "source": "mcp"}
        resp = await client.post(f"{CONTROLLER_URL}/split", json=payload)
        if resp.status_code == 200:
            print("[MCP SUCCESS] Traffic split updated:")
            print(json.dumps(resp.json(), indent=2))
        else:
            print(f"[MCP REJECTED - {resp.status_code}]")
            print(json.dumps(resp.json(), indent=2))


async def rollout(source: str, target: str, timeout: float = 15.0):
    async with httpx.AsyncClient() as client:
        payload = {
            "source_container": source,
            "target_container": target,
            "timeout_seconds": timeout,
            "drain_timeout_seconds": 10.0,
            "source": "mcp",
        }
        print(f"[MCP CLIENT] Requesting Safe Rollout: {source} -> {target}...")
        resp = await client.post(f"{CONTROLLER_URL}/rollout", json=payload, timeout=timeout + 20.0)
        if resp.status_code == 200:
            print("[MCP SUCCESS] Safe Rollout Completed:")
            print(json.dumps(resp.json(), indent=2))
        else:
            print(f"[MCP FAILED/ABORTED - {resp.status_code}]:")
            print(json.dumps(resp.json(), indent=2))


async def stop_c(container: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{CONTROLLER_URL}/containers/{container}/stop?source=mcp", timeout=20.0)
        print(f"[MCP Status {resp.status_code}]:")
        print(json.dumps(resp.json(), indent=2))


async def start_c(container: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{CONTROLLER_URL}/containers/{container}/start?source=mcp", timeout=20.0)
        print(f"[MCP Status {resp.status_code}]:")
        print(json.dumps(resp.json(), indent=2))


async def simulate_unhealthy(container: str, unhealthy: bool = True):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CONTROLLER_URL}/containers/{container}/unhealthy?unhealthy={'true' if unhealthy else 'false'}&source=mcp",
            timeout=5.0
        )
        print(f"[MCP Status {resp.status_code}]:")
        print(json.dumps(resp.json(), indent=2))


async def show_logs(limit: int = 15):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{CONTROLLER_URL}/logs?limit={limit}")
        data = resp.json()
        print(f"=== Recent Controller Events (Limit: {limit}) ===")
        for e in data.get("events", []):
            print(f"[{e.get('time_str', e.get('timestamp'))}] [{e.get('source').upper()}] {e.get('action')} -> {e.get('result')}: {json.dumps(e.get('details', {}))}")


def main():
    parser = argparse.ArgumentParser(description="Docker Load Balancer MCP Client CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Get cluster status via MCP")

    split_p = subparsers.add_parser("split", help="Set traffic split (e.g. split 80 20)")
    split_p.add_argument("a", type=int)
    split_p.add_argument("b", type=int)

    rollout_p = subparsers.add_parser("rollout", help="Trigger safe rollout (e.g. rollout A B)")
    rollout_p.add_argument("source", type=str)
    rollout_p.add_argument("target", type=str)
    rollout_p.add_argument("--timeout", type=float, default=15.0)

    stop_p = subparsers.add_parser("stop", help="Stop container via MCP (e.g. stop A)")
    stop_p.add_argument("container", type=str)

    start_p = subparsers.add_parser("start", help="Start container via MCP (e.g. start A)")
    start_p.add_argument("container", type=str)

    fail_p = subparsers.add_parser("fail", help="Simulate container failure (e.g. fail B)")
    fail_p.add_argument("container", type=str)
    fail_p.add_argument("--restore", action="store_true", help="Restore to healthy")

    logs_p = subparsers.add_parser("logs", help="Get controller logs")
    logs_p.add_argument("--limit", type=int, default=15)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "status":
        asyncio.run(check_status())
    elif args.command == "split":
        asyncio.run(set_split(args.a, args.b))
    elif args.command == "rollout":
        asyncio.run(rollout(args.source, args.target, args.timeout))
    elif args.command == "stop":
        asyncio.run(stop_c(args.container))
    elif args.command == "start":
        asyncio.run(start_c(args.container))
    elif args.command == "fail":
        asyncio.run(simulate_unhealthy(args.container, not args.restore))
    elif args.command == "logs":
        asyncio.run(show_logs(args.limit))


if __name__ == "__main__":
    main()
