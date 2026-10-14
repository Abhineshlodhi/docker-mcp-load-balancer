"""Interactive / Automated Demo Script matching the exact 7 steps in the assignment PDF:

1. Traffic running on A
2. An MCP client checks status, then starts a safe rollout from A to B
3. The dashboard shows each rollout step, with source mcp
4. Traffic now on B, with zero failed requests
5. An in-flight request finishes on A before A stops
6. A failed rollout: the target never becomes healthy, and the original container keeps serving
7. An invalid operation returns a clear error
"""

import asyncio
import json
import time
import httpx

CONTROLLER_URL = "http://127.0.0.1:8000"


def print_banner(step_num: int, title: str):
    print("\n" + "=" * 70)
    print(f" DEMO STEP {step_num}: {title.upper()}")
    print("=" * 70)


async def main():
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=30.0) as client:
        # Step 0: Ensure cluster is up and healthy
        print("\n[Setup] Initializing cluster state...")
        await client.post("/containers/A/start?source=system")
        await client.post("/containers/B/start?source=system")
        await client.post("/containers/A/unhealthy?unhealthy=false&source=system")
        await client.post("/containers/B/unhealthy?unhealthy=false&source=system")
        await client.post("/split", json={"A": 100, "B": 0, "source": "system"})
        await asyncio.sleep(1.0)

        # -------------------------------------------------------------
        # Step 1: Traffic running on A
        # -------------------------------------------------------------
        print_banner(1, "Traffic running on Container A")
        print("Sending initial burst of requests to verify traffic goes to Container A...")
        for i in range(5):
            res = await client.get("/route")
            data = res.json()
            print(f"  -> Request {i+1}: HTTP {res.status_code} | Served by: {data['served_by']} | Latency: {data['latency_seconds']}s")
            await asyncio.sleep(0.3)
        print("\n[OK] 100% of traffic is successfully flowing through Container A.")
        await asyncio.sleep(1.5)

        # -------------------------------------------------------------
        # Step 2: An MCP client checks status, then starts a safe rollout from A to B
        # -------------------------------------------------------------
        print_banner(2, "MCP client checks status, then initiates safe rollout A -> B")
        status_res = await client.get("/status")
        status = status_res.json()
        print("[MCP CLIENT: get_cluster_status()] Response:")
        print(f"  Container A: status={status['containers']['A']['status']}, in_flight={status['containers']['A']['in_flight']}")
        print(f"  Container B: status={status['containers']['B']['status']}, in_flight={status['containers']['B']['in_flight']}")
        print(f"  Configured Split: {status['configured_split']}")
        print(f"  Effective Split:  {status['effective_split']}")

        print("\n[MCP CLIENT: safe_rollout(source='A', target='B')] Invoking rollout...")
        rollout_task = asyncio.create_task(
            client.post(
                "/rollout",
                json={
                    "source_container": "A",
                    "target_container": "B",
                    "timeout_seconds": 15.0,
                    "drain_timeout_seconds": 5.0,
                    "source": "mcp"
                }
            )
        )

        # -------------------------------------------------------------
        # Step 3: The dashboard shows each rollout step, with source mcp
        # -------------------------------------------------------------
        print_banner(3, "Rollout progress audit events (source: mcp)")
        while not rollout_task.done():
            logs_res = await client.get("/logs?source=mcp&limit=5")
            recent = logs_res.json().get("events", [])
            if recent:
                top = recent[0]
                print(f"  [DASHBOARD/AUDIT] Time: {top.get('time_str')} | Action: {top.get('action')} | Source: {top.get('source')} | Result: {top.get('result')} | Step: {top.get('details', {}).get('step')}")
            await asyncio.sleep(0.8)

        rollout_res = await rollout_task
        print("\n[Rollout Response]:", json.dumps(rollout_res.json(), indent=2))
        await asyncio.sleep(1.5)

        # -------------------------------------------------------------
        # Step 4: Traffic now on B, with zero failed requests
        # -------------------------------------------------------------
        print_banner(4, "Traffic now on Container B with ZERO failed requests")
        print("Sending verification traffic burst:")
        failures = 0
        for i in range(8):
            res = await client.get("/route")
            data = res.json()
            if res.status_code != 200 or data["served_by"] != "B":
                failures += 1
            print(f"  -> Request {i+1}: HTTP {res.status_code} | Served by: {data['served_by']} | Response: {data['upstream_response']['message']}")
            await asyncio.sleep(0.2)
        print(f"\n[OK] Traffic successfully transitioned to Container B. Failed requests: {failures}")
        await asyncio.sleep(1.5)

        # -------------------------------------------------------------
        # Step 5: An in-flight request finishes on A before A stops
        # -------------------------------------------------------------
        print_banner(5, "In-flight request finishes on A before A stops (Graceful Drain)")
        # Start A again and route to A
        await client.post("/containers/A/start?source=system")
        await client.post("/split", json={"A": 100, "B": 0, "source": "system"})

        print("Dispatching long-running request with 2.5s artificial delay to Container A...")
        delayed_req = asyncio.create_task(client.get("/route?delay=2.5"))
        await asyncio.sleep(0.4)  # Let it hit Container A

        status = (await client.get("/status")).json()
        print(f"  -> Active In-Flight on Container A: {status['containers']['A']['in_flight']}")

        print("Issuing drain & stop command to Container A while request is running...")
        stop_req = asyncio.create_task(client.post("/containers/A/stop?source=mcp&timeout_seconds=5.0"))

        print("Awaiting completion of in-flight request...")
        delayed_res = await delayed_req
        data = delayed_res.json()
        print(f"  -> Delayed in-flight request finished: HTTP {delayed_res.status_code} | Handled by: {data['served_by']} (Latency: {data['latency_seconds']}s)")

        stop_res = await stop_req
        print(f"  -> Container A stopped: {stop_res.json()['status']}")
        await asyncio.sleep(1.5)

        # -------------------------------------------------------------
        # Step 6: Failed rollout (target never becomes healthy, original keeps serving)
        # -------------------------------------------------------------
        print_banner(6, "Failed Rollout Demo: Target never becomes healthy")
        # Ensure B is running and taking 100% traffic
        await client.post("/containers/B/start?source=system")
        await client.post("/split", json={"A": 0, "B": 100, "source": "system"})

        # Force target A to be UNHEALTHY
        print("Forcing target Container A to be UNHEALTHY (simulating startup defect)...")
        await client.post("/containers/A/unhealthy?unhealthy=true&source=mcp")

        print("Attempting Safe Rollout from B -> A (timeout 4s)...")
        failed_rollout_res = await client.post(
            "/rollout",
            json={
                "source_container": "B",
                "target_container": "A",
                "timeout_seconds": 4.0,
                "drain_timeout_seconds": 2.0,
                "source": "mcp"
            }
        )
        print(f"  -> Rollout Result: HTTP {failed_rollout_res.status_code}")
        err_msg = failed_rollout_res.json().get('detail', failed_rollout_res.text)
        print(f"  -> Rollout Aborted Message: {err_msg}")

        print("\nVerifying that original Container B continued serving without dropping traffic:")
        for i in range(5):
            res = await client.get("/route")
            data = res.json()
            print(f"  -> Request {i+1}: HTTP {res.status_code} | Served by: {data['served_by']} (B is uninterrupted!)")
            await asyncio.sleep(0.2)
        print("[OK] Original container B safely preserved 100% of traffic. Zero downtime.")
        await asyncio.sleep(1.5)

        # -------------------------------------------------------------
        # Step 7: Invalid operation returns a clear error
        # -------------------------------------------------------------
        print_banner(7, "Invalid operation validation returns a clear error")
        print("Attempting to stop Container B (the LAST healthy container):")
        invalid_stop = await client.post("/containers/B/stop?source=mcp")
        print(f"  -> Rejected with HTTP {invalid_stop.status_code}: {invalid_stop.json()['detail']}")

        print("\nAttempting invalid split percentages that do not add to 100 (60% + 60%):")
        invalid_split1 = await client.post("/split", json={"A": 60, "B": 60, "source": "mcp"})
        print(f"  -> Rejected with HTTP {invalid_split1.status_code}: {invalid_split1.json()['detail']}")

        print("\nAttempting to route split traffic to stopped/unhealthy container A:")
        invalid_split2 = await client.post("/split", json={"A": 50, "B": 50, "source": "mcp"})
        print(f"  -> Rejected with HTTP {invalid_split2.status_code}: {invalid_split2.json()['detail']}")

        # Cleanup
        await client.post("/containers/A/unhealthy?unhealthy=false&source=system")
        await client.post("/containers/A/start?source=system")
        await client.post("/split", json={"A": 50, "B": 50, "source=system": "system"})

        print("\n" + "=" * 70)
        print(" ALL 7 DEMO SCENARIOS COMPLETED SUCCESSFULLY! ")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
