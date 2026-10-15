"""In-memory room state, kept independent from the web layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import choice
from string import ascii_uppercase, digits
from uuid import uuid4

ROOM_CODE_ALPHABET = ascii_uppercase + digits


@dataclass
class Participant:
    id: str
    name: str
    connected: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {"id": self.id, "name": self.name, "connected": self.connected}


@dataclass
class Room:
    code: str
    host_id: str
    participants: dict[str, Participant] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {"code": self.code, "host_id": self.host_id, "participants": [p.as_dict() for p in self.participants.values()]}


class RoomNotFoundError(KeyError):
    """Raised when a requested room code does not exist."""


class RoomStore:
    """A small, process-local implementation of the room repository."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def create_room(self, host_name: str) -> tuple[Room, Participant]:
        code = self._new_code()
        host = Participant(id=str(uuid4()), name=self._clean_name(host_name, "Host"))
        room = Room(code=code, host_id=host.id, participants={host.id: host})
        self._rooms[code] = room
        return room, host

    def get_room(self, code: str) -> Room:
        room = self._rooms.get(code.upper())
        if room is None:
            raise RoomNotFoundError(code)
        return room

    def add_participant(self, code: str, name: str) -> Participant:
        room = self.get_room(code)
        participant = Participant(id=str(uuid4()), name=self._clean_name(name, "Guest"))
        room.participants[participant.id] = participant
        return participant

    def connect_participant(self, code: str, participant_id: str, name: str) -> tuple[Room, Participant, bool]:
        room = self.get_room(code)
        participant = room.participants.get(participant_id)
        if participant is None:
            participant = Participant(id=participant_id, name=self._clean_name(name, "Guest"))
            room.participants[participant.id] = participant
        was_connected = participant.connected
        participant.connected = True
        return room, participant, not was_connected

    def disconnect_participant(self, code: str, participant_id: str) -> Room:
        room = self.get_room(code)
        participant = room.participants.get(participant_id)
        if participant is not None:
            participant.connected = False
        return room

    def _new_code(self) -> str:
        while True:
            code = "".join(choice(ROOM_CODE_ALPHABET) for _ in range(6))
            if code not in self._rooms:
                return code

    @staticmethod
    def _clean_name(name: str, fallback: str) -> str:
        clean_name = name.strip()[:32]
        return clean_name or fallback
