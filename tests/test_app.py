import pytest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vinnovatit_task.main import app, store
from vinnovatit_task.connections import ConnectionManager
from vinnovatit_task.movies import MOVIES, MOVIES_BY_ID
from vinnovatit_task.room_store import (
    DuplicateParticipantNameError,
    HostAuthorizationError,
    InvalidHostTransferError,
    RoomActionError,
    RoomPermissionError,
    RoomStore,
    calculate_playback_drift,
    estimate_playback_position,
)

_host_recovery_tokens: dict[str, str] = {}
_raw_websocket_connect = TestClient.websocket_connect


def create_room(client: TestClient, name: str = "Aditi") -> dict[str, str]:
    response = client.post("/api/rooms", json={"name": name})
    assert response.status_code == 201
    room = response.json()
    _host_recovery_tokens[room["participant_id"]] = room["host_recovery_token"]
    return room


@pytest.fixture(autouse=True)
def authenticate_test_websockets(monkeypatch):
    """Make test sockets follow the browser's first-frame identity protocol."""
    from contextlib import contextmanager

    def connect(client, url, *args, **kwargs):
        raw_context = _raw_websocket_connect(client, url, *args, **kwargs)

        @contextmanager
        def authenticated_context():
            with raw_context as socket:
                query = parse_qs(urlsplit(url).query)
                participant_id = query.get("participant_id", [str(uuid4())])[0]
                name = query.get("name", ["Guest"])[0]
                socket.send_json({
                    "type": "room_connect",
                    "participant_id": participant_id,
                    "name": name,
                    "recovery_token": _host_recovery_tokens.get(participant_id),
                })
                yield socket

        return authenticated_context()

    monkeypatch.setattr(TestClient, "websocket_connect", connect)


def consume_initial_room_messages(socket) -> None:
    """Consume the initial room, presence, and playback messages for a new host."""
    assert socket.receive_json()["type"] == "room_state"
    assert socket.receive_json()["type"] == "participant_joined"
    assert socket.receive_json()["type"] == "room_state"
    assert socket.receive_json()["type"] == "playback_state"


def connect_guest(client: TestClient, room: dict[str, str], participant_name: str = "Guest"):
    guest = client.post(
        f"/api/rooms/{room['code']}/participants", json={"name": participant_name}
    ).json()
    return guest, f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}&name={participant_name}"


def host_ws_url(room: dict[str, str], name: str | None = None) -> str:
    suffix = f"&name={name or room['participant_name']}"
    return (
        f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}{suffix}"
    )


def test_create_room_returns_a_six_character_code_and_share_url() -> None:
    with TestClient(app) as client:
        room = create_room(client)
    assert len(room["code"]) == 6
    assert room["code"].isalnum()
    assert room["room_url"] == f"/rooms/{room['code']}"
    assert room["participant_name"] == "Aditi"
    assert len(room["host_recovery_token"]) >= 40


def test_landing_and_valid_room_pages_render() -> None:
    with TestClient(app) as client:
        assert "Stay in sync." in client.get("/").text
        room = create_room(client)
        room_page = client.get(room["room_url"])
    assert room_page.status_code == 200
    assert room["code"] in room_page.text
    assert 'id="chat-form"' in room_page.text
    assert 'data-emoji="😂"' in room_page.text
    assert 'id="start-synced-playback"' in room_page.text
    assert 'id="movie-catalogue"' in room_page.text
    assert 'id="poll-form"' in room_page.text
    assert 'src="https://media.w3.org/2010/05/sintel/trailer.mp4"' in room_page.text


def test_movie_catalogue_entries_have_browser_video_metadata_and_https_sources() -> None:
    from urllib.parse import urlparse

    assert set(MOVIES_BY_ID) == {"sintel-trailer", "big-buck-bunny", "tears-of-steel"}
    for movie in MOVIES:
        assert all(movie.get(field) for field in (
            "id", "title", "category", "year", "description", "credit", "source_page",
            "video_url", "media_type",
        ))
        url = urlparse(movie["video_url"])
        assert url.scheme == "https"
        assert url.netloc
        assert movie["media_type"] in {"video/mp4", "video/webm"}
        assert url.path.lower().endswith((".mp4", ".webm"))
    assert MOVIES_BY_ID["big-buck-bunny"]["media_type"] == "video/webm"
    assert MOVIES_BY_ID["tears-of-steel"]["media_type"] == "video/webm"


def test_movie_catalogue_supports_title_search_and_category_filter() -> None:
    with TestClient(app) as client:
        all_movies = client.get("/api/movies").json()
        searched = client.get("/api/movies", params={"search": "buck"}).json()
        filtered = client.get("/api/movies", params={"category": "Fantasy"}).json()
    assert len(all_movies["movies"]) >= 3
    assert searched["movies"][0]["id"] == "big-buck-bunny"
    assert all(movie["category"] == "Fantasy" for movie in filtered["movies"])
    assert "video_url" in all_movies["movies"][0]


def test_join_existing_room_and_reject_unknown_code() -> None:
    with TestClient(app) as client:
        room = create_room(client)
        joined = client.post(f"/api/rooms/{room['code']}/participants", json={"name": "Rohan"})
        missing = client.post("/api/rooms/NOPE00/participants", json={"name": "Rohan"})
    assert joined.status_code == 201
    assert joined.json()["code"] == room["code"]
    assert joined.json()["participant_name"] == "Rohan"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Room code not found."


def test_display_names_are_unique_ignoring_case_within_a_room() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        first = client.post(
            f"/api/rooms/{room['code']}/participants", json={"name": "Sad"}
        )
        duplicate = client.post(
            f"/api/rooms/{room['code']}/participants", json={"name": "sAD"}
        )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == (
        "That display name is already in use in this room. Please choose another name."
    )


def test_same_display_name_is_allowed_in_separate_rooms() -> None:
    with TestClient(app) as client:
        first_room = create_room(client, "Host one")
        second_room = create_room(client, "Host two")
        first_join = client.post(
            f"/api/rooms/{first_room['code']}/participants", json={"name": "Sad"}
        )
        second_join = client.post(
            f"/api/rooms/{second_room['code']}/participants", json={"name": "sAD"}
        )

    assert first_join.status_code == 201
    assert second_join.status_code == 201


def test_reconnecting_participant_keeps_id_and_uses_requested_name() -> None:
    store = RoomStore()
    room, host = store.create_room("Host")
    participant = store.add_participant(room.code, "Sad")
    _, connected, first_join, first_name_changed = store.connect_participant(
        room.code, participant.id, "Sad"
    )
    store.disconnect_participant(room.code, participant.id)

    _, reconnected, joined_again, name_changed = store.connect_participant(
        room.code, participant.id, "sAD"
    )

    assert first_join is True
    assert first_name_changed is False
    assert joined_again is True
    assert name_changed is True
    assert connected.id == reconnected.id == participant.id
    assert reconnected.name == "sAD"
    assert host.id != reconnected.id


def test_renaming_existing_participant_rejects_another_participants_name() -> None:
    room_store = RoomStore()
    room, _ = room_store.create_room("Host")
    first = room_store.add_participant(room.code, "Sad")
    second = room_store.add_participant(room.code, "Rohan")
    with pytest.raises(DuplicateParticipantNameError):
        room_store.connect_participant(room.code, second.id, "SAD")
    assert first.name == "Sad"
    assert second.name == "Rohan"


def test_websocket_reconnect_renames_participant_and_broadcasts_update() -> None:
    from urllib.parse import quote

    with TestClient(app) as client:
        room = create_room(client, "Host")
        guest, guest_url = connect_guest(client, room, "Rohan")
        with client.websocket_connect(host_ws_url(room, "Host")) as host_socket:
            consume_initial_room_messages(host_socket)
            with client.websocket_connect(guest_url) as guest_socket:
                assert guest_socket.receive_json()["type"] == "room_state"
                assert host_socket.receive_json()["type"] == "participant_joined"
                assert host_socket.receive_json()["type"] == "room_state"

                renamed_url = (
                    f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}"
                    f"&name={quote('Rohan New')}"
                )
                with client.websocket_connect(renamed_url) as renamed_socket:
                    assert renamed_socket.receive_json()["type"] == "room_state"
                    updated = host_socket.receive_json()
                    assert updated["type"] == "participant_updated"
                    assert updated["participant"]["name"] == "Rohan New"
                    snapshot = host_socket.receive_json()
                    assert snapshot["type"] == "room_state"
                    assert any(
                        participant["name"] == "Rohan New"
                        for participant in snapshot["participants"]
                    )
                    assert renamed_socket.receive_json()["type"] == "participant_updated"
                    assert renamed_socket.receive_json()["type"] == "room_state"
                    assert renamed_socket.receive_json()["type"] == "playback_state"


def test_new_websocket_identity_with_duplicate_name_is_rejected() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        url = host_ws_url(room, "Host")
        with client.websocket_connect(url) as host_socket:
            consume_initial_room_messages(host_socket)
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect(
                    f"/ws/rooms/{room['code']}?participant_id=new-id&name=hOST"
                ) as duplicate_socket:
                    duplicate_socket.receive_json()
    assert error.value.code == 4409


def test_concurrent_join_attempts_cannot_claim_same_display_name() -> None:
    from threading import Barrier

    store = RoomStore()
    room, _ = store.create_room("Host")
    start_together = Barrier(2)

    def attempt_join(name: str) -> str:
        start_together.wait()
        try:
            store.add_participant(room.code, name)
            return "joined"
        except DuplicateParticipantNameError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt_join, ("Sad", "sAD")))

    assert sorted(results) == ["duplicate", "joined"]
    assert sum(participant.name.casefold() == "sad" for participant in room.participants.values()) == 1


def test_room_page_returns_404_for_an_invalid_room_code() -> None:
    with TestClient(app) as client:
        assert client.get("/rooms/NOPE00").status_code == 404


def test_websocket_notifies_participants_in_the_same_room() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        guest = client.post(f"/api/rooms/{room['code']}/participants", json={"name": "Guest"}).json()
        with client.websocket_connect(host_ws_url(room, "Host")) as host_socket:
            initial_state = host_socket.receive_json()
            assert initial_state["type"] == "room_state"
            assert initial_state["participants"][0]["connected"] is True
            host_socket.receive_json()  # host participant_joined
            host_socket.receive_json()  # updated room state
            assert host_socket.receive_json()["type"] == "playback_state"
            with client.websocket_connect(f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}&name=Guest") as guest_socket:
                assert guest_socket.receive_json()["type"] == "room_state"
                joined_event = host_socket.receive_json()
                assert joined_event == {"type": "participant_joined", "participant": {"id": guest["participant_id"], "name": "Guest", "connected": True}}
                updated_state = host_socket.receive_json()
                assert len([p for p in updated_state["participants"] if p["connected"]]) == 2


def test_invalid_websocket_message_returns_an_error_without_ending_connection() -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            socket.receive_json()
            socket.receive_json()
            socket.receive_json()
            assert socket.receive_json()["type"] == "playback_state"
            socket.send_text("not json")
            assert socket.receive_json() == {"type": "error", "detail": "Messages must be valid JSON."}
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


def test_invalid_websocket_room_code_is_closed_cleanly() -> None:
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/rooms/NOPE00"):
                pass
    assert error.value.code == 4404


def test_playback_state_estimates_position_while_playing() -> None:
    room, _ = RoomStore().create_room("Host")

    playing = room.set_playback("play", position=12.5, now=100.0)
    estimated = room.playback_snapshot(now=103.25)

    assert playing == {
        "type": "playback_state",
        "position": 12.5,
        "paused": False,
        "updated_at": 100.0,
        "revision": 1,
        "action": "play",
        "movie": {
            "id": "sintel-trailer",
            "title": "Sintel",
            "category": "Fantasy",
            "year": "2010",
            "description": "A young woman searches for a lost dragon.",
            "video_url": "https://media.w3.org/2010/05/sintel/trailer.mp4",
            "media_type": "video/mp4",
            "credit": "Blender Foundation, CC BY 3.0",
            "source_page": "https://durian.blender.org/",
        },
    }
    assert estimated["position"] == 15.75
    assert estimated["paused"] is False

    paused = room.set_playback("pause", position=15.75, now=104.0)
    assert paused["position"] == 15.75
    assert paused["paused"] is True
    assert paused["revision"] == 2

    resumed = room.set_playback("play", position=20.0, now=105.0)
    seeked_while_playing = room.set_playback("seek", position=7.0, now=106.0)
    assert resumed["paused"] is False
    assert seeked_while_playing["paused"] is False
    assert seeked_while_playing["revision"] == 4


def test_drift_calculation_uses_estimated_authoritative_position() -> None:
    target = estimate_playback_position(20.0, False, updated_at=100.0, now=103.0)
    assert target == 23.0
    assert calculate_playback_drift(23.4, 20.0, False, 100.0, 103.0) == pytest.approx(0.4)
    assert calculate_playback_drift(21.5, 20.0, False, 100.0, 103.0) == pytest.approx(-1.5)
    assert calculate_playback_drift(10.0, 10.0, True, 100.0, 103.0) == 0


def test_play_pause_and_seek_commands_broadcast_authoritative_state() -> None:
    with TestClient(app) as client:
        room = create_room(client)
        url = host_ws_url(room, "Host")
        with client.websocket_connect(url) as first:
            assert first.receive_json()["type"] == "room_state"
            first.receive_json()  # participant_joined
            first.receive_json()  # room_state
            initial_playback = first.receive_json()
            assert initial_playback["paused"] is True

            guest = client.post(f"/api/rooms/{room['code']}/participants", json={"name": "Guest"}).json()
            guest_url = f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}&name=Guest"
            with client.websocket_connect(guest_url) as second:
                assert second.receive_json()["type"] == "room_state"
                first.receive_json()  # participant_joined
                first.receive_json()  # room_state
                second.receive_json()  # participant_joined
                second.receive_json()  # room_state
                assert second.receive_json()["type"] == "playback_state"

                for action, position, paused in [("play", 4.0, False), ("pause", 5.0, True), ("seek", 9.5, True)]:
                    command = {"type": "playback_command", "action": action, "position": position}
                    first.send_json(command)
                    first_state = first.receive_json()
                    second_state = second.receive_json()
                    assert first_state == second_state
                    assert first_state["type"] == "playback_state"
                    assert first_state["position"] == position
                    assert first_state["paused"] is paused
                    assert first_state["revision"] == [1, 2, 3][["play", "pause", "seek"].index(action)]


def test_late_join_receives_current_playback_state() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room, "Host")
        with client.websocket_connect(host_url) as host:
            host.receive_json()
            host.receive_json()
            host.receive_json()
            host.receive_json()
            host.send_json({"type": "playback_command", "action": "play", "position": 31.0})
            current_state = host.receive_json()
            assert current_state["paused"] is False

            with client.websocket_connect(f"/ws/rooms/{room['code']}?participant_id=late-user&name=Late") as late_user:
                late_user.receive_json()  # room_state
                late_user.receive_json()  # participant_joined
                late_user.receive_json()  # updated room_state
                restored = late_user.receive_json()
                assert restored["type"] == "playback_state"
                assert restored["revision"] == current_state["revision"]
                assert restored["paused"] is False
                assert restored["position"] >= 31.0


def test_reconnect_restores_playback_without_duplicating_participant() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        url = host_ws_url(room, "Host")
        with client.websocket_connect(url) as first:
            first.receive_json()
            first.receive_json()
            first.receive_json()
            first.receive_json()
            first.send_json({"type": "playback_command", "action": "play", "position": 14.0})
            playback_state = first.receive_json()

        with client.websocket_connect(url) as reconnected:
            reconnected.receive_json()  # room_state
            reconnected.receive_json()  # participant_joined
            restored_room = reconnected.receive_json()
            restored_playback = reconnected.receive_json()
            assert len(restored_room["participants"]) == 1
            assert restored_room["participants"][0]["id"] == room["participant_id"]
            assert restored_playback["revision"] == playback_state["revision"]
            assert restored_playback["paused"] is False
            assert restored_playback["position"] >= 14.0


def test_replacement_socket_does_not_duplicate_participant_or_drop_presence() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        url = host_ws_url(room)
        with client.websocket_connect(url) as original:
            original.receive_json()  # room_state
            original.receive_json()  # participant_joined
            original.receive_json()  # room_state
            original.receive_json()  # playback_state

            with client.websocket_connect(url) as replacement:
                current_room = replacement.receive_json()
                current_playback = replacement.receive_json()
                assert current_room["type"] == "room_state"
                assert len(current_room["participants"]) == 1
                assert current_room["participants"][0]["connected"] is True
                assert current_playback["type"] == "playback_state"
                replacement.send_json({"type": "ping"})
                assert replacement.receive_json() == {"type": "pong"}


def test_sequential_overlapping_commands_get_distinct_ordered_revisions() -> None:
    room, _ = RoomStore().create_room("Host")

    first = room.set_playback("play", position=6.0, now=100.0)
    second = room.set_playback("seek", position=9.0, now=100.1)

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["action"] == "seek"
    assert second["position"] == 9.0
    assert second["paused"] is False


def test_guest_websocket_commands_cannot_change_host_playback() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room)
        guest = client.post(f"/api/rooms/{room['code']}/participants", json={"name": "Guest"}).json()
        guest_url = f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}"
        with client.websocket_connect(host_url) as host, client.websocket_connect(guest_url) as guest_socket:
            host.receive_json()  # room_state
            host.receive_json()  # participant_joined
            host.receive_json()  # room_state
            host.receive_json()  # playback_state
            guest_socket.receive_json()  # room_state
            host.receive_json()  # guest joined
            host.receive_json()  # updated room_state
            guest_socket.receive_json()  # participant_joined
            guest_socket.receive_json()  # room_state
            guest_socket.receive_json()  # playback_state

            host.send_json({"type": "playback_command", "action": "play", "position": 3.0})
            host_state = host.receive_json()
            guest_state = guest_socket.receive_json()
            assert host_state == guest_state
            assert host_state["revision"] == 1
            assert host_state["paused"] is False

            guest_socket.send_json({"type": "playback_command", "action": "pause", "position": 8.0})
            assert guest_socket.receive_json()["type"] == "error"
            room_state = store.get_room(room["code"])
            assert room_state.playback_position == 3.0
            assert room_state.playback_paused is False


def test_host_transfer_broadcasts_new_host_and_moves_playback_permissions() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room, "Host")
        with client.websocket_connect(host_url) as host_socket:
            consume_initial_room_messages(host_socket)
            guest, guest_url = connect_guest(client, room, "Guest")
            with client.websocket_connect(guest_url) as guest_socket:
                host_socket.receive_json()  # participant_joined
                host_socket.receive_json()  # updated room_state
                for _ in range(4):
                    guest_socket.receive_json()  # initial room/presence/playback state

                host_socket.send_json({"type": "playback_command", "action": "pause", "position": 25.0})
                assert host_socket.receive_json()["type"] == "playback_state"
                assert guest_socket.receive_json()["type"] == "playback_state"
                before_transfer = store.get_room(room["code"])
                prior_revision = before_transfer.playback_revision

                host_socket.send_json({
                    "type": "host_transfer",
                    "target_participant_id": guest["participant_id"],
                })
                host_update = host_socket.receive_json()
                guest_update = guest_socket.receive_json()
                assert host_update == guest_update
                assert host_update["type"] == "host_transferred"
                assert host_update["previous_host_id"] == room["participant_id"]
                assert host_update["host_id"] == guest["participant_id"]
                assert "_private_host_recovery_token" not in host_update
                assert all(p["connected"] for p in host_update["participants"])
                assert store.get_room(room["code"]).playback_position == 25.0
                assert store.get_room(room["code"]).playback_revision == prior_revision
                new_host_credential = guest_socket.receive_json()
                assert new_host_credential["type"] == "host_recovery_credential"
                assert new_host_credential["token"]

                for action, position in (("play", 30.0), ("pause", 35.0), ("seek", 40.0)):
                    host_socket.send_json({
                        "type": "playback_command",
                        "action": action,
                        "position": position,
                    })
                    assert host_socket.receive_json()["type"] == "error"

                    guest_socket.send_json({
                        "type": "playback_command",
                        "action": action,
                        "position": position,
                    })
                    assert host_socket.receive_json()["type"] == "playback_state"
                    new_host_state = guest_socket.receive_json()
                    assert new_host_state["type"] == "playback_state"
                    assert new_host_state["action"] == action
                    assert new_host_state["position"] == position


def test_guest_cannot_transfer_host_privileges() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room)
        with client.websocket_connect(host_url) as host_socket:
            consume_initial_room_messages(host_socket)
            guest, guest_url = connect_guest(client, room, "Guest")
            with client.websocket_connect(guest_url) as guest_socket:
                host_socket.receive_json()
                host_socket.receive_json()
                for _ in range(4):
                    guest_socket.receive_json()

                guest_socket.send_json({
                    "type": "host_transfer",
                    "target_participant_id": guest["participant_id"],
                })
                assert guest_socket.receive_json()["type"] == "error"
                assert store.get_room(room["code"]).host_id == room["participant_id"]
                host_socket.send_json({"type": "ping"})
                assert host_socket.receive_json() == {"type": "pong"}


def test_malformed_and_cross_room_host_transfer_targets_are_rejected() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        other_room = create_room(client, "Other host")
        host_url = host_ws_url(room)
        with client.websocket_connect(host_url) as host_socket:
            consume_initial_room_messages(host_socket)
            for request in (
                {"type": "host_transfer", "target_participant_id": []},
                {"type": "host_transfer", "target_participant_id": "missing", "extra": True},
                {"type": "host_transfer", "target_participant_id": other_room["participant_id"]},
            ):
                host_socket.send_json(request)
                assert host_socket.receive_json()["type"] == "error"
            assert store.get_room(room["code"]).host_id == room["participant_id"]


def test_host_transfer_rejects_self_unknown_and_disconnected_targets() -> None:
    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    disconnected_target = room_store.add_participant(room.code, "Disconnected")

    with pytest.raises(InvalidHostTransferError, match="already the host"):
        room_store.transfer_host(room.code, host.id, host.id)
    with pytest.raises(InvalidHostTransferError, match="currently connected"):
        room_store.transfer_host(room.code, host.id, disconnected_target.id)
    with pytest.raises(InvalidHostTransferError, match="currently connected"):
        room_store.transfer_host(room.code, host.id, "not-a-room-participant")
    assert room.host_id == host.id


def test_concurrent_host_transfers_have_one_authorized_winner() -> None:
    from threading import Barrier

    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    first_target = room_store.add_participant(room.code, "First")
    second_target = room_store.add_participant(room.code, "Second")
    room_store.connect_participant(room.code, first_target.id, first_target.name)
    room_store.connect_participant(room.code, second_target.id, second_target.name)
    start_together = Barrier(2)

    def transfer(target_id: str) -> str:
        start_together.wait()
        try:
            room_store.transfer_host(room.code, host.id, target_id)
            return target_id
        except HostAuthorizationError:
            return "no-longer-host"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(transfer, (first_target.id, second_target.id)))

    assert results.count("no-longer-host") == 1
    assert room.host_id in {first_target.id, second_target.id}


def test_host_disconnect_is_hostless_until_same_identity_reconnects() -> None:
    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    guest = room_store.add_participant(room.code, "Guest")
    room_store.connect_participant(room.code, guest.id, guest.name)

    room_store.disconnect_participant(room.code, host.id)
    assert room.snapshot()["host_id"] is None
    assert room.snapshot()["hostless"] is True
    with pytest.raises(HostAuthorizationError):
        room_store.set_playback_as_host(room.code, guest.id, "play", 10)

    # A refresh and a permanent tab close look identical. Do not transfer the
    # role automatically; the same stable participant ID restores it on reconnect.
    _, reconnected, _, _ = room_store.connect_participant(room.code, host.id, "Host")
    assert reconnected.id == host.id
    assert room.snapshot()["host_id"] == host.id
    assert len(room.participants) == 2


def test_host_recovery_requires_room_scoped_secret_not_participant_id() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        assert "recovery_token" not in host_ws_url(room, "Host")
        with pytest.raises(WebSocketDisconnect) as no_token:
            with _raw_websocket_connect(
                client,
                f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
            ) as socket:
                socket.send_json({"type": "room_connect", "participant_id": room["participant_id"], "name": "Host", "recovery_token": None})
                socket.receive_json()
        assert no_token.value.code == 4403

        other_room = create_room(client, "Other")
        with pytest.raises(WebSocketDisconnect) as wrong_room_token:
            with _raw_websocket_connect(
                client,
                f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
            ) as socket:
                socket.send_json({"type": "room_connect", "participant_id": room["participant_id"], "name": "Host", "recovery_token": other_room["host_recovery_token"]})
                socket.receive_json()
        assert wrong_room_token.value.code == 4403


def test_old_host_reconnects_as_guest_after_host_transfer() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room, "Host")
        guest, guest_url = connect_guest(client, room, "Guest")
        recovery_credential = None
        with client.websocket_connect(host_url) as former_host:
            consume_initial_room_messages(former_host)
            with client.websocket_connect(guest_url) as new_host:
                former_host.receive_json()
                former_host.receive_json()
                for _ in range(4):
                    new_host.receive_json()
                former_host.send_json({"type": "host_transfer", "target_participant_id": guest["participant_id"]})
                former_host.receive_json()
                assert new_host.receive_json()["type"] == "host_transferred"
                credential = new_host.receive_json()
                assert credential["type"] == "host_recovery_credential"
                recovery_credential = credential["token"]
                new_host.close()

        assert recovery_credential
        _host_recovery_tokens[guest["participant_id"]] = recovery_credential
        with client.websocket_connect(guest_url) as recovered_new_host:
            assert recovered_new_host.receive_json()["host_id"] == guest["participant_id"]
            with client.websocket_connect(host_url) as former_reconnected:
                state = former_reconnected.receive_json()
                assert state["host_id"] == guest["participant_id"]
                former_reconnected.receive_json()  # participant_joined
                former_reconnected.receive_json()  # updated room_state
                former_reconnected.receive_json()  # playback_state
                former_reconnected.send_json({"type": "playback_command", "action": "play", "position": 1})
                assert former_reconnected.receive_json()["type"] == "error"
                assert store.get_room(room["code"]).active_host_id == guest["participant_id"]


def test_malformed_playback_commands_are_rejected_and_connection_survives() -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            socket.receive_json()
            socket.receive_json()
            socket.receive_json()
            socket.receive_json()
            for command in (
                {"type": "playback_command", "action": "skip", "position": 1},
                {"type": "playback_command", "action": "seek", "position": True},
                {"type": "playback_command", "action": "play", "position": 86_401},
                {"type": "playback_command", "action": "pause"},
            ):
                socket.send_json(command)
                response = socket.receive_json()
                assert response["type"] == "error"
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


def test_playback_broadcast_is_isolated_to_its_room() -> None:
    with TestClient(app) as client:
        room_one = create_room(client, "One")
        room_two = create_room(client, "Two")
        url_one = host_ws_url(room_one)
        url_two = host_ws_url(room_two)
        with client.websocket_connect(url_one) as first, client.websocket_connect(url_two) as second:
            for socket in (first, second):
                socket.receive_json()  # room_state
                socket.receive_json()  # participant_joined
                socket.receive_json()  # room_state
                socket.receive_json()  # playback_state
            first.send_json({"type": "playback_command", "action": "seek", "position": 21})
            assert first.receive_json()["position"] == 21
            second.send_json({"type": "ping"})
            assert second.receive_json() == {"type": "pong"}


def test_chat_message_is_trimmed_timestamped_and_broadcast_to_room() -> None:
    from datetime import datetime

    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room, "Host")
        with client.websocket_connect(host_url) as host:
            consume_initial_room_messages(host)
            guest, guest_url = connect_guest(client, room)
            with client.websocket_connect(guest_url) as guest_socket:
                host.receive_json()  # participant_joined
                host.receive_json()  # updated room_state
                guest_socket.receive_json()  # initial room_state
                guest_socket.receive_json()  # participant_joined
                guest_socket.receive_json()  # updated room_state
                guest_socket.receive_json()  # playback_state

                host.send_json({"type": "chat_send", "text": "  Hello, room!  "})
                for socket in (host, guest_socket):
                    event = socket.receive_json()
                    assert event["type"] == "chat_message"
                    assert event["sender"] == "Host"
                    assert event["sender_id"] == room["participant_id"]
                    assert event["text"] == "Hello, room!"
                    assert event["message_id"]
                    datetime.fromisoformat(event["timestamp"])


@pytest.mark.parametrize("payload", [
    {"type": "chat_send", "text": ""},
    {"type": "chat_send", "text": "   \n  "},
    {"type": "chat_send", "text": "x" * 501},
    {"type": "chat_send", "text": 42},
    {"type": "chat_send", "text": "hello", "extra": True},
])
def test_invalid_chat_payloads_return_error_and_keep_socket_open(payload) -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            consume_initial_room_messages(socket)
            socket.send_json(payload)
            assert socket.receive_json()["type"] == "error"
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


def test_reaction_broadcasts_only_supported_reaction_to_room() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = host_ws_url(room, "Host")
        with client.websocket_connect(host_url) as host:
            consume_initial_room_messages(host)
            _, guest_url = connect_guest(client, room)
            with client.websocket_connect(guest_url) as guest_socket:
                host.receive_json()  # participant_joined
                host.receive_json()  # updated room_state
                for _ in range(4):
                    guest_socket.receive_json()

                host.send_json({"type": "reaction_send", "emoji": "❤️"})
                for socket in (host, guest_socket):
                    event = socket.receive_json()
                    assert event["type"] == "reaction"
                    assert event["sender"] == "Host"
                    assert event["emoji"] == "❤️"
                    assert event["reaction_id"]


@pytest.mark.parametrize("payload", [
    {"type": "reaction_send", "emoji": "💩"},
    {"type": "reaction_send", "emoji": ["😂"]},
    {"type": "reaction_send", "emoji": "😂", "extra": 1},
])
def test_invalid_reactions_return_error_without_crashing(payload) -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            consume_initial_room_messages(socket)
            socket.send_json(payload)
            assert socket.receive_json()["type"] == "error"
            socket.send_json({"type": "ping"})
            assert socket.receive_json() == {"type": "pong"}


def test_chat_and_reactions_are_isolated_between_rooms() -> None:
    with TestClient(app) as client:
        first = create_room(client, "First")
        second = create_room(client, "Second")
        first_url = host_ws_url(first)
        second_url = host_ws_url(second)
        with client.websocket_connect(first_url) as first_socket, client.websocket_connect(second_url) as second_socket:
            consume_initial_room_messages(first_socket)
            consume_initial_room_messages(second_socket)
            first_socket.send_json({"type": "chat_send", "text": "only room one"})
            assert first_socket.receive_json()["text"] == "only room one"
            first_socket.send_json({"type": "reaction_send", "emoji": "🔥"})
            assert first_socket.receive_json()["type"] == "reaction"
            second_socket.send_json({"type": "ping"})
            assert second_socket.receive_json() == {"type": "pong"}


def test_broadcast_continues_when_one_disconnected_socket_fails() -> None:
    class FakeSocket:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.messages = []

        async def accept(self) -> None:
            pass

        async def send_json(self, message) -> None:
            if self.fail:
                raise RuntimeError("socket is already disconnected")
            self.messages.append(message)

    manager = ConnectionManager()
    failed_socket = FakeSocket(fail=True)
    live_socket = FakeSocket()

    async def broadcast() -> None:
        await manager.connect("ROOM01", "gone", failed_socket)
        await manager.connect("ROOM01", "live", live_socket)
        await manager.broadcast("ROOM01", {"type": "chat_message", "text": "hello"})

    asyncio.run(broadcast())
    assert live_socket.messages == [{"type": "chat_message", "text": "hello"}]


def test_playlist_add_remove_duplicate_validation_and_permissions() -> None:
    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    adder = room_store.add_participant(room.code, "Adder")
    room_store.connect_participant(room.code, adder.id, adder.name)
    guest = room_store.add_participant(room.code, "Guest")
    room_store.connect_participant(room.code, guest.id, guest.name)

    event = room_store.add_playlist_movie(room.code, adder.id, "big-buck-bunny")
    assert event["type"] == "playlist_updated"
    assert event["playlist"][0]["added_by_id"] == adder.id
    assert event["playlist"][0]["title"] == "Big Buck Bunny"
    with pytest.raises(RoomActionError, match="already in"):
        room_store.add_playlist_movie(room.code, guest.id, "big-buck-bunny")
    with pytest.raises(RoomActionError, match="not in the catalogue"):
        room_store.add_playlist_movie(room.code, guest.id, "not-a-movie")
    with pytest.raises(RoomPermissionError, match="Only the participant"):
        room_store.remove_playlist_movie(room.code, guest.id, "big-buck-bunny")

    selected = room_store.select_playlist_movie(room.code, host.id, "big-buck-bunny")
    assert selected["action"] == "select"
    assert selected["movie"]["id"] == "big-buck-bunny"
    assert selected["paused"] is True and selected["position"] == 0
    with pytest.raises(HostAuthorizationError):
        room_store.select_playlist_movie(room.code, guest.id, "big-buck-bunny")
    assert room.playback_revision == selected["revision"]

    room_store.transfer_host(room.code, host.id, guest.id)
    with pytest.raises(HostAuthorizationError):
        room_store.select_playlist_movie(room.code, host.id, "big-buck-bunny")
    selected_by_new_host = room_store.select_playlist_movie(room.code, guest.id, "big-buck-bunny")
    assert selected_by_new_host["movie"]["id"] == "big-buck-bunny"

    removed = room_store.remove_playlist_movie(room.code, adder.id, "big-buck-bunny")
    assert removed["playlist"] == []
    with pytest.raises(RoomActionError, match="not in the shared playlist"):
        room_store.remove_playlist_movie(room.code, host.id, "big-buck-bunny")
    room_store.add_playlist_movie(room.code, adder.id, "tears-of-steel")
    host_removal = room_store.remove_playlist_movie(room.code, guest.id, "tears-of-steel")
    assert host_removal["playlist"] == []


def test_playlist_is_room_scoped_and_concurrent_duplicates_are_rejected() -> None:
    from threading import Barrier

    room_store = RoomStore()
    first, first_host = room_store.create_room("First")
    second, second_host = room_store.create_room("Second")
    for room, host in ((first, first_host), (second, second_host)):
        room_store.connect_participant(room.code, host.id, host.name)
    one = room_store.add_participant(first.code, "One")
    two = room_store.add_participant(first.code, "Two")
    for participant in (one, two):
        room_store.connect_participant(first.code, participant.id, participant.name)
    start_together = Barrier(2)

    def add() -> str:
        start_together.wait()
        try:
            room_store.add_playlist_movie(first.code, one.id, "sintel-trailer")
            return "added"
        except RoomActionError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: add(), range(2)))
    assert sorted(results) == ["added", "duplicate"]
    assert room_store.get_room(first.code).snapshot()["playlist"]
    assert room_store.get_room(second.code).snapshot()["playlist"] == []


def test_poll_validation_votes_and_close_permissions() -> None:
    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    creator = room_store.add_participant(room.code, "Creator")
    voter = room_store.add_participant(room.code, "Voter")
    other = room_store.add_participant(room.code, "Other")
    for participant in (creator, voter, other):
        room_store.connect_participant(room.code, participant.id, participant.name)

    for question, options in (("  ", ["A", "B"]), ("Question", ["Only one"]), ("Q", ["A"] * 6), ("Q", ["A", "a"]), ("Q", ["x" * 81, "B"])):
        with pytest.raises(RoomActionError):
            room_store.create_poll(room.code, creator.id, question, options)

    created = room_store.create_poll(room.code, creator.id, "  Which film?  ", ["Sintel", "Bunny"])
    poll = created["poll"]
    assert poll["question"] == "Which film?"
    assert poll["creator_id"] == creator.id
    first_option, second_option = poll["options"]
    with pytest.raises(RoomActionError, match="valid option"):
        room_store.vote_poll(room.code, voter.id, poll["id"], "missing")
    voted = room_store.vote_poll(room.code, voter.id, poll["id"], first_option["id"])
    assert voted["poll"]["options"][0]["votes"] == 1
    assert room.snapshot(voter.id)["polls"][0]["my_vote"] == first_option["id"]
    assert room.snapshot(other.id)["polls"][0]["my_vote"] is None
    changed = room_store.vote_poll(room.code, voter.id, poll["id"], second_option["id"])
    assert changed["poll"]["options"][0]["votes"] == 0
    assert changed["poll"]["options"][1]["votes"] == 1
    assert room.snapshot(voter.id)["polls"][0]["my_vote"] == second_option["id"]
    repeated = room_store.vote_poll(room.code, voter.id, poll["id"], second_option["id"])
    assert repeated["poll"]["options"][0]["votes"] == 0
    assert repeated["poll"]["options"][1]["votes"] == 1
    with pytest.raises(RoomPermissionError, match="creator or current host"):
        room_store.close_poll(room.code, other.id, poll["id"])
    closed = room_store.close_poll(room.code, creator.id, poll["id"])
    assert closed["poll"]["status"] == "closed"
    with pytest.raises(RoomActionError, match="closed"):
        room_store.vote_poll(room.code, other.id, poll["id"], first_option["id"])
    with pytest.raises(RoomActionError, match="already closed"):
        room_store.close_poll(room.code, creator.id, poll["id"])
    assert room.snapshot(creator.id)["polls"] == []

    host_poll = room_store.create_poll(room.code, creator.id, "Host can close?", ["Yes", "No"])["poll"]
    assert room_store.close_poll(room.code, host.id, host_poll["id"])["poll"]["status"] == "closed"


def test_concurrent_vote_changes_keep_one_active_vote_and_consistent_totals() -> None:
    from threading import Barrier

    room_store = RoomStore()
    room, host = room_store.create_room("Host")
    room_store.connect_participant(room.code, host.id, host.name)
    voter = room_store.add_participant(room.code, "Voter")
    room_store.connect_participant(room.code, voter.id, voter.name)
    poll = room_store.create_poll(room.code, host.id, "Pick one", ["A", "B"])["poll"]
    choices = [option["id"] for option in poll["options"]]
    start_together = Barrier(2)

    def vote(option_id: str) -> str:
        start_together.wait()
        try:
            room_store.vote_poll(room.code, voter.id, poll["id"], option_id)
            return "voted"
        except RoomActionError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(vote, choices))
    assert results == ["voted", "voted"]
    current = room.polls[poll["id"]]
    assert len(current.votes) == 1
    assert sum(option["votes"] for option in current.as_dict()["options"]) == 1


def test_playlist_and_poll_broadcasts_are_isolated_and_restored_on_late_join() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        other_room = create_room(client, "Other host")
        host_url = host_ws_url(room, "Host")
        other_url = host_ws_url(other_room, "Other")
        with client.websocket_connect(host_url) as host_socket, client.websocket_connect(other_url) as other_socket:
            consume_initial_room_messages(host_socket)
            consume_initial_room_messages(other_socket)
            host_socket.send_json({"type": "playlist_add", "movie_id": "sintel-trailer"})
            playlist_event = host_socket.receive_json()
            assert playlist_event["type"] == "playlist_updated"
            host_socket.send_json({"type": "poll_create", "question": "Which?", "options": ["Sintel", "Bunny"]})
            created = host_socket.receive_json()
            assert created["type"] == "poll_created"
            poll = created["poll"]
            host_socket.send_json({"type": "poll_vote", "poll_id": poll["id"], "option_id": poll["options"][0]["id"]})
            updated = host_socket.receive_json()
            assert updated["type"] == "poll_updated"
            assert updated["poll"]["total_votes"] == 1
            assert "my_vote" not in updated["poll"]
            assert host_socket.receive_json()["type"] == "poll_vote_recorded"

            guest, guest_url = connect_guest(client, room, "Late guest")
            with client.websocket_connect(guest_url) as guest_socket:
                initial = guest_socket.receive_json()
                assert initial["type"] == "room_state"
                assert initial["playlist"][0]["movie_id"] == "sintel-trailer"
                restored_poll = initial["polls"][0]
                assert restored_poll["total_votes"] == 1
                assert restored_poll["my_vote"] is None
                host_socket.receive_json()  # participant_joined
                host_socket.receive_json()  # updated room_state
                guest_socket.receive_json()  # participant_joined
                guest_socket.receive_json()  # updated room_state
                guest_socket.receive_json()  # playback_state
                guest_socket.send_json({"type": "poll_vote", "poll_id": poll["id"], "option_id": poll["options"][1]["id"]})
                host_update = host_socket.receive_json()
                guest_update = guest_socket.receive_json()
                assert host_update["poll"]["total_votes"] == guest_update["poll"]["total_votes"] == 2
                vote_ack = guest_socket.receive_json()
                assert vote_ack["type"] == "poll_vote_recorded"
                assert vote_ack["option_id"] == poll["options"][1]["id"]
                assert vote_ack["poll"]["my_vote"] == poll["options"][1]["id"]

                host_socket.send_json({"type": "poll_close", "poll_id": poll["id"]})
                host_closed = host_socket.receive_json()
                guest_closed = guest_socket.receive_json()
                assert host_closed["type"] == guest_closed["type"] == "poll_closed"
                assert host_closed["poll"]["status"] == "closed"

                host_socket.send_json({"type": "playlist_remove", "movie_id": "sintel-trailer"})
                host_playlist = host_socket.receive_json()
                guest_playlist = guest_socket.receive_json()
                assert host_playlist == guest_playlist
                assert host_playlist["type"] == "playlist_updated"
                assert host_playlist["playlist"] == []

            other_socket.send_json({"type": "ping"})
            assert other_socket.receive_json() == {"type": "pong"}


@pytest.mark.parametrize("payload", [
    {"type": "playlist_add"},
    {"type": "playlist_add", "movie_id": "missing"},
    {"type": "poll_create", "question": "?", "options": ["one"]},
    {"type": "poll_create", "question": "?", "options": ["one", 3]},
    {"type": ["poll_create"]},
])
def test_invalid_playlist_and_poll_messages_return_errors(payload) -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(host_ws_url(room)) as socket:
            consume_initial_room_messages(socket)
            socket.send_json(payload)
            assert socket.receive_json()["type"] == "error"
            socket.send_json({"type": "ping"})
            assert socket.receive_json()["type"] == "pong"
