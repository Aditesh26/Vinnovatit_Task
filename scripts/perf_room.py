"""Small loopback WebSocket load/latency check; requires a running app server."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import time
import os
from urllib.parse import urlencode, urlsplit

import fastapi
import httpx
import uvicorn
import websockets


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


async def receive_json(socket) -> dict[str, object]:
    return json.loads(await socket.recv())


async def expect_type(socket, expected: str) -> None:
    message = await receive_json(socket)
    if message.get("type") != expected:
        raise RuntimeError(f"Expected {expected}, received {message.get('type')!r}")


async def run_check(
    base_url: str, participants: int, command_samples: int
) -> dict[str, object]:
    parsed = urlsplit(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_root = f"{ws_scheme}://{parsed.netloc}"
    sockets = []
    latencies_ms: list[float] = []
    errors: list[str] = []
    delivered = 0
    commands_sent = 0
    action_counts = {"play": 0, "pause": 0, "seek": 0}
    room_code = None

    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        try:
            response = await client.post("/api/rooms", json={"name": "Perf host"})
            response.raise_for_status()
            room = response.json()
            room_code = room["code"]
            host_query = urlencode(
                {
                    "participant_id": room["participant_id"],
                    "name": room["participant_name"],
                }
            )
            host = await websockets.connect(
                f"{ws_root}/ws/rooms/{room_code}?{host_query}", open_timeout=5
            )
            sockets.append(host)
            await host.send(
                json.dumps(
                    {
                        "type": "room_connect",
                        "participant_id": room["participant_id"],
                        "name": room["participant_name"],
                        "recovery_token": room["host_recovery_token"],
                    }
                )
            )
            for expected_type in (
                "room_state",
                "participant_joined",
                "room_state",
                "playback_state",
            ):
                await expect_type(host, expected_type)

            for index in range(1, participants):
                guest_response = await client.post(
                    f"/api/rooms/{room_code}/participants",
                    json={"name": f"Perf guest {index}"},
                )
                guest_response.raise_for_status()
                guest_data = guest_response.json()
                guest_query = urlencode(
                    {
                        "participant_id": guest_data["participant_id"],
                        "name": guest_data["participant_name"],
                    }
                )
                guest = await websockets.connect(
                    f"{ws_root}/ws/rooms/{room_code}?{guest_query}", open_timeout=5
                )
                sockets.append(guest)
                await guest.send(
                    json.dumps(
                        {
                            "type": "room_connect",
                            "participant_id": guest_data["participant_id"],
                            "name": guest_data["participant_name"],
                            "recovery_token": None,
                        }
                    )
                )
                await expect_type(guest, "room_state")

                # Each already connected socket receives one join notice and
                # one room snapshot; the new socket receives those plus its
                # initial state and targeted playback snapshot.
                async def drain_existing(socket) -> None:
                    await expect_type(socket, "participant_joined")
                    await expect_type(socket, "room_state")

                await asyncio.gather(*(drain_existing(socket) for socket in sockets))
                await expect_type(guest, "playback_state")

            for sample in range(command_samples):
                action = ("play", "pause", "seek")[sample % 3]
                command = {
                    "type": "playback_command",
                    "action": action,
                    "position": float(sample + 1),
                }

                async def receive_timed(socket):
                    payload = await receive_json(socket)
                    return payload, time.perf_counter_ns()

                waiters = [
                    asyncio.create_task(receive_timed(socket)) for socket in sockets
                ]
                started_ns = time.perf_counter_ns()
                await host.send(json.dumps(command))
                commands_sent += 1
                action_counts[action] += 1
                try:
                    received = await asyncio.wait_for(
                        asyncio.gather(*waiters), timeout=10
                    )
                except Exception as error:
                    for task in waiters:
                        task.cancel()
                    errors.append(f"sample {sample}: receive failed: {error}")
                    break

                for socket_index, (payload, received_ns) in enumerate(received):
                    if (
                        payload.get("type") != "playback_state"
                        or payload.get("action") != action
                    ):
                        errors.append(
                            f"sample {sample}, socket {socket_index}: unexpected event {payload.get('type')!r}"
                        )
                        continue
                    if socket_index:
                        latencies_ms.append((received_ns - started_ns) / 1_000_000)
                        delivered += 1

        except Exception as error:
            errors.append(f"setup/runtime failure: {type(error).__name__}: {error}")
        finally:
            if sockets:
                await asyncio.gather(
                    *(socket.close() for socket in sockets), return_exceptions=True
                )

    expected_deliveries = commands_sent * max(0, participants - 1)
    return {
        "base_url": base_url,
        "isolated_room_code": room_code,
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "fastapi": fastapi.__version__,
            "uvicorn": uvicorn.__version__,
            "websockets": websockets.__version__,
            "httpx": httpx.__version__,
            "cpu": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
            "participants_requested": participants,
        },
        "connections": {
            "expected": participants,
            "successful": len(sockets),
            "failures": max(0, participants - len(sockets)),
        },
        "playback_commands": commands_sent,
        "action_counts": action_counts,
        "guest_messages": {
            "expected": expected_deliveries,
            "delivered": delivered,
            "failures": max(0, expected_deliveries - delivered),
        },
        "guest_propagation_latency_ms": {
            "sample_count": len(latencies_ms),
            "median": statistics.median(latencies_ms) if latencies_ms else None,
            "p95_nearest_rank": percentile(latencies_ms, 0.95),
            "maximum": max(latencies_ms) if latencies_ms else None,
        },
        "errors": errors,
        "drift": "Not measured: this harness has no browser HTMLMediaElement or decoded video clock.",
        "cleanup": "All WebSocket clients closed; the isolated room remains in server memory until restart.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--participants", type=int, default=10)
    parser.add_argument(
        "--samples", type=int, default=100, help="Number of host commands"
    )
    args = parser.parse_args()
    if args.participants < 2 or args.samples < 1:
        parser.error("--participants must be at least 2 and --samples at least 1")

    result = asyncio.run(
        run_check(args.base_url.rstrip("/"), args.participants, args.samples)
    )
    print(json.dumps(result, indent=2))
    if (
        result["errors"]
        or result["connections"]["failures"]
        or result["guest_messages"]["failures"]
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
