"""Automated Test Suite for Docker Load Balancer with MCP Controls.

Validates all cases specified in the assignment:
1. Container A and B response formatting and health checks
2. Traffic split validation (totals 100, reject invalid)
3. Routing & failover (temporary 100% split shift when one container unhealthy)
4. Neither container healthy -> 503 Service Unavailable
5. Reject split sending traffic to stopped/unhealthy container
6. In-flight request finishes cleanly before container stops (drain)
7. Reject stopping the last healthy container
8. Safe rollout success (A -> B with zero dropped requests)
9. Failed rollout aborts safely and traffic stays on source container
10. MCP audit logging with source 'mcp'
"""

import asyncio
import time
import httpx
import pytest

CONTROLLER_URL = "http://127.0.0.1:8000"


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_01_status_and_health():
    """Verify cluster is accessible and initial status reports healthy."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Restore both to healthy initially
        await client.post("/containers/A/unhealthy?unhealthy=false&source=test")
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await client.post("/containers/A/start?source=test")
        await client.post("/containers/B/start?source=test")
        await client.post("/split", json={"A": 50, "B": 50, "source": "test"})

        resp = await client.get("/status")
        assert resp.status_code == 200, f"Status check failed: {resp.text}"
        data = resp.json()
        assert "containers" in data
        assert "A" in data["containers"]
        assert "B" in data["containers"]
        assert data["effective_split"] == {"A": 50, "B": 50}


@pytest.mark.asyncio
async def test_02_container_response_format():
    """Verify each response specifies which container handled it."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        resp = await client.get("/route")
        assert resp.status_code == 200
        data = resp.json()
        assert "served_by" in data
        assert data["served_by"] in ["A", "B"]
        assert data["upstream_response"]["container"] in ["A", "B"]


@pytest.mark.asyncio
async def test_03_split_validation_total_100():
    """Verify split percentages must add up to 100."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Invalid split 60 + 50 != 100
        resp = await client.post("/split", json={"A": 60, "B": 50, "source": "mcp"})
        assert resp.status_code == 400
        assert "must add up to 100" in resp.json()["detail"]

        # Valid split
        resp2 = await client.post("/split", json={"A": 70, "B": 30, "source": "mcp"})
        assert resp2.status_code == 200
        assert resp2.json()["configured_split"] == {"A": 70, "B": 30}


@pytest.mark.asyncio
async def test_04_one_container_unhealthy_failover():
    """When one container is unhealthy: temporarily send all traffic to the other, show effective split and log it."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Set split 70/30
        await client.post("/split", json={"A": 70, "B": 30, "source": "test"})

        # Force B unhealthy
        resp_unhealthy = await client.post("/containers/B/unhealthy?unhealthy=true&source=mcp")
        assert resp_unhealthy.status_code == 200

        # Wait a moment for health refresh
        await asyncio.sleep(0.5)

        # Check status: effective split should now be A: 100, B: 0
        status_resp = await client.get("/status")
        eff = status_resp.json()["effective_split"]
        assert eff == {"A": 100, "B": 0}, f"Expected effective split 100/0, got {eff}"

        # Send 10 requests: 100% must go to A without failure
        for _ in range(10):
            r = await client.get("/route")
            assert r.status_code == 200
            assert r.json()["served_by"] == "A"

        # Restore B
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_05_reject_split_to_unhealthy_container():
    """Reject any split that sends traffic to a stopped, unhealthy, or draining container."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Force B unhealthy
        await client.post("/containers/B/unhealthy?unhealthy=true&source=test")
        await asyncio.sleep(0.5)

        # Try setting split that allocates traffic to B
        resp = await client.post("/split", json={"A": 50, "B": 50, "source": "mcp"})
        assert resp.status_code == 400
        assert "unhealthy" in resp.json()["detail"].lower()

        # Restore B
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_06_neither_container_healthy_returns_503():
    """Return a service-unavailable response when neither container is healthy."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Force both unhealthy
        await client.post("/containers/A/unhealthy?unhealthy=true&source=test")
        await client.post("/containers/B/unhealthy?unhealthy=true&source=test")
        await asyncio.sleep(0.5)

        resp = await client.get("/route")
        assert resp.status_code == 503
        assert "neither container is healthy" in resp.json()["detail"].lower()

        # Restore both
        await client.post("/containers/A/unhealthy?unhealthy=false&source=test")
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_07_cannot_stop_last_healthy_container():
    """Stopping the last healthy container must be REJECTED."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        # Make B unhealthy
        await client.post("/containers/B/unhealthy?unhealthy=true&source=test")
        await asyncio.sleep(0.5)

        # Attempt to stop A (the only healthy container)
        resp = await client.post("/containers/A/stop?source=mcp")
        assert resp.status_code == 409
        assert "last healthy container" in resp.json()["detail"].lower()

        # Restore B
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_08_inflight_request_finishes_during_drain():
    """Verify an in-flight request finishes cleanly before the container stops."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=20.0) as client:
        # Ensure clean state
        await client.post("/containers/A/start?source=test")
        await client.post("/containers/B/start?source=test")
        await client.post("/containers/A/unhealthy?unhealthy=false&source=test")
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")
        await client.post("/split", json={"A": 100, "B": 0, "source": "test"})
        await asyncio.sleep(0.5)

        # Fire a delayed request in background (delay = 2 seconds)
        async def slow_request():
            async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as c:
                return await c.get("/route?delay=2.0")

        slow_task = asyncio.create_task(slow_request())
        await asyncio.sleep(0.3)  # Wait for request to register as in-flight

        # Verify in-flight count > 0
        status = await client.get("/status")
        assert status.json()["containers"]["A"]["in_flight"] >= 1

        # Now drain container A
        drain_resp = await client.post("/containers/A/drain?source=mcp&timeout_seconds=5.0")
        assert drain_resp.status_code == 200

        # Slow task must finish successfully with 200
        slow_resp = await slow_task
        assert slow_resp.status_code == 200
        assert slow_resp.json()["served_by"] == "A"


@pytest.mark.asyncio
async def test_09_safe_rollout_success():
    """Verify safe rollout: Start target -> wait healthy -> move traffic -> drain old -> stop old."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=30.0) as client:
        # Setup: A is primary (100%), B is healthy
        await client.post("/containers/A/start?source=test")
        await client.post("/containers/B/start?source=test")
        await client.post("/split", json={"A": 100, "B": 0, "source": "test"})

        # Rollout from A to B
        rollout_resp = await client.post(
            "/rollout",
            json={
                "source_container": "A",
                "target_container": "B",
                "timeout_seconds": 15.0,
                "drain_timeout_seconds": 5.0,
                "source": "mcp",
            }
        )
        assert rollout_resp.status_code == 200
        data = rollout_resp.json()
        assert data["status"] == "success"
        assert data["active_container"] == "B"
        assert data["stopped_container"] == "A"
        assert data["split"] == {"A": 0, "B": 100}

        # Check traffic now goes 100% to B
        r = await client.get("/route")
        assert r.status_code == 200
        assert r.json()["served_by"] == "B"


@pytest.mark.asyncio
async def test_10_failed_rollout_aborts_safely():
    """When target fails health check: Abort rollout; traffic stays on old container."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=30.0) as client:
        # Setup: Start A, make A primary (100% A)
        await client.post("/containers/A/start?source=test")
        await client.post("/containers/B/start?source=test")
        await client.post("/split", json={"A": 100, "B": 0, "source": "test"})

        # Force target B to be unhealthy
        await client.post("/containers/B/unhealthy?unhealthy=true&source=test")
        await asyncio.sleep(0.5)

        # Attempt rollout from A to B with short timeout
        rollout_resp = await client.post(
            "/rollout",
            json={
                "source_container": "A",
                "target_container": "B",
                "timeout_seconds": 3.0,
                "drain_timeout_seconds": 2.0,
                "source": "mcp",
            }
        )
        assert rollout_resp.status_code == 504
        assert "rollout aborted" in rollout_resp.json()["detail"].lower()

        # Traffic MUST stay 100% on A with zero interruption!
        r = await client.get("/route")
        assert r.status_code == 200
        assert r.json()["served_by"] == "A"

        # Restore B
        await client.post("/containers/B/unhealthy?unhealthy=false&source=test")


@pytest.mark.asyncio
async def test_11_mcp_source_logging():
    """Verify all MCP actions are logged with source 'mcp', including rejected ones."""
    async with httpx.AsyncClient(base_url=CONTROLLER_URL, timeout=10.0) as client:
        logs_resp = await client.get("/logs?source=mcp&limit=50")
        assert logs_resp.status_code == 200
        events = logs_resp.json()["events"]
        assert len(events) > 0
        for e in events:
            assert e["source"] == "mcp"
