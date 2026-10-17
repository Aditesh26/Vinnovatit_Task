"""In-memory room state, kept independent from the web layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import choice
from string import ascii_uppercase, digits
from threading import Lock
from time import time
from uuid import uuid4

ROOM_CODE_ALPHABET = ascii_uppercase + digits


def estimate_playback_position(
    position: float, paused: bool, updated_at: float, now: float
) -> float:
    """Estimate the authoritative playhead at a given wall-clock time."""
    if paused:
        return position
    return position + max(0.0, now - updated_at)


def calculate_playback_drift(
    local_position: float,
    position: float,
    paused: bool,
    updated_at: float,
    now: float,
) -> float:
    """Return signed local-minus-authoritative drift in seconds."""
    return local_position - estimate_playback_position(position, paused, updated_at, now)


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
    playback_position: float = 0.0
    playback_paused: bool = True
    playback_updated_at: float = field(default_factory=time)
    playback_revision: int = 0
    playback_action: str = "pause"

    @property
    def active_host_id(self) -> str | None:
        host = self.participants.get(self.host_id)
        return self.host_id if host is not None and host.connected else None

    def snapshot(self) -> dict[str, object]:
        return {
            "code": self.code,
            # The owner is retained internally while disconnected so the same
            # session can regain host status on reconnect. Clients see no active
            # host until that identity reconnects.
            "host_id": self.active_host_id,
            "hostless": self.active_host_id is None,
            "participants": [p.as_dict() for p in self.participants.values()],
        }

    def playback_snapshot(self, now: float | None = None) -> dict[str, object]:
        """Return playback state with an estimated position at the snapshot time."""
        snapshot_time = time() if now is None else now
        return {
            "type": "playback_state",
            "position": estimate_playback_position(
                self.playback_position,
                self.playback_paused,
                self.playback_updated_at,
                snapshot_time,
            ),
            "paused": self.playback_paused,
            "updated_at": snapshot_time,
            "revision": self.playback_revision,
            "action": self.playback_action,
        }

    def set_playback(self, action: str, position: float, now: float | None = None) -> dict[str, object]:
        """Apply one validated command and return the new server state."""
        updated_at = time() if now is None else now
        self.playback_position = position
        if action == "play":
            self.playback_paused = False
        elif action == "pause":
            self.playback_paused = True
        self.playback_updated_at = updated_at
        self.playback_revision += 1
        self.playback_action = action
        return self.playback_snapshot(updated_at)


class RoomNotFoundError(KeyError):
    """Raised when a requested room code does not exist."""


class DuplicateParticipantNameError(ValueError):
    """Raised when a room already has a participant using the requested name."""


class HostAuthorizationError(PermissionError):
    """Raised when a participant tries an action reserved for the active host."""


class InvalidHostTransferError(ValueError):
    """Raised when a host transfer target is invalid."""


class RoomStore:
    """A small, process-local implementation of the room repository."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        # Join requests may arrive on different server threads. Keep the
        # uniqueness check and participant insertion as one operation.
        self._participant_lock = Lock()

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
        with self._participant_lock:
            room = self.get_room(code)
            clean_name = self._clean_name(name, "Guest")
            self._ensure_name_available(room, clean_name)
            participant = Participant(id=str(uuid4()), name=clean_name)
            room.participants[participant.id] = participant
            return participant

    def connect_participant(self, code: str, participant_id: str, name: str) -> tuple[Room, Participant, bool]:
        with self._participant_lock:
            room = self.get_room(code)
            participant = room.participants.get(participant_id)
            if participant is None:
                clean_name = self._clean_name(name, "Guest")
                self._ensure_name_available(room, clean_name)
                participant = Participant(id=participant_id, name=clean_name)
                room.participants[participant.id] = participant
            was_connected = participant.connected
            participant.connected = True
            return room, participant, not was_connected

    def disconnect_participant(self, code: str, participant_id: str) -> Room:
        with self._participant_lock:
            room = self.get_room(code)
            participant = room.participants.get(participant_id)
            if participant is not None:
                participant.connected = False
            return room

    def transfer_host(
        self, code: str, sender_id: str, target_id: str
    ) -> dict[str, object]:
        """Atomically authorize and apply a host transfer to a connected member."""
        with self._participant_lock:
            room = self.get_room(code)
            if room.active_host_id != sender_id:
                raise HostAuthorizationError("Only the current connected host can transfer host privileges.")
            if target_id == sender_id:
                raise InvalidHostTransferError("You are already the host. Choose another participant.")
            target = room.participants.get(target_id)
            if target is None or not target.connected:
                raise InvalidHostTransferError("Choose a participant who is currently connected to this room.")

            previous_host_id = room.host_id
            room.host_id = target_id
            return {
                "type": "host_transferred",
                "previous_host_id": previous_host_id,
                **room.snapshot(),
            }

    def set_playback(self, code: str, action: str, position: float) -> dict[str, object]:
        """Update playback for one room and return its authoritative state."""
        return self.get_room(code).set_playback(action, position)

    def set_playback_as_host(
        self, code: str, participant_id: str, action: str, position: float
    ) -> dict[str, object]:
        """Authorize the active host and update playback as one store operation."""
        with self._participant_lock:
            room = self.get_room(code)
            if room.active_host_id != participant_id:
                raise HostAuthorizationError("Only the current connected host can control playback.")
            return room.set_playback(action, position)

    def _new_code(self) -> str:
        while True:
            code = "".join(choice(ROOM_CODE_ALPHABET) for _ in range(6))
            if code not in self._rooms:
                return code

    @staticmethod
    def _ensure_name_available(room: Room, name: str) -> None:
        requested_name = name.casefold()
        if any(participant.name.casefold() == requested_name for participant in room.participants.values()):
            raise DuplicateParticipantNameError(
                "That display name is already in use in this room. Please choose another name."
            )

    @staticmethod
    def _clean_name(name: str, fallback: str) -> str:
        clean_name = name.strip()[:32]
        return clean_name or fallback
