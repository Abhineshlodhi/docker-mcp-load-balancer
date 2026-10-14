"""CLI Traffic Generator for Docker Load Balancer.

Usage:
  python traffic_generator.py --rpm 120
  python traffic_generator.py --rpm 300 --duration 30
"""

import argparse
import asyncio
import sys
import time

import httpx

CONTROLLER_URL = "http://127.0.0.1:8000"


class TrafficStats:
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.served_a = 0
        self.served_b = 0
        self.split_a = 0
        self.split_b = 0


async def send_request(client: httpx.AsyncClient, stats: TrafficStats):
    stats.total += 1
    try:
        resp = await client.get(f"{CONTROLLER_URL}/route", timeout=5.0)
        if resp.status_code == 200:
            stats.success += 1
            data = resp.json()
            served = data.get("served_by")
            if served == "A":
                stats.served_a += 1
            elif served == "B":
                stats.served_b += 1
            eff = data.get("effective_split", {})
            stats.split_a = eff.get("A", 0)
            stats.split_b = eff.get("B", 0)
        else:
            stats.failed += 1
    except Exception:
        stats.failed += 1


async def run_traffic(rpm: int, duration: float = None):
    delay_between_requests = 60.0 / max(rpm, 1)
    stats = TrafficStats()
    start_time = time.time()

    print("=" * 60)
    print(f" Traffic Generator Started: {rpm} Requests/Min")
    print(f" Target: {CONTROLLER_URL}/route")
    print(" Press Ctrl+C at any time to stop.")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break

                t0 = time.time()
                await send_request(client, stats)

                elapsed = round(time.time() - start_time, 1)
                eff_split_str = f"A:{stats.split_a}% / B:{stats.split_b}%"
                sys.stdout.write(
                    f"\r[{elapsed:>5}s] Total: {stats.total:<5} | "
                    f"OK: \033[92m{stats.success:<5}\033[0m | "
                    f"Fail: \033[91m{stats.failed:<3}\033[0m | "
                    f"Served A: {stats.served_a:<5} | "
                    f"Served B: {stats.served_b:<5} | "
                    f"Split: {eff_split_str:<12}"
                )
                sys.stdout.flush()

                cost = time.time() - t0
                to_sleep = max(0.001, delay_between_requests - cost)
                await asyncio.sleep(to_sleep)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    print("\n" + "=" * 60)
    print(" Final Traffic Summary:")
    print(f" Total Requests:    {stats.total}")
    print(f" Successful (200):  {stats.success}")
    print(f" Failed (5xx/Err):  {stats.failed}")
    print(f" Requests by A:     {stats.served_a}")
    print(f" Requests by B:     {stats.served_b}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Load Balancer CLI Traffic Generator")
    parser.add_argument("--rpm", type=int, default=120, help="Requests per minute (default 120)")
    parser.add_argument("--duration", type=float, default=None, help="Duration in seconds (optional)")
    args = parser.parse_args()

    asyncio.run(run_traffic(args.rpm, args.duration))


if __name__ == "__main__":
    main()
