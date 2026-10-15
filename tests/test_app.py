import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vinnovatit_task.main import app


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
            with client.websocket_connect(f"/ws/rooms/{room['code']}?participant_id={guest['participant_id']}&name=Guest") as guest_socket:
                assert guest_socket.receive_json()["type"] == "room_state"
                joined_event = host_socket.receive_json()
                assert joined_event == {"type": "participant_joined", "participant": {"id": guest["participant_id"], "name": "Guest", "connected": True}}
                updated_state = host_socket.receive_json()
                assert len([p for p in updated_state["participants"] if p["connected"]]) == 2
            left_event = host_socket.receive_json()
            assert left_event == {"type": "participant_left", "participant_id": guest["participant_id"]}


def test_invalid_websocket_message_returns_an_error_without_ending_connection() -> None:
    with TestClient(app) as client:
        room = create_room(client)
        with client.websocket_connect(f"/ws/rooms/{room['code']}") as socket:
            socket.receive_json()
            socket.receive_json()
            socket.receive_json()
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
