import pytest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vinnovatit_task.main import app, store
from vinnovatit_task.connections import ConnectionManager
from vinnovatit_task.room_store import (
    DuplicateParticipantNameError,
    HostAuthorizationError,
    InvalidHostTransferError,
    RoomStore,
    calculate_playback_drift,
    estimate_playback_position,
)


def create_room(client: TestClient, name: str = "Aditi") -> dict[str, str]:
    response = client.post("/api/rooms", json={"name": name})
    assert response.status_code == 201
    return response.json()


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
    assert 'id="chat-form"' in room_page.text
    assert 'data-emoji="😂"' in room_page.text
    assert 'id="start-synced-playback"' in room_page.text


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


def test_reconnecting_participant_keeps_existing_identity_and_name() -> None:
    store = RoomStore()
    room, host = store.create_room("Host")
    participant = store.add_participant(room.code, "Sad")
    _, connected, first_join = store.connect_participant(
        room.code, participant.id, "Sad"
    )
    store.disconnect_participant(room.code, participant.id)

    _, reconnected, joined_again = store.connect_participant(
        room.code, participant.id, "sAD"
    )

    assert first_join is True
    assert joined_again is True
    assert connected.id == reconnected.id == participant.id
    assert reconnected.name == "Sad"
    assert host.id != reconnected.id


def test_new_websocket_identity_with_duplicate_name_is_rejected() -> None:
    with TestClient(app) as client:
        room = create_room(client, "Host")
        url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
        with client.websocket_connect(url) as host_socket:
            consume_initial_room_messages(host_socket)
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect(
                    f"/ws/rooms/{room['code']}?participant_id=new-id&name=hOST"
                ):
                    pass
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


def test_guest_websocket_commands_cannot_change_host_playback() -> None:
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
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
                assert all(p["connected"] for p in host_update["participants"])
                assert store.get_room(room["code"]).playback_position == 25.0
                assert store.get_room(room["code"]).playback_revision == prior_revision

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
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
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
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}"
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
    _, reconnected, _ = room_store.connect_participant(room.code, host.id, "Host")
    assert reconnected.id == host.id
    assert room.snapshot()["host_id"] == host.id
    assert len(room.participants) == 2


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


def test_chat_message_is_trimmed_timestamped_and_broadcast_to_room() -> None:
    from datetime import datetime

    with TestClient(app) as client:
        room = create_room(client, "Host")
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
        host_url = f"/ws/rooms/{room['code']}?participant_id={room['participant_id']}&name=Host"
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
        first_url = f"/ws/rooms/{first['code']}?participant_id={first['participant_id']}"
        second_url = f"/ws/rooms/{second['code']}?participant_id={second['participant_id']}"
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
