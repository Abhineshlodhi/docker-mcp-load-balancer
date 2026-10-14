import asyncio
import datetime
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import docker
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Configuration & environment variables
APP_A_URL = os.getenv("APP_A_URL", "http://app_a:8000")
APP_B_URL = os.getenv("APP_B_URL", "http://app_b:8000")
CONTAINER_A_DOCKER_NAME = os.getenv("CONTAINER_A_DOCKER_NAME", "container_a")
CONTAINER_B_DOCKER_NAME = os.getenv("CONTAINER_B_DOCKER_NAME", "container_b")

# Container states
CONTAINERS: Dict[str, Dict[str, Any]] = {
    "A": {
        "name": "A",
        "url": APP_A_URL,
        "docker_name": CONTAINER_A_DOCKER_NAME,
        "running": True,
        "healthy": False,
        "draining": False,
        "in_flight": 0,
        "requests_served": 0,
        "requests_failed": 0,
        "forced_unhealthy": False,
        "last_health_status": "unknown",
        "last_health_check": 0.0,
    },
    "B": {
        "name": "B",
        "url": APP_B_URL,
        "docker_name": CONTAINER_B_DOCKER_NAME,
        "running": True,
        "healthy": False,
        "draining": False,
        "in_flight": 0,
        "requests_served": 0,
        "requests_failed": 0,
        "forced_unhealthy": False,
        "last_health_status": "unknown",
        "last_health_check": 0.0,
    },
}

configured_split: Dict[str, int] = {"A": 50, "B": 50}
last_logged_effective_split: Optional[Dict[str, int]] = None

# Active rollout state
rollout_state: Dict[str, Any] = {
    "in_progress": False,
    "source_container": None,
    "target_container": None,
    "current_step": "idle",
    "started_at": None,
    "source": None,
    "error": None,
}

# Internal traffic generator state
traffic_gen_state: Dict[str, Any] = {
    "running": False,
    "target_rpm": 60,
    "total_sent": 0,
    "total_success": 0,
    "total_failed": 0,
    "served_by_a": 0,
    "served_by_b": 0,
    "task": None,
}

# Event log (bounded circular buffer)
events: deque = deque(maxlen=500)
event_counter = 0

state_lock = asyncio.Lock()
http_client: Optional[httpx.AsyncClient] = None
docker_client: Optional[docker.DockerClient] = None


def get_docker_client() -> Optional[docker.DockerClient]:
    global docker_client
    if docker_client is None:
        try:
            docker_client = docker.from_env()
        except Exception:
            docker_client = None
    return docker_client


def log_event(action: str, source: str, result: str, details: Optional[Dict[str, Any]] = None):
    global event_counter
    event_counter += 1
    now = datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "id": event_counter,
        "timestamp": now.isoformat(),
        "time_str": now.strftime("%H:%M:%S.%f")[:-3],
        "action": action,
        "source": source,
        "result": result,  # success, rejected, failed, aborted, info
        "details": details or {},
    }
    events.appendleft(entry)
    return entry


def is_container_eligible(name: str) -> bool:
    """A container is eligible only if it is running, healthy, and not draining."""
    state = CONTAINERS.get(name)
    if not state:
        return False
    return state["running"] and state["healthy"] and not state["draining"]


def get_container_display_status(name: str) -> str:
    """Returns human-readable status: running, healthy, draining, or stopped."""
    state = CONTAINERS[name]
    if not state["running"]:
        return "stopped"
    if state["draining"]:
        return "draining"
    if state["healthy"]:
        return "healthy"
    return "running"  # running but unhealthy


def calculate_effective_split() -> Dict[str, int]:
    """Calculate the effective traffic split based on container health and eligibility.
    If one container is unhealthy, temporarily send all traffic to the other.
    If neither is healthy, both are 0.
    """
    eligible_a = is_container_eligible("A")
    eligible_b = is_container_eligible("B")

    if eligible_a and eligible_b:
        # Both eligible: normalize configured split (or use configured directly if sum is 100)
        total = configured_split["A"] + configured_split["B"]
        if total == 0:
            return {"A": 50, "B": 50}
        return {"A": configured_split["A"], "B": configured_split["B"]}
    elif eligible_a and not eligible_b:
        return {"A": 100, "B": 0}
    elif eligible_b and not eligible_a:
        return {"A": 0, "B": 100}
    else:
        return {"A": 0, "B": 0}


async def check_container_health(name: str) -> bool:
    """Poll container /health endpoint and update state."""
    state = CONTAINERS[name]
    if not state["running"]:
        state["healthy"] = False
        state["last_health_status"] = "stopped"
        state["last_health_check"] = time.time()
        return False

    if state.get("forced_unhealthy"):
        state["healthy"] = False
        state["last_health_status"] = "forced_unhealthy"
        state["last_health_check"] = time.time()
        return False

    global http_client
    if not http_client:
        return False

    try:
        url = f"{state['url']}/health"
        resp = await http_client.get(url, timeout=2.0)
        is_healthy = (resp.status_code == 200)
        old_healthy = state["healthy"]
        state["healthy"] = is_healthy
        state["last_health_status"] = "healthy" if is_healthy else f"status_{resp.status_code}"
        state["last_health_check"] = time.time()

        # If health status flipped, log event
        if old_healthy != is_healthy:
            result = "healthy" if is_healthy else "unhealthy"
            log_event(
                action="health_check_change",
                source="system",
                result="info",
                details={
                    "container": name,
                    "previous": old_healthy,
                    "current": is_healthy,
                    "status_code": resp.status_code,
                }
            )
        return is_healthy
    except Exception as exc:
        old_healthy = state["healthy"]
        state["healthy"] = False
        state["last_health_status"] = "unreachable"
        state["last_health_check"] = time.time()
        if old_healthy:
            log_event(
                action="health_check_change",
                source="system",
                result="info",
                details={"container": name, "previous": True, "current": False, "error": str(exc)}
            )
        return False


async def refresh_all_health():
    """Refreshes health for all containers and checks for effective split changes."""
    global last_logged_effective_split
    await asyncio.gather(*(check_container_health(name) for name in CONTAINERS))

    eff_split = calculate_effective_split()
    if eff_split != last_logged_effective_split:
        if last_logged_effective_split is not None:
            log_event(
                action="effective_split_adjusted",
                source="controller",
                result="info",
                details={
                    "configured_split": configured_split.copy(),
                    "new_effective_split": eff_split.copy(),
                    "reason": "Container eligibility changed"
                }
            )
        last_logged_effective_split = eff_split.copy()


async def health_monitor_loop():
    """Background loop that regularly checks container health."""
    while True:
        try:
            async with state_lock:
                await refresh_all_health()
        except Exception:
            pass
        await asyncio.sleep(2.0)


async def traffic_generator_worker():
    """Background worker for traffic generator."""
    while traffic_gen_state["running"]:
        rpm = traffic_gen_state["target_rpm"]
        interval = 60.0 / max(rpm, 1)

        try:
            traffic_gen_state["total_sent"] += 1
            # Send request to local route handler
            start_t = time.time()
            async with httpx.AsyncClient() as client:
                res = await client.get("http://127.0.0.1:8000/route", timeout=5.0)
                if res.status_code == 200:
                    traffic_gen_state["total_success"] += 1
                    data = res.json()
                    served = data.get("served_by")
                    if served == "A":
                        traffic_gen_state["served_by_a"] += 1
                    elif served == "B":
                        traffic_gen_state["served_by_b"] += 1
                else:
                    traffic_gen_state["total_failed"] += 1
        except Exception:
            traffic_gen_state["total_failed"] += 1

        elapsed = time.time() - start_t
        to_sleep = max(0.01, interval - elapsed)
        await asyncio.sleep(to_sleep)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient()
    # Initial health check
    await refresh_all_health()
    log_event("controller_started", "system", "success", {"message": "Load Balancer Controller initialized"})

    monitor_task = asyncio.create_task(health_monitor_loop())
    yield
    monitor_task.cancel()
    if traffic_gen_state["task"]:
        traffic_gen_state["task"].cancel()
    await http_client.aclose()


app = FastAPI(title="Docker Load Balancer Controller with MCP", lifespan=lifespan)

# Allow CORS for dashboard UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------ Request / Response Models ------------------

class SplitPayload(BaseModel):
    A: int = Field(..., ge=0, le=100, description="Percentage of traffic for Container A")
    B: int = Field(..., ge=0, le=100, description="Percentage of traffic for Container B")
    source: str = Field(default="dashboard", description="Initiator of split change ('mcp', 'dashboard', 'cli')")


class RolloutPayload(BaseModel):
    source_container: str = Field(..., description="Container currently taking traffic ('A' or 'B')")
    target_container: str = Field(..., description="Target container to roll out to ('A' or 'B')")
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0, description="Timeout for target health")
    drain_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0, description="Timeout for draining old container")
    source: str = Field(default="dashboard", description="Source of action ('mcp', 'dashboard')")


class ContainerActionPayload(BaseModel):
    source: str = Field(default="dashboard", description="Source of action ('mcp', 'dashboard')")
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)


class TrafficGenPayload(BaseModel):
    requests_per_minute: int = Field(default=60, ge=1, le=6000)


# ------------------ Core Controller API ------------------

@app.get("/health")
async def controller_health():
    """Controller's own health endpoint."""
    return {"status": "healthy", "service": "load_balancer_controller"}


@app.get("/status")
async def get_status():
    """Returns state of containers, health, split, effective split, rollout status, and recent events."""
    async with state_lock:
        await refresh_all_health()
        containers_summary = {}
        for name, data in CONTAINERS.items():
            containers_summary[name] = {
                "name": name,
                "url": data["url"],
                "docker_name": data["docker_name"],
                "running": data["running"],
                "healthy": data["healthy"],
                "draining": data["draining"],
                "status": get_container_display_status(name),
                "in_flight": data["in_flight"],
                "requests_served": data["requests_served"],
                "requests_failed": data["requests_failed"],
                "last_health_status": data["last_health_status"],
            }

        eff_split = calculate_effective_split()

        return {
            "containers": containers_summary,
            "configured_split": configured_split.copy(),
            "effective_split": eff_split,
            "rollout": rollout_state.copy(),
            "traffic_generator": {
                "running": traffic_gen_state["running"],
                "target_rpm": traffic_gen_state["target_rpm"],
                "total_sent": traffic_gen_state["total_sent"],
                "total_success": traffic_gen_state["total_success"],
                "total_failed": traffic_gen_state["total_failed"],
                "served_by_a": traffic_gen_state["served_by_a"],
                "served_by_b": traffic_gen_state["served_by_b"],
            },
            "recent_events": list(events)[:25],
        }


@app.post("/split")
async def set_traffic_split(payload: SplitPayload):
    """Update configured traffic split with rigorous validation."""
    source = payload.source
    async with state_lock:
        # Check active rollout
        if rollout_state["in_progress"]:
            log_event(
                "change_split", source, "rejected",
                {"reason": "Cannot change split while rollout is in progress", "rollout": rollout_state}
            )
            raise HTTPException(
                status_code=409,
                detail="Rollout is currently in progress. Conflicting changes are rejected."
            )

        # Validate total == 100
        if payload.A + payload.B != 100:
            log_event(
                "change_split", source, "rejected",
                {"reason": "Percentages must add up to 100", "A": payload.A, "B": payload.B}
            )
            raise HTTPException(
                status_code=400,
                detail=f"Traffic split percentages must add up to 100 (got {payload.A} + {payload.B} = {payload.A + payload.B})"
            )

        # Refresh health to ensure fresh eligibility evaluation
        await refresh_all_health()

        # Reject any split that sends traffic to a stopped, unhealthy, or draining container
        for name, percent in [("A", payload.A), ("B", payload.B)]:
            if percent > 0 and not is_container_eligible(name):
                state = CONTAINERS[name]
                reason = "stopped" if not state["running"] else ("draining" if state["draining"] else "unhealthy")
                log_event(
                    "change_split", source, "rejected",
                    {"container": name, "assigned_percent": percent, "status": reason}
                )
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot allocate {percent}% traffic to container {name} because it is {reason}."
                )

        configured_split["A"] = payload.A
        configured_split["B"] = payload.B
        eff_split = calculate_effective_split()

        log_event(
            "change_split", source, "success",
            {"configured_split": configured_split.copy(), "effective_split": eff_split}
        )

        return {
            "status": "success",
            "configured_split": configured_split.copy(),
            "effective_split": eff_split,
        }


# ------------------ Traffic Routing / Load Balancer ------------------

@app.get("/route")
async def route_traffic(
    delay: float = Query(default=0.0, ge=0.0, le=60.0, description="Optional delay passed to upstream container")
):
    """Load balancer routing logic:
    - New requests go only to healthy containers that are not draining.
    - One container unhealthy: Temporarily send all traffic to the other; show effective split.
    - Neither container healthy: Return 503 Service Unavailable.
    """
    async with state_lock:
        eligible_containers = [n for n in ["A", "B"] if is_container_eligible(n)]

        # Neither container healthy
        if not eligible_containers:
            log_event(
                "route_request", "client", "failed",
                {"error": "Neither container is healthy or eligible", "status_code": 503}
            )
            raise HTTPException(
                status_code=503,
                detail="Service Unavailable: Neither container is healthy or eligible to receive traffic."
            )

        eff_split = calculate_effective_split()

        # Selection logic based on effective split
        if len(eligible_containers) == 1:
            chosen = eligible_containers[0]
        else:
            # Weighted random selection according to effective split
            weights = [eff_split[n] for n in eligible_containers]
            if sum(weights) == 0:
                chosen = eligible_containers[0]
            else:
                chosen = random.choices(eligible_containers, weights=weights, k=1)[0]

        target_state = CONTAINERS[chosen]
        target_state["in_flight"] += 1
        target_state["requests_served"] += 1
        target_url = target_state["url"]

    # Dispatch request to container
    start_time = time.time()
    try:
        url = f"{target_url}/"
        if delay > 0:
            url += f"?delay={delay}"

        resp = await http_client.get(url, timeout=delay + 10.0)
        duration = round(time.time() - start_time, 4)

        if resp.status_code != 200:
            async with state_lock:
                target_state["requests_failed"] += 1
            log_event("route_request", "client", "failed", {"container": chosen, "upstream_status": resp.status_code})
            raise HTTPException(
                status_code=502,
                detail=f"Upstream container {chosen} returned error status {resp.status_code}"
            )

        return {
            "served_by": chosen,
            "container": chosen,
            "status": "success",
            "latency_seconds": duration,
            "upstream_response": resp.json(),
            "effective_split": eff_split,
        }
    except httpx.HTTPError as exc:
        async with state_lock:
            target_state["requests_failed"] += 1
        log_event("route_request", "client", "failed", {"container": chosen, "error": str(exc)})
        raise HTTPException(
            status_code=502,
            detail=f"Request to upstream container {chosen} failed: {str(exc)}"
        )
    finally:
        async with state_lock:
            target_state["in_flight"] = max(0, target_state["in_flight"] - 1)


# ------------------ Container Lifecycle (Drain, Stop, Start) ------------------

async def docker_start_container_action(name: str):
    """Attempt starting container via Docker API, or fall back to simulated state."""
    state = CONTAINERS[name]
    client = get_docker_client()
    if client:
        try:
            container = client.containers.get(state["docker_name"])
            if container.status != "running":
                container.start()
        except Exception:
            pass
    state["running"] = True


async def docker_stop_container_action(name: str):
    """Attempt stopping container via Docker API, or fall back to simulated state."""
    state = CONTAINERS[name]
    client = get_docker_client()
    if client:
        try:
            container = client.containers.get(state["docker_name"])
            if container.status == "running":
                container.stop(timeout=5)
        except Exception:
            pass
    state["running"] = False
    state["healthy"] = False
    state["draining"] = False


@app.post("/containers/{name}/drain")
async def drain_container_endpoint(
    name: str,
    payload: Optional[ContainerActionPayload] = None,
    timeout_seconds: float = Query(default=10.0, ge=1.0, le=120.0),
    source: str = Query(default="dashboard")
):
    """Drain container: Stop sending new requests, wait for in-flight to finish."""
    name = name.upper()
    if name not in CONTAINERS:
        raise HTTPException(status_code=404, detail=f"Container {name} not found")

    if payload:
        source = payload.source
        timeout_seconds = payload.timeout_seconds

    async with state_lock:
        state = CONTAINERS[name]
        if not state["running"]:
            log_event("drain", source, "rejected", {"container": name, "reason": "Container is stopped"})
            raise HTTPException(status_code=400, detail=f"Container {name} is already stopped")

        # Check stopping last healthy container
        other_name = "B" if name == "A" else "A"
        if is_container_eligible(name) and not is_container_eligible(other_name):
            log_event("drain", source, "rejected", {"container": name, "reason": "Last healthy container"})
            raise HTTPException(status_code=409, detail="Cannot drain or stop the last healthy container.")

        state["draining"] = True
        log_event("drain", source, "info", {"container": name, "in_flight": state["in_flight"]})

    # Wait for in-flight requests to complete
    deadline = time.monotonic() + timeout_seconds
    drained_cleanly = False

    while time.monotonic() < deadline:
        async with state_lock:
            if CONTAINERS[name]["in_flight"] == 0:
                drained_cleanly = True
                break
        await asyncio.sleep(0.2)

    async with state_lock:
        in_flight = CONTAINERS[name]["in_flight"]
        if drained_cleanly:
            log_event("drain", source, "success", {"container": name, "in_flight": 0})
            return {"container": name, "status": "drained", "in_flight": 0}
        else:
            # Drain timeout: Report an error; keep the container running but send it no new requests
            log_event(
                "drain_timeout", source, "failed",
                {"container": name, "in_flight": in_flight, "timeout": timeout_seconds}
            )
            # Notice: state["draining"] stays True so it receives no new requests, and state["running"] stays True!
            raise HTTPException(
                status_code=409,
                detail=f"Drain timed out on container {name}. {in_flight} in-flight request(s) remain. Container kept running but excluded from routing."
            )


@app.post("/containers/{name}/stop")
async def stop_container_endpoint(
    name: str,
    payload: Optional[ContainerActionPayload] = None,
    timeout_seconds: float = Query(default=10.0, ge=1.0, le=120.0),
    source: str = Query(default="dashboard")
):
    """Stop container:
    1. Reject if stopping the last healthy container.
    2. Remove from routing -> let in-flight requests finish -> stop.
    3. If drain timeout: Report an error; keep running but send it no new requests.
    """
    name = name.upper()
    if name not in CONTAINERS:
        raise HTTPException(status_code=404, detail=f"Container {name} not found")

    if payload:
        source = payload.source
        timeout_seconds = payload.timeout_seconds

    # Validation: last healthy container check
    async with state_lock:
        state = CONTAINERS[name]
        if not state["running"]:
            log_event("stop_container", source, "rejected", {"container": name, "reason": "Already stopped"})
            raise HTTPException(status_code=400, detail=f"Container {name} is already stopped")

        other_name = "B" if name == "A" else "A"
        if is_container_eligible(name) and not is_container_eligible(other_name):
            log_event("stop_container", source, "rejected", {"container": name, "reason": "Last healthy container"})
            raise HTTPException(status_code=409, detail="Cannot stop the last healthy container.")

        state["draining"] = True

    # Step 2: Drain in-flight requests
    deadline = time.monotonic() + timeout_seconds
    drain_completed = False

    while time.monotonic() < deadline:
        async with state_lock:
            if CONTAINERS[name]["in_flight"] == 0:
                drain_completed = True
                break
        await asyncio.sleep(0.2)

    async with state_lock:
        if not drain_completed:
            in_flight = CONTAINERS[name]["in_flight"]
            log_event(
                "drain_timeout", source, "failed",
                {"container": name, "in_flight": in_flight, "action": "stop_aborted_to_prevent_dropping_in_flight"}
            )
            raise HTTPException(
                status_code=409,
                detail=f"Drain timeout: Container {name} still has {in_flight} in-flight request(s). Container kept running, but excluded from routing."
            )

        # Stop container
        await docker_stop_container_action(name)
        log_event("stop_container", source, "success", {"container": name})

        return {
            "status": "success",
            "container": name,
            "running": False,
            "healthy": False,
            "draining": False,
            "message": f"Container {name} drained and stopped successfully."
        }


@app.post("/containers/{name}/start")
async def start_container_endpoint(
    name: str,
    payload: Optional[ContainerActionPayload] = None,
    timeout_seconds: float = Query(default=10.0, ge=1.0, le=120.0),
    source: str = Query(default="dashboard")
):
    """Start container:
    Starts the container, then waits until it is healthy or times out.
    """
    name = name.upper()
    if name not in CONTAINERS:
        raise HTTPException(status_code=404, detail=f"Container {name} not found")

    if payload:
        source = payload.source
        timeout_seconds = payload.timeout_seconds

    async with state_lock:
        state = CONTAINERS[name]
        if state["running"] and state["healthy"]:
            log_event("start_container", source, "rejected", {"container": name, "reason": "Already running and healthy"})
            raise HTTPException(status_code=409, detail=f"Container {name} is already running and healthy.")

        # Trigger start
        await docker_start_container_action(name)
        state["draining"] = False

        # If it was marked unhealthy in worker container admin, try resetting it
        try:
            await http_client.post(f"{state['url']}/admin/reset", timeout=2.0)
        except Exception:
            pass

    log_event("start_container", source, "info", {"container": name, "message": "Container starting, awaiting health"})

    # Wait until healthy or timeout
    deadline = time.monotonic() + timeout_seconds
    became_healthy = False

    while time.monotonic() < deadline:
        is_healthy = await check_container_health(name)
        if is_healthy:
            became_healthy = True
            break
        await asyncio.sleep(0.5)

    async with state_lock:
        if became_healthy:
            log_event("start_container", source, "success", {"container": name})
            return {
                "status": "success",
                "container": name,
                "running": True,
                "healthy": True,
                "message": f"Container {name} started and verified healthy."
            }
        else:
            log_event("start_container", source, "failed", {"container": name, "error": "Health check timed out"})
            raise HTTPException(
                status_code=504,
                detail=f"Container {name} started but failed to become healthy within {timeout_seconds} seconds."
            )


# ------------------ Safe Rollout Engine ------------------

@app.post("/rollout")
async def trigger_safe_rollout(payload: RolloutPayload):
    """Safe rollout:
    1. Start target container -> Wait until healthy.
    2. If target fails health checks / times out:
       - Abort rollout!
       - Traffic stays on the old container.
    3. If target healthy:
       - Move traffic to target container (100%).
       - Drain old container.
       - Stop old container.
    """
    source = payload.source
    src_c = payload.source_container.upper()
    tgt_c = payload.target_container.upper()

    if src_c not in CONTAINERS or tgt_c not in CONTAINERS:
        raise HTTPException(status_code=400, detail="Invalid source or target container name")

    if src_c == tgt_c:
        raise HTTPException(status_code=400, detail="Source and target container cannot be the same")

    async with state_lock:
        if rollout_state["in_progress"]:
            log_event(
                "safe_rollout", source, "rejected",
                {"reason": "Another rollout is already in progress", "current_rollout": rollout_state}
            )
            raise HTTPException(status_code=409, detail="A rollout is already in progress.")

        rollout_state["in_progress"] = True
        rollout_state["source_container"] = src_c
        rollout_state["target_container"] = tgt_c
        rollout_state["current_step"] = "starting_target"
        rollout_state["started_at"] = time.time()
        rollout_state["source"] = source
        rollout_state["error"] = None

    log_event("safe_rollout", source, "info", {
        "step": "starting_target",
        "source_container": src_c,
        "target_container": tgt_c,
        "message": f"Starting target container {tgt_c}"
    })

    try:
        # Step 1: Start target
        await docker_start_container_action(tgt_c)
        async with state_lock:
            CONTAINERS[tgt_c]["draining"] = False

        # Step 2: Wait until healthy
        async with state_lock:
            rollout_state["current_step"] = "waiting_for_target_health"

        log_event("safe_rollout", source, "info", {
            "step": "waiting_for_target_health",
            "target_container": tgt_c,
            "timeout": payload.timeout_seconds
        })

        target_healthy = False
        deadline = time.monotonic() + payload.timeout_seconds

        while time.monotonic() < deadline:
            is_h = await check_container_health(tgt_c)
            if is_h:
                target_healthy = True
                break
            await asyncio.sleep(0.5)

        if not target_healthy:
            # ROLLOUT TIMEOUT / FAILURE:
            # Abort; traffic stays on the old container!
            async with state_lock:
                rollout_state["in_progress"] = False
                rollout_state["current_step"] = "aborted"
                rollout_state["error"] = f"Target container {tgt_c} failed health check"

            log_event("safe_rollout", source, "aborted", {
                "step": "rollout_aborted",
                "reason": f"Target {tgt_c} never became healthy within timeout",
                "traffic_status": f"Traffic safely preserved on original container {src_c}"
            })

            raise HTTPException(
                status_code=504,
                detail=f"Rollout aborted: Target container {tgt_c} failed health checks. Traffic remains 100% on container {src_c}."
            )

        # Step 3: Move traffic (100% to target)
        async with state_lock:
            rollout_state["current_step"] = "moving_traffic"
            configured_split[tgt_c] = 100
            configured_split[src_c] = 0

        log_event("safe_rollout", source, "info", {
            "step": "moving_traffic",
            "new_split": configured_split.copy(),
            "message": f"All traffic moved to healthy container {tgt_c}"
        })

        # Step 4: Drain old container
        async with state_lock:
            rollout_state["current_step"] = "draining_old"
            CONTAINERS[src_c]["draining"] = True

        log_event("safe_rollout", source, "info", {
            "step": "draining_old",
            "draining_container": src_c,
            "in_flight": CONTAINERS[src_c]["in_flight"]
        })

        drain_deadline = time.monotonic() + payload.drain_timeout_seconds
        while time.monotonic() < drain_deadline:
            async with state_lock:
                if CONTAINERS[src_c]["in_flight"] == 0:
                    break
            await asyncio.sleep(0.2)

        # Step 5: Stop old container
        async with state_lock:
            rollout_state["current_step"] = "stopping_old"

        await docker_stop_container_action(src_c)

        async with state_lock:
            rollout_state["in_progress"] = False
            rollout_state["current_step"] = "completed"

        log_event("safe_rollout", source, "success", {
            "step": "rollout_completed",
            "active_container": tgt_c,
            "stopped_container": src_c,
            "message": f"Safe rollout from {src_c} to {tgt_c} completed with zero dropped requests."
        })

        return {
            "status": "success",
            "message": f"Rollout completed successfully. Traffic is now 100% on {tgt_c}. {src_c} stopped.",
            "active_container": tgt_c,
            "stopped_container": src_c,
            "split": configured_split.copy(),
        }

    except HTTPException:
        raise
    except Exception as exc:
        async with state_lock:
            rollout_state["in_progress"] = False
            rollout_state["current_step"] = "failed"
            rollout_state["error"] = str(exc)
        log_event("safe_rollout", source, "failed", {"error": str(exc)})
        raise HTTPException(status_code=500, detail=f"Rollout failed unexpectedly: {str(exc)}")


# ------------------ Admin Failure Simulation Helpers ------------------

@app.post("/containers/{name}/unhealthy")
async def toggle_unhealthy(
    name: str,
    unhealthy: bool = Query(default=True),
    source: str = Query(default="dashboard")
):
    """Force container A or B unhealthy to demonstrate failover or failed rollout."""
    name = name.upper()
    if name not in CONTAINERS:
        raise HTTPException(status_code=404, detail="Container not found")

    state = CONTAINERS[name]
    state["forced_unhealthy"] = unhealthy
    try:
        if state["running"]:
            await http_client.post(
                f"{state['url']}/admin/unhealthy?unhealthy={'true' if unhealthy else 'false'}",
                timeout=2.0
            )
    except Exception:
        pass

    await check_container_health(name)
    log_event(
        "simulate_unhealthy", source, "success",
        {"container": name, "forced_unhealthy": unhealthy}
    )
    return {
        "container": name,
        "unhealthy": unhealthy,
        "status": "unhealthy" if unhealthy else "healthy"
    }


# ------------------ Traffic Generator Controls ------------------

@app.post("/traffic/start")
async def start_traffic_gen(payload: Optional[TrafficGenPayload] = None):
    """Start the integrated background traffic generator."""
    if payload:
        traffic_gen_state["target_rpm"] = payload.requests_per_minute

    if traffic_gen_state["running"]:
        return {"status": "already_running", "rpm": traffic_gen_state["target_rpm"]}

    traffic_gen_state["running"] = True
    traffic_gen_state["task"] = asyncio.create_task(traffic_generator_worker())
    log_event("traffic_generator", "dashboard", "success", {"status": "started", "rpm": traffic_gen_state["target_rpm"]})
    return {"status": "started", "rpm": traffic_gen_state["target_rpm"]}


@app.post("/traffic/stop")
async def stop_traffic_gen():
    """Stop the background traffic generator."""
    if not traffic_gen_state["running"]:
        return {"status": "already_stopped"}

    traffic_gen_state["running"] = False
    if traffic_gen_state["task"]:
        traffic_gen_state["task"].cancel()
        traffic_gen_state["task"] = None

    log_event("traffic_generator", "dashboard", "success", {"status": "stopped"})
    return {"status": "stopped", "stats": {
        "sent": traffic_gen_state["total_sent"],
        "success": traffic_gen_state["total_success"],
        "failed": traffic_gen_state["total_failed"],
        "served_by_a": traffic_gen_state["served_by_a"],
        "served_by_b": traffic_gen_state["served_by_b"],
    }}


@app.post("/traffic/reset")
async def reset_traffic_stats():
    traffic_gen_state["total_sent"] = 0
    traffic_gen_state["total_success"] = 0
    traffic_gen_state["total_failed"] = 0
    traffic_gen_state["served_by_a"] = 0
    traffic_gen_state["served_by_b"] = 0
    return {"status": "reset"}


# ------------------ Logs API ------------------

@app.get("/logs")
async def get_recent_logs(
    limit: int = Query(default=50, ge=1, le=200),
    source: Optional[str] = Query(default=None)
):
    """Returns recent events, optionally filtered by source (e.g. 'mcp')."""
    filtered = list(events)
    if source:
        filtered = [e for e in filtered if e["source"].lower() == source.lower()]
    return {"events": filtered[:limit]}


# ------------------ Static Dashboard Serving ------------------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def serve_dashboard():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/dashboard")
    async def serve_dashboard_alias():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
