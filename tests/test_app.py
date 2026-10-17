import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vinnovatit_task.main import app
from vinnovatit_task.room_store import (
    RoomStore,
    calculate_playback_drift,
    estimate_playback_position,
)


def create_room(client: TestClient, name: str = "Aditi") -> dict[str, str]:
    response = client.post("/api/rooms", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_create_room_returns_a_six_character_code_and_share_url() -> None:
    with TestClient(app) as client:
        room = create_room(client)
    assert len(room["code"]) == 6
    assert room["code"].isalnum()
    assert room["room_url"] == f"/rooms/{room['code']}"
    assert room["participant_name"] == "Aditi"


def test_landing_and_valid_room_pages_render() -> None:
    with TestClient(app) as client:
        assert "Stay in sync." in client.get("/").text
        room = create_room(client)
        room_page = client.get(room["room_url"])
    assert room_page.status_code == 200
    assert room["code"] in room_page.text


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


def test_room_page_returns_404_for_an_invalid_room_code() -> None:
    with TestClient(app) as client:
        assert client.get("/rooms/NOPE00").status_code == 404


def test_websocket_notifies_participants_in_the_same_room() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        guest = client.post(f"/api/rooms/{room['code']}/participants", json={"name": "Guest"}).json()
        with client.websocket_connect(f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host") as host_socket:
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
        url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
        url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
        url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
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


def test_concurrent_websocket_commands_get_distinct_room_revisions() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
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

            commands = (
                {"type": "playback_command", "action": "play", "position": 3.0},
                {"type": "playback_command", "action": "pause", "position": 8.0},
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_send = pool.submit(host.send_json, commands[0])
                second_send = pool.submit(guest_socket.send_json, commands[1])
                first_send.result()
                second_send.result()

            host_states = [host.receive_json(), host.receive_json()]
            guest_states = [guest_socket.receive_json(), guest_socket.receive_json()]
            assert sorted(state["revision"] for state in host_states) == [1, 2]
            assert sorted(state["revision"] for state in guest_states) == [1, 2]
            for state in host_states + guest_states:
                assert (state["paused"] is True) == (state["action"] == "pause")


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
        url_one = f"/ws/rooms/{room_one['code']}?participant_id={room_one['participant_id']}"
        url_two = f"/ws/rooms/{room_two['code']}?participant_id={room_two['participant_id']}"
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
