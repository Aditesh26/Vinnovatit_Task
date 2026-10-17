from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, WebSocket]] = defaultdict(dict)

    async def connect(
        self, room_code: str, participant_id: str, websocket: WebSocket
    ) -> None:
        await websocket.accept()
        existing = self._connections[room_code].get(participant_id)
        self._connections[room_code][participant_id] = websocket
        if existing is not None and existing is not websocket:
            await existing.close(code=4001, reason="Connected from another tab")

    def disconnect(
        self, room_code: str, participant_id: str, websocket: WebSocket
    ) -> bool:
        room_connections = self._connections.get(room_code)
        if not room_connections:
            return False
        if room_connections.get(participant_id) is websocket:
            del room_connections[participant_id]
        if not room_connections:
            del self._connections[room_code]
            return False
        return participant_id in room_connections

    async def send(self, websocket: WebSocket, message: dict[str, object]) -> None:
        try:
            await websocket.send_json(message)
        except (RuntimeError, OSError, WebSocketDisconnect):
            pass

    async def send_to_participant(
        self, room_code: str, participant_id: str, message: dict[str, object]
    ) -> None:
        """Deliver a private event without exposing it to the room broadcast."""
        socket = self._connections.get(room_code, {}).get(participant_id)
        if socket is not None:
            await self.send(socket, message)

    async def broadcast(self, room_code: str, message: dict[str, object]) -> None:
        await asyncio.gather(
            *(
                self.send(socket, message)
                for socket in list(self._connections.get(room_code, {}).values())
            )
        )
