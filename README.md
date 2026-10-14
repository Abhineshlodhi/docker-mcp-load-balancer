# Docker Load Balancer with MCP Controls

A production-grade, resilient HTTP load balancer and container controller that routes traffic between two Docker containers (`A` and `B`) with dynamic traffic splitting, health-aware failover, zero-downtime safe rollouts, live web observability, and a **Model Context Protocol (MCP)** server interface.

---

## Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │             Clients                    │
                      │  (Browser / MCP / Traffic Generator)  │
                      └──────────────────┬─────────────────────┘
                                         │
                                         ▼
                 ┌──────────────────────────────────────────────────┐
                 │           Controller & Load Balancer             │
                 │                 (Port: 8000)                     │
                 │                                                  │
                 │  • Reverse Proxy & Routing (/route)              │
                 │  • Configured & Effective Split Engine           │
                 │  • Safe Rollout State Machine Orchestrator       │
                 │  • Graceful Drain & In-Flight Tracking           │
                 │  • Comprehensive Audit Logger (source: mcp, ...) │
                 │  • Live Web Dashboard (HTML/CSS/JS)              │
                 │  • Built-in Background Traffic Generator         │
                 └─────────┬──────────────────────┬─────────────────┘
                           │                      │
                  HTTP/1.1 │                      │ HTTP/1.1
            (Docker Bridge)│                      │(Docker Bridge)
                           ▼                      ▼
                  ┌─────────────────┐    ┌─────────────────┐
                  │   Container A   │    │   Container B   │
                  │  container_a    │    │  container_b    │
                  │  (Host: 8001)   │    │  (Host: 8002)   │
                  │  (Internal:8000)│    │  (Internal:8000)│
                  └─────────────────┘    └─────────────────┘
```

---

## Key Features

1. **Containers A and B**:
   - Each response contains the serving container identifier (`container: "A"` or `"B"` and `X-Handled-By` header).
   - Readiness health checks via `/health` (HTTP 200 when healthy, HTTP 503 when unhealthy).
   - Configurable artificial delay parameter `?delay=N` to demonstrate request draining and zero-downtime migrations.
   - Admin failure simulation endpoint `/admin/unhealthy` to trigger controlled rollout failures.

2. **Controller and Routing Engine**:
   - **Traffic Splitting**: Percentage-based split across containers adding up to 100%.
   - **Automatic Failover**: If one container becomes unhealthy, 100% of traffic is immediately shifted to the remaining healthy container, the effective split is logged, and zero requests fail.
   - **Full Cluster Outage Defense**: If neither container is healthy, returns HTTP 503 Service Unavailable.
   - **Split Validation**: Rejects any split allocating traffic to a stopped, draining, or unhealthy container.
   - **Graceful Draining**: Waits for active in-flight requests to complete before stopping a container.
   - **Last Container Protection**: Strictly rejects stopping or draining the last remaining healthy container.
   - **Drain Timeout**: If in-flight requests exceed the drain timeout, reports an error, keeps the container running to avoid killing requests, but stops routing new requests to it.

3. **Safe Rollout Orchestrator**:
   - Execution pipeline: `Start target` $\to$ `Wait until healthy` $\to$ `Move traffic (100%)` $\to$ `Drain old` $\to$ `Stop old`.
   - **Zero-Downtime Failure Abort**: If the target container never becomes healthy within the timeout, the rollout safely aborts, and 100% of traffic stays on the original container.
   - Conflicting actions (e.g. split adjustments or concurrent rollouts) are rejected while a rollout is active.

4. **Model Context Protocol (MCP) Integration**:
   - MCP and Dashboard use the **same controller logic and state engine**.
   - Every MCP action is audited with `source: "mcp"`, including rejected operations.
   - Clear, structured error reporting for all invalid operations.

5. **Live Glassmorphic Dashboard & Traffic Generator**:
   - Real-time container metrics (status, health, in-flight count, served count).
   - Visual split slider and ratio bar with instant presets.
   - One-click safe rollout orchestrator with step progression indicator.
   - Integrated traffic generator (configurable RPM, real-time success/failure metrics).
   - Searchable, filterable audit log stream.

---

## Quick Start

### Prerequisites
- Docker Engine $\ge$ 24.0
- Docker Compose $\ge$ 2.20
- Python $\ge$ 3.10 (optional, for CLI tools and running tests)

### Starting the Stack

From the project root:

```bash
docker compose up --build -d
```

Verify all services are running and healthy:

```bash
docker compose ps
```

The services will be available at:
- **Live Web Dashboard**: [http://localhost:8000/](http://localhost:8000/)
- **Load Balancer Proxy**: [http://localhost:8000/route](http://localhost:8000/route)
- **Container A (Direct)**: [http://localhost:8001/](http://localhost:8001/)
- **Container B (Direct)**: [http://localhost:8002/](http://localhost:8002/)

---

## MCP Client Configuration

The load balancer includes an MCP server (`mcp_server.py`) using the official Model Context Protocol SDK. It exposes tools to check status, adjust traffic splits, start/stop containers, run safe rollouts, and view audit logs.

### 1. Claude Desktop Configuration
Add the server to your `claude_desktop_config.json` (located at `%APPDATA%\Claude\claude_desktop_config.json` on Windows or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "docker-load-balancer": {
      "command": "python",
      "args": ["D:/docker_mcp_load_balancer/mcp_server.py"],
      "env": {
        "CONTROLLER_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

### 2. Cursor Configuration
Add to `.cursor/mcp.json` or your Cursor global MCP settings:

```json
{
  "mcpServers": {
    "docker-load-balancer": {
      "command": "python D:/docker_mcp_load_balancer/mcp_server.py",
      "env": {
        "CONTROLLER_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

### 3. Standalone MCP Client CLI
An interactive MCP client is included for command-line execution and automated scripting:

```bash
# Check cluster status via MCP
python mcp_client.py status

# Change traffic split via MCP (with validation)
python mcp_client.py split 80 20

# Execute safe zero-downtime rollout from A to B
python mcp_client.py rollout A B

# Stop container via MCP (gracefully drains first)
python mcp_client.py stop A

# Start container via MCP (waits until healthy)
python mcp_client.py start A

# Simulate container failure via MCP
python mcp_client.py fail B

# View recent controller audit events
python mcp_client.py logs --limit 10
```

---

## API Endpoints & MCP Tools Reference

| Feature | HTTP Endpoint | MCP Tool Name | Description |
| :--- | :--- | :--- | :--- |
| **Status** | `GET /status` | `get_cluster_status()` | Returns status, health, split, effective split, in-flight counts, and rollout state |
| **Traffic Split** | `POST /split` | `set_traffic_split(a_percent, b_percent)` | Sets split (must total 100). Rejects split to stopped/unhealthy/draining containers |
| **Routing** | `GET /route?delay=N` | — | Routes traffic according to effective split. Supports artificial delay `delay` |
| **Start Container** | `POST /containers/{name}/start` | `start_container(container_name, timeout)` | Starts container and polls health until 200 or timeout |
| **Stop Container** | `POST /containers/{name}/stop` | `stop_container(container_name, timeout)` | Drains in-flight requests, then stops container. Rejects stopping last healthy container |
| **Drain Container** | `POST /containers/{name}/drain` | — | Drains in-flight requests without stopping container |
| **Safe Rollout** | `POST /rollout` | `safe_rollout(source, target, timeout)` | Zero-downtime transition: start target $\to$ wait healthy $\to$ move traffic $\to$ drain $\to$ stop old |
| **Simulate Failure** | `POST /containers/{name}/unhealthy` | `simulate_failure(container_name, unhealthy)` | Toggles unhealthy status on container A or B to test failover or aborted rollout |
| **Audit Logs** | `GET /logs` | `get_recent_logs(limit, source)` | Returns controller event log (supports filter by `source='mcp'`) |
| **Traffic Gen** | `POST /traffic/start` | — | Starts background traffic generator with target RPM |
| **Traffic Gen** | `POST /traffic/stop` | — | Stops background traffic generator |

---

## Timeouts & Health Check Specifications

| Parameter | Default Value | Configurable Range | Description |
| :--- | :--- | :--- | :--- |
| **Container Healthcheck Interval** | `3s` | Docker Compose | Time between container readiness health evaluations |
| **Container Healthcheck Timeout** | `2s` | Docker Compose | Maximum duration allowed for container `/health` response |
| **Container Healthcheck Retries** | `3` | Docker Compose | Consecutive failures before Docker marks container unhealthy |
| **Controller Health Polling** | `2s` (background) + on-demand | Controller Engine | Active polling loop and pre-route refresh |
| **Graceful Drain Timeout** | `10.0s` | `1.0s` – `120.0s` | Time to wait for in-flight requests to hit 0 before reporting drain timeout |
| **Safe Rollout Target Health Timeout** | `15.0s` | `1.0s` – `120.0s` | Timeout for target container to report healthy before aborting rollout |

---

## Automated Test Suite

A complete test suite (`tests/test_all_scenarios.py`) is included, covering all requirements and edge cases specified in the assignment.

### Running Pytest
Install testing dependencies (if running on host):
```bash
pip install pytest pytest-asyncio httpx
```

Execute the test suite:
```bash
pytest tests/test_all_scenarios.py -v
```

**Results:**
- `test_01_status_and_health`: **PASSED**
- `test_02_container_response_format`: **PASSED**
- `test_03_split_validation_total_100`: **PASSED**
- `test_04_one_container_unhealthy_failover`: **PASSED**
- `test_05_reject_split_to_unhealthy_container`: **PASSED**
- `test_06_neither_container_healthy_returns_503`: **PASSED**
- `test_07_cannot_stop_last_healthy_container`: **PASSED**
- `test_08_inflight_request_finishes_during_drain`: **PASSED**
- `test_09_safe_rollout_success`: **PASSED**
- `test_10_failed_rollout_aborts_safely`: **PASSED**
- `test_11_mcp_source_logging`: **PASSED**

---

## Demo Script Guide (3–5 Minute Video Workflow)

You can run the included automated demo runner, which walks through the exact 7 demo steps outlined in the assignment PDF:

```bash
python tests/run_demo_script.py
```

### Manual Demo Steps

1. **Traffic Running on A**:
   - Set split: `100% A` / `0% B`.
   - Start traffic generator at `120 RPM` via Dashboard or run `python traffic_generator.py --rpm 120`.
   - Observe live dashboard: Container A served count increments; Container B served count remains unchanged.

2. **MCP Client Initiates Safe Rollout**:
   - In MCP client or terminal, run:
     ```bash
     python mcp_client.py rollout A B
     ```
   - MCP client calls `safe_rollout(source='A', target='B')`.

3. **Dashboard Displays Rollout Steps (Source: MCP)**:
   - On the web dashboard, observe the **Safe Rollout Orchestrator** highlight steps in real time:
     `Start Target` $\to$ `Wait Healthy` $\to$ `Move Traffic` $\to$ `Drain Old` $\to$ `Stop Old`.
   - In the **Audit Log Table**, observe the entries logged with badge `SOURCE: MCP`.

4. **Traffic Transitioned to B (Zero Failed Requests)**:
   - Observe traffic smoothly shift to Container B.
   - Traffic generator displays `Failed (5xx): 0`.
   - Upstream responses show `"container": "B"`.

5. **In-Flight Request Finishes on A Before A Stops**:
   - Send request with delay: `curl "http://localhost:8000/route?delay=3.0"`
   - Immediately issue stop command for Container A.
   - Container A status changes to `DRAINING`. In-flight counter shows `1`.
   - The delayed request completes with HTTP 200, and only then does Container A transition to `STOPPED`.

6. **Failed Rollout: Target Never Becomes Healthy**:
   - Make Container A unhealthy to simulate a defect:
     ```bash
     python mcp_client.py fail A
     ```
   - Attempt safe rollout from B to A:
     ```bash
     python mcp_client.py rollout B A
     ```
   - Rollout times out waiting for A, logs abort message, and returns HTTP 504.
   - **Verification**: Container B continues serving 100% of traffic without dropping a single request.

7. **Invalid Operations Return Clear Errors**:
   - Attempt to stop the last healthy container (B):
     ```bash
     python mcp_client.py stop B
     # HTTP 409 Conflict: Cannot stop the last healthy container.
     ```
   - Attempt invalid split totaling 120%:
     ```bash
     python mcp_client.py split 60 60
     # HTTP 400 Bad Request: Traffic split percentages must add up to 100.
     ```
   - Attempt routing split traffic to unhealthy container:
     ```bash
     python mcp_client.py split 50 50
     # HTTP 400 Bad Request: Cannot allocate 50% traffic to container A because it is unhealthy.
     ```

---

## AI Tools Used

| Tool | Purpose & Usage |
| :--- | :--- |
| **Gemini 3.8 / Antigravity Agentic Framework** | Orchestrated end-to-end architecture design, FastAPI controller state machine implementation, Docker container definitions, and comprehensive test suite generation. |
| **Claude / Cursor MCP Specifications** | Designed the Model Context Protocol (MCP) tool schema and client integration following MCP 2024 standards. |
| **Browser Subagent / Automated UI Verification** | Validated live web dashboard DOM elements, responsiveness, and aesthetic styling tokens. |
