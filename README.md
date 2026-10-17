# Watch Party

A beginner-friendly FastAPI foundation for a real-time college club Watch Party. The project currently supports room creation, joining by code or invite link, participant presence, and basic room-scoped HTML5 playback synchronization.

## Current functionality

- Create a room with a unique six-character code.
- Join an existing room from the landing page or directly through its invite URL.
- Copy a shareable room URL from the room page.
- See the host and live connected-participant list.
- Receive real-time join and leave updates over WebSockets.
- Safely reject invalid room codes and malformed WebSocket messages.
- Synchronize play, pause, and seek controls through room-scoped playback commands.

Movie discovery, chat, reactions, playlists, polls, and host transfer are intentionally deferred to later milestones.

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

The tests cover room creation, successful and invalid joins, page rendering, WebSocket participant events, playback state timing and commands, malformed messages, and room isolation.

## Playback behavior

The video element uses the W3C hosted [Sintel trailer](https://media.w3.org/2010/05/sintel/trailer.mp4), an HTML5 media test asset also used in W3C examples. Sintel is a Blender Foundation open movie distributed under [Creative Commons Attribution 3.0](https://durian.blender.org/about/); the page credits the film and links to its official project site. The stream is hosted externally, so playback depends on that host remaining available and reachable from each participant's browser. Browser codec support and autoplay rules can also vary.

Browsers send `playback_command` JSON with an `action` (`play`, `pause`, or `seek`) and a numeric `position` in seconds. The FastAPI WebSocket handler validates each command, updates the room's server-side playback record, and broadcasts `playback_state` to sockets registered to that room. State carries a base position, paused flag, and server update time; while playing, clients can estimate the current position from that timestamp. Remote updates are applied without echoing them as new commands. A browser that blocks synced autoplay displays a prompt to use the player's play control.

## Reliability and performance notes

Room and playback state are held in process memory and every broadcast is scoped to its room code. Disconnects mark participants offline and notify remaining sockets. The browser reconnects after a disconnect; the same participant ID is reused for that browser session. A newly connected WebSocket receives the current playback state, but reconnection and late-join synchronization have not been measured as a feature and do not include drift correction.

No formal latency, 10-participant load, or playback-drift measurements have been run for this milestone, so none of the project performance targets are claimed as met. The in-memory store also means rooms are lost when the server restarts and cannot be shared by multiple server processes. Playback commands accept a maximum position of 24 hours; the server does not know the media duration, and there is no host-only permission model or automatic drift correction.

## Next milestone

Add periodic playback checks and a measured drift-correction strategy before claiming playback reliability targets.
