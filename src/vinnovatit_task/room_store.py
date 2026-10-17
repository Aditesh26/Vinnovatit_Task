"""In-memory room state, kept independent from the web layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import choice
from string import ascii_uppercase, digits
from threading import Lock
from time import time
from uuid import uuid4

from .movies import MOVIES_BY_ID

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
    playlist: list[dict[str, object]] = field(default_factory=list)
    playlist_revision: int = 0
    polls: dict[str, "RoomPoll"] = field(default_factory=dict)
    current_movie_id: str = "sintel-trailer"

    @property
    def active_host_id(self) -> str | None:
        host = self.participants.get(self.host_id)
        return self.host_id if host is not None and host.connected else None

    def snapshot(self, participant_id: str | None = None) -> dict[str, object]:
        return {
            "code": self.code,
            # The owner is retained internally while disconnected so the same
            # session can regain host status on reconnect. Clients see no active
            # host until that identity reconnects.
            "host_id": self.active_host_id,
            "hostless": self.active_host_id is None,
            "participants": [p.as_dict() for p in self.participants.values()],
            "playlist": [dict(entry) for entry in self.playlist],
            "playlist_revision": self.playlist_revision,
            "polls": [poll.as_dict(participant_id) for poll in self.polls.values() if poll.open],
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
            "movie": dict(MOVIES_BY_ID[self.current_movie_id]),
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


@dataclass
class RoomPoll:
    id: str
    question: str
    creator_id: str
    creator_name: str
    options: list[dict[str, str]]
    votes: dict[str, str] = field(default_factory=dict)
    open: bool = True
    revision: int = 0

    def as_dict(self, participant_id: str | None = None) -> dict[str, object]:
        totals = {
            option["id"]: sum(voted_option == option["id"] for voted_option in self.votes.values())
            for option in self.options
        }
        result: dict[str, object] = {
            "id": self.id,
            "question": self.question,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "options": [dict(option, votes=totals[option["id"]]) for option in self.options],
            "status": "open" if self.open else "closed",
            "total_votes": len(self.votes),
            "revision": self.revision,
        }
        if participant_id is not None:
            result["my_vote"] = self.votes.get(participant_id)
        return result


class RoomNotFoundError(KeyError):
    """Raised when a requested room code does not exist."""


class DuplicateParticipantNameError(ValueError):
    """Raised when a room already has a participant using the requested name."""


class HostAuthorizationError(PermissionError):
    """Raised when a participant tries an action reserved for the active host."""


class InvalidHostTransferError(ValueError):
    """Raised when a host transfer target is invalid."""


class RoomActionError(ValueError):
    """Raised when a playlist or poll request is invalid for the room state."""


class RoomPermissionError(PermissionError):
    """Raised when a participant lacks permission for a room action."""


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

    def add_playlist_movie(self, code: str, participant_id: str, movie_id: str) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            participant = self._connected_participant(room, participant_id)
            movie = MOVIES_BY_ID.get(movie_id)
            if movie is None:
                raise RoomActionError("That movie is not in the catalogue.")
            if any(entry["movie_id"] == movie_id for entry in room.playlist):
                raise RoomActionError("That movie is already in the shared playlist.")
            room.playlist.append({
                "movie_id": movie_id,
                "title": movie["title"],
                "category": movie["category"],
                "year": movie["year"],
                "description": movie["description"],
                "added_by_id": participant.id,
                "added_by_name": participant.name,
            })
            room.playlist_revision += 1
            return {"type": "playlist_updated", "playlist_revision": room.playlist_revision, "playlist": [dict(entry) for entry in room.playlist]}

    def remove_playlist_movie(self, code: str, participant_id: str, movie_id: str) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            participant = self._connected_participant(room, participant_id)
            entry = next((item for item in room.playlist if item["movie_id"] == movie_id), None)
            if entry is None:
                raise RoomActionError("That movie is not in the shared playlist.")
            if entry["added_by_id"] != participant_id and room.active_host_id != participant_id:
                raise RoomPermissionError("Only the participant who added a movie or the host can remove it.")
            room.playlist.remove(entry)
            room.playlist_revision += 1
            return {"type": "playlist_updated", "playlist_revision": room.playlist_revision, "playlist": [dict(item) for item in room.playlist]}

    def select_playlist_movie(self, code: str, participant_id: str, movie_id: str) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            if room.active_host_id != participant_id:
                raise HostAuthorizationError("Only the current connected host can select a movie for playback.")
            if not any(entry["movie_id"] == movie_id for entry in room.playlist):
                raise RoomActionError("Choose a movie that is in the shared playlist.")
            room.current_movie_id = movie_id
            room.playback_position = 0.0
            room.playback_paused = True
            room.playback_updated_at = time()
            room.playback_revision += 1
            room.playback_action = "select"
            return room.playback_snapshot()

    def create_poll(self, code: str, participant_id: str, question: str, options: list[str]) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            participant = self._connected_participant(room, participant_id)
            clean_question = question.strip()
            clean_options = [option.strip() for option in options]
            if not 1 <= len(clean_question) <= 160:
                raise RoomActionError("Poll questions must contain 1 to 160 characters.")
            if not 2 <= len(clean_options) <= 5:
                raise RoomActionError("Polls need between 2 and 5 options.")
            if any(not option or len(option) > 80 for option in clean_options):
                raise RoomActionError("Poll options must contain 1 to 80 characters.")
            if len({option.casefold() for option in clean_options}) != len(clean_options):
                raise RoomActionError("Poll options must be different.")
            poll = RoomPoll(
                id=str(uuid4()), question=clean_question, creator_id=participant.id,
                creator_name=participant.name,
                options=[{"id": str(uuid4()), "text": option} for option in clean_options],
            )
            room.polls[poll.id] = poll
            return {"type": "poll_created", "poll": poll.as_dict()}

    def vote_poll(self, code: str, participant_id: str, poll_id: str, option_id: str) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            self._connected_participant(room, participant_id)
            poll = room.polls.get(poll_id)
            if poll is None:
                raise RoomActionError("That poll does not exist in this room.")
            if not poll.open:
                raise RoomActionError("This poll is closed.")
            if not any(option["id"] == option_id for option in poll.options):
                raise RoomActionError("Choose a valid option for this poll.")
            if participant_id in poll.votes:
                raise RoomActionError("You have already voted in this poll.")
            poll.votes[participant_id] = option_id
            poll.revision += 1
            # Vote identity and choice stay private; the handler sends a
            # separate acknowledgement only to this voter's WebSocket.
            return {"type": "poll_updated", "poll": poll.as_dict()}

    def close_poll(self, code: str, participant_id: str, poll_id: str) -> dict[str, object]:
        with self._participant_lock:
            room = self.get_room(code)
            participant = self._connected_participant(room, participant_id)
            poll = room.polls.get(poll_id)
            if poll is None:
                raise RoomActionError("That poll does not exist in this room.")
            if participant_id != poll.creator_id and room.active_host_id != participant_id:
                raise RoomPermissionError("Only the poll creator or current host can close this poll.")
            if not poll.open:
                raise RoomActionError("This poll is already closed.")
            poll.open = False
            poll.revision += 1
            return {"type": "poll_closed", "poll": poll.as_dict()}

    @staticmethod
    def _connected_participant(room: Room, participant_id: str) -> Participant:
        participant = room.participants.get(participant_id)
        if participant is None or not participant.connected:
            raise RoomActionError("Your participant session is unavailable. Rejoin the room.")
        return participant

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
