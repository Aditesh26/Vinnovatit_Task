from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .connections import ConnectionManager
from .room_store import (
    DuplicateParticipantNameError,
    HostAuthorizationError,
    InvalidHostTransferError,
    RoomNotFoundError,
    RoomStore,
)

PACKAGE_DIR = Path(__file__).parent
DRIFT_CORRECTION_THRESHOLD_SECONDS = 0.75
DRIFT_CHECK_INTERVAL_MS = 1_000
DRIFT_CORRECTION_COOLDOWN_MS = 2_500
MAX_CHAT_MESSAGE_LENGTH = 500
ALLOWED_REACTIONS = {"😂", "❤️", "🔥", "👏", "😮"}
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
store = RoomStore()
connections = ConnectionManager()

app = FastAPI(title="Watch Party", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")


class RoomCreate(BaseModel):
    name: str = Field(default="Host", max_length=32)


class RoomJoin(BaseModel):
    name: str = Field(default="Guest", max_length=32)


def room_payload(
    room_code: str, participant_id: str, participant_name: str
) -> dict[str, str]:
    return {
        "code": room_code,
        "participant_id": participant_id,
        "participant_name": participant_name,
        "room_url": f"/rooms/{room_code}",
    }


@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(data: RoomCreate) -> dict[str, str]:
    room, host = store.create_room(data.name)
    return room_payload(room.code, host.id, host.name)


@app.post("/api/rooms/{room_code}/participants", status_code=status.HTTP_201_CREATED)
async def join_room(room_code: str, data: RoomJoin) -> dict[str, str]:
    try:
        participant = store.add_participant(room_code, data.name)
        room = store.get_room(room_code)
    except RoomNotFoundError as error:
        raise HTTPException(status_code=404, detail="Room code not found.") from error
    except DuplicateParticipantNameError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return room_payload(room.code, participant.id, participant.name)


@app.get("/rooms/{room_code}", response_class=HTMLResponse)
async def room_page(request: Request, room_code: str) -> HTMLResponse:
    try:
        room = store.get_room(room_code)
    except RoomNotFoundError as error:
        raise HTTPException(status_code=404, detail="Room code not found.") from error
    return templates.TemplateResponse(
        request,
        "room.html",
        {
            "room": room,
            "room_code": room.code,
            "drift_threshold_seconds": DRIFT_CORRECTION_THRESHOLD_SECONDS,
            "drift_check_interval_ms": DRIFT_CHECK_INTERVAL_MS,
            "drift_correction_cooldown_ms": DRIFT_CORRECTION_COOLDOWN_MS,
        },
    )


@app.websocket("/ws/rooms/{room_code}")
async def room_websocket(websocket: WebSocket, room_code: str) -> None:
    room_code = room_code.upper()
    try:
        store.get_room(room_code)
    except RoomNotFoundError:
        await websocket.close(code=4404, reason="Room code not found")
        return

    participant_id = websocket.query_params.get("participant_id") or str(uuid4())
    participant_name = websocket.query_params.get("name", "Guest")
    try:
        # Resolve identity/name before registering a socket, so a rejected
        # duplicate WebSocket cannot appear in the room's connection manager.
        room, participant, joined = store.connect_participant(
            room_code, participant_id, participant_name
        )
    except DuplicateParticipantNameError as error:
        await websocket.close(code=4409, reason=str(error))
        return
    await connections.connect(room_code, participant_id, websocket)
    await connections.send(websocket, {"type": "room_state", **room.snapshot()})
    if joined:
        await connections.broadcast(
            room_code,
            {"type": "participant_joined", "participant": participant.as_dict()},
        )
        await connections.broadcast(
            room_code, {"type": "room_state", **room.snapshot()}
        )
    await connections.send(websocket, room.playback_snapshot())

    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await connections.send(
                    websocket,
                    {"type": "error", "detail": "Messages must be valid JSON."},
                )
                continue
            if not isinstance(message, dict):
                await connections.send(
                    websocket,
                    {"type": "error", "detail": "Messages must be JSON objects."},
                )
                continue
            if message == {"type": "ping"}:
                await connections.send(websocket, {"type": "pong"})
                continue
            if message.get("type") == "chat_send":
                text = message.get("text")
                if set(message) != {"type", "text"} or not isinstance(text, str):
                    detail = "Chat messages need only a text field."
                else:
                    text = text.strip()
                    if not text:
                        detail = "Chat messages cannot be empty."
                    elif len(text) > MAX_CHAT_MESSAGE_LENGTH:
                        detail = f"Chat messages must be {MAX_CHAT_MESSAGE_LENGTH} characters or fewer."
                    else:
                        participant = store.get_room(room_code).participants.get(participant_id)
                        if participant is None:
                            detail = "Your participant session is unavailable. Rejoin the room."
                        else:
                            await connections.broadcast(room_code, {
                                "type": "chat_message",
                                "message_id": str(uuid4()),
                                "sender_id": participant.id,
                                "sender": participant.name,
                                "text": text,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            continue
                await connections.send(websocket, {"type": "error", "detail": detail})
                continue
            if message.get("type") == "reaction_send":
                emoji = message.get("emoji")
                if set(message) != {"type", "emoji"} or not isinstance(emoji, str) or emoji not in ALLOWED_REACTIONS:
                    await connections.send(websocket, {
                        "type": "error",
                        "detail": "Choose one of the supported room reactions.",
                    })
                    continue
                participant = store.get_room(room_code).participants.get(participant_id)
                if participant is None:
                    await connections.send(websocket, {
                        "type": "error",
                        "detail": "Your participant session is unavailable. Rejoin the room.",
                    })
                    continue
                await connections.broadcast(room_code, {
                    "type": "reaction",
                    "reaction_id": str(uuid4()),
                    "sender_id": participant.id,
                    "sender": participant.name,
                    "emoji": emoji,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                continue
            if message.get("type") == "host_transfer":
                target_id = message.get("target_participant_id")
                if (
                    set(message) != {"type", "target_participant_id"}
                    or not isinstance(target_id, str)
                    or not target_id
                ):
                    await connections.send(websocket, {
                        "type": "error",
                        "detail": "Host transfers need a valid target participant ID.",
                    })
                    continue
                try:
                    transfer_state = store.transfer_host(
                        room_code, participant_id, target_id
                    )
                except (HostAuthorizationError, InvalidHostTransferError) as error:
                    await connections.send(
                        websocket, {"type": "error", "detail": str(error)}
                    )
                    continue
                # The store changes the authoritative host before this room-only
                # broadcast, so every client sees the new role immediately.
                await connections.broadcast(room_code, transfer_state)
                continue
            if message.get("type") != "playback_command":
                await connections.send(
                    websocket, {"type": "error", "detail": "Unsupported message type."}
                )
                continue

            action = message.get("action")
            position = message.get("position")
            valid_position = (
                isinstance(position, (int, float))
                and not isinstance(position, bool)
                and 0 <= position <= 86_400
                and (isinstance(position, int) or math.isfinite(position))
            )
            if (
                set(message) != {"type", "action", "position"}
                or not isinstance(action, str)
                or action not in {"play", "pause", "seek"}
                or not valid_position
            ):
                await connections.send(
                    websocket,
                    {
                        "type": "error",
                        "detail": "Playback commands need action play, pause, or seek and a position from 0 to 86400 seconds.",
                    },
                )
                continue

            # This mutation has no await, so one event-loop task assigns each
            # room revision atomically. Clients discard delayed older revisions.
            try:
                playback_state = store.set_playback_as_host(
                    room_code, participant_id, action, float(position)
                )
            except HostAuthorizationError as error:
                await connections.send(
                    websocket, {"type": "error", "detail": str(error)}
                )
                continue
            await connections.broadcast(room_code, playback_state)
    except WebSocketDisconnect:
        pass
    finally:
        still_connected = connections.disconnect(room_code, participant_id, websocket)
        if not still_connected:
            room = store.disconnect_participant(room_code, participant_id)
            await connections.broadcast(
                room_code,
                {"type": "participant_left", "participant_id": participant_id},
            )
            await connections.broadcast(
                room_code, {"type": "room_state", **room.snapshot()}
            )


def main() -> None:
    uvicorn.run("vinnovatit_task.main:app", host="127.0.0.1", port=8000, reload=True)
