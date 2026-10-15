# Watch Party

A beginner-friendly FastAPI foundation for a real-time college club Watch Party. This first milestone supports room creation, joining by code or invite link, participant presence, and room-scoped WebSocket events.

## Current functionality

- Create a room with a unique six-character code.
- Join an existing room from the landing page or directly through its invite URL.
- Copy a shareable room URL from the room page.
- See the host and live connected-participant list.
- Receive real-time join and leave updates over WebSockets.
- Safely reject invalid room codes and malformed WebSocket messages.

Playback synchronization, recommendations, chat, reactions, playlists, polls, and host transfer are intentionally deferred to later milestones.

## Project structure

```text
src/vinnovatit_task/
  main.py          # FastAPI routes, page rendering, and WebSocket endpoint
  room_store.py    # replaceable in-memory room and participant state
  connections.py   # room-scoped WebSocket registry and broadcasts
  templates/       # landing and room pages
  static/          # vanilla JavaScript and responsive CSS
tests/test_app.py  # HTTP and WebSocket integration tests
```

## Setup and run

This project uses [uv](https://docs.astral.sh/uv/). From the project folder:

```powershell
uv sync --group dev
uv run uvicorn vinnovatit_task.main:app --reload
```

Open http://127.0.0.1:8000 in a browser. Alternatively, run `uv run vinnovatit-task`.

## Tests

```powershell
uv run pytest
```

The tests cover room creation, successful and invalid joins, an invalid room page, WebSocket participant events, and malformed WebSocket messages.

## Reliability and performance notes

Room state is held in process memory and every broadcast is scoped to its room code. Disconnects mark participants offline and notify remaining sockets. The browser reconnects after a disconnect; the same participant ID is reused for that browser session.

No formal latency, 10-participant load, or playback-drift measurements have been run for this milestone, so none of the project performance targets are claimed as met. The in-memory store also means rooms are lost when the server restarts and cannot be shared by multiple server processes.

## Next milestone

Add an HTML5 video player with host-authoritative play, pause, seek, and late-join state synchronization. Then add periodic timestamps and a measured drift-correction strategy before claiming playback reliability targets.
