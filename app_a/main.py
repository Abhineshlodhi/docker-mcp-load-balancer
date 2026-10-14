import asyncio
import os
import time
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

CONTAINER_NAME = os.getenv("CONTAINER_NAME", "A")

app = FastAPI(title=f"Worker Container {CONTAINER_NAME}")
is_unhealthy = False


class UnhealthyPayload(BaseModel):
    unhealthy: bool = True


@app.get("/")
async def handle_request(
    response: Response,
    delay: float = Query(default=0.0, ge=0.0, le=60.0, description="Artificial delay in seconds")
):
    if delay > 0:
        await asyncio.sleep(delay)

    response.headers["X-Handled-By"] = CONTAINER_NAME
    return {
        "container": CONTAINER_NAME,
        "message": f"Hello from Container {CONTAINER_NAME}",
        "status": "success",
        "delay_applied": delay,
        "timestamp": time.time(),
    }


@app.get("/health")
async def health_check():
    if is_unhealthy:
        raise HTTPException(
            status_code=503,
            detail={
                "container": CONTAINER_NAME,
                "status": "unhealthy",
                "reason": "Simulated failure via admin toggle"
            }
        )
    return {
        "container": CONTAINER_NAME,
        "status": "healthy",
        "timestamp": time.time()
    }


@app.post("/admin/unhealthy")
async def set_unhealthy(
    payload: UnhealthyPayload = None,
    unhealthy: bool = Query(default=None)
):
    global is_unhealthy
    if unhealthy is not None:
        is_unhealthy = unhealthy
    elif payload is not None:
        is_unhealthy = payload.unhealthy
    else:
        is_unhealthy = True

    return {
        "container": CONTAINER_NAME,
        "unhealthy": is_unhealthy,
        "status": "unhealthy" if is_unhealthy else "healthy"
    }


@app.post("/admin/reset")
async def reset_health():
    global is_unhealthy
    is_unhealthy = False
    return {
        "container": CONTAINER_NAME,
        "unhealthy": False,
        "status": "healthy"
    }
