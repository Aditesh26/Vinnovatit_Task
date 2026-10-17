# Watch Party

A beginner-friendly FastAPI foundation for a real-time college club Watch Party. The project currently supports room creation, joining by code or invite link, participant presence, and basic room-scoped HTML5 playback synchronization.

## Current functionality

- Create a room with a unique six-character code.
- Join an existing room from the landing page or directly through its invite URL.
- Copy the room's six-character invite code.
- See the host and live connected-participant list.
- Receive real-time join and leave updates over WebSockets.
- Enforce case-insensitive, room-local unique participant display names.
- Safely reject invalid room codes and malformed WebSocket messages.
- Synchronize play, pause, and seek controls through room-scoped playback commands.
- Let the active host transfer ownership to another connected participant.
- Send room-scoped live chat messages and a small set of emoji reactions.
- Search and category-filter a small curated movie catalogue.
- Maintain a room-scoped shared playlist and create/vote/close group polls.

The movie catalogue is static and curated for this project; it is not live movie data.

## Project structure

```text
src/vinnovatit_task/
  main.py          # FastAPI routes, page rendering, and WebSocket endpoint
  room_store.py    # replaceable in-memory room, playlist, poll, and participant state
  movies.py        # static curated movie catalogue and title/category search
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
node tests/test_room_ui.mjs
```

The tests cover room creation, successful and invalid joins, page rendering, WebSocket participant events, playback state timing and host authorization, host transfer and reconnect behavior, chat/reaction validation and broadcasts, movie search, playlist and poll rules, malformed messages, disconnected send failures, concurrent room actions, and room isolation. The JavaScript test protects the local autoplay fallback behavior.

## Playback behavior

The video element uses the W3C hosted [Sintel trailer](https://media.w3.org/2010/05/sintel/trailer.mp4), an HTML5 media test asset also used in W3C examples. Sintel is a Blender Foundation open movie distributed under [Creative Commons Attribution 3.0](https://durian.blender.org/about/); the page credits the film and links to its official project site. The stream is hosted externally, so playback depends on that host remaining available and reachable from each participant's browser. Browser codec support and autoplay rules can also vary.

Browsers send `playback_command` JSON with an `action` (`play`, `pause`, or `seek`) and a numeric `position` in seconds. The FastAPI WebSocket handler validates each command, updates the room's server-side playback record, and broadcasts `playback_state` to sockets registered to that room. State carries a base position, paused flag, server update time, and increasing room revision. Mutations are synchronous on the event loop, and clients ignore delayed messages with older revisions so overlapping broadcasts cannot roll playback back. New and reconnected sockets receive one targeted snapshot; their joining does not rebroadcast playback state to the room.

The browser checks drift once per second and seeks to the estimated server position when the difference exceeds 0.75 seconds. The threshold, check interval, and 2.5-second correction cooldown are passed to the page from constants in `src/vinnovatit_task/main.py`. This avoids seeking for small differences and limits repeated corrections. A remote explicit seek and a first state after connection are applied immediately. Drift checks pause while video metadata is loading, the player is buffering, paused, seeking, or the WebSocket is disconnected. The estimate uses the server timestamp and the browser's wall clock, so clock skew between devices can affect it. If synced autoplay is blocked for a guest, the page offers a “Click to start synchronized playback” button. Its click seeks to the latest server-estimated position and starts only that browser's video; the client sends playback-changing WebSocket commands only for the current host. A paused server state keeps the guest paused even if a stale click arrives. Playback rate is applied from a rate field if the server provides one; the current protocol does not provide synchronized rate changes.

## Reliability and performance notes

Room and playback state are held in process memory and every broadcast is scoped to its room code. Disconnects mark participants offline and notify remaining sockets. The browser reconnects after a disconnect; the same participant ID is reused for that browser session. New and reconnected sockets receive the current playback snapshot, with browser drift checks correcting large position differences. These behaviors have automated protocol and calculation tests, but have not been measured in real browsers under network or load conditions.

No formal latency, 10-participant load, or post-correction playback-drift measurements have been run, so none of the project performance targets are claimed as met. The in-memory store also means rooms are lost when the server restarts and cannot be shared by multiple server processes. Playback commands accept a maximum position of 24 hours; the server does not know the media duration.

## Chat and reactions

Chat is delivered live to sockets connected to the same room; messages are not stored, so a participant who is disconnected or joins later does not receive chat history. The server uses the participant name already held in the room store (rather than trusting a sender name from the message) and attaches an ISO-8601 UTC timestamp and unique event ID. Text is trimmed and must contain 1–500 characters after trimming. The browser also trims and checks this limit, but server validation remains authoritative.

Browser-to-server WebSocket messages:

```json
{"type":"chat_send","text":"Hello everyone"}
{"type":"reaction_send","emoji":"👏"}
```

The only accepted reaction values are `😂`, `❤️`, `🔥`, `👏`, and `😮`. Successful server-to-room events have type `chat_message` or `reaction`; they include sender identity, event ID, and UTC timestamp (chat events also include the trimmed text). Invalid JSON, unsupported types, extra/missing fields, empty/overlong text, and unsupported reactions receive an `{ "type": "error", "detail": "..." }` response. Rejected messages do not close the connection. The room connection manager handles a send failing on a closed socket without preventing sends to other participants.

## Participant display names

Display names are trimmed and compared with Unicode-aware `casefold()` within a room, so `Sad`, `sad`, and `SAD` collide. A conflicting HTTP join receives status 409 and a message asking the user to choose another name. The same name can be used in a different room. A participant's stored room membership reserves their name while temporarily disconnected: this protects automatic reconnection from colliding with a newly created identity. The WebSocket URL carries the participant ID returned at join time; a known ID reconnects to its existing participant record and keeps its original name. New WebSocket identities are checked by the same store rule and rejected with close code 4409 if their requested name is taken. A `threading.Lock` protects the uniqueness check plus insertion, including requests arriving concurrently on different threads.

## Host transfer and permissions

Only the active host sees “Make host” buttons next to other connected participants. The browser sends `{"type":"host_transfer","target_participant_id":"..."}` over its room WebSocket. The server ignores any client-supplied host flag or username: it compares the WebSocket participant ID with the room's authoritative host ID, then verifies the target belongs to the same room and is connected. Self-transfer, malformed requests, guests, unknown IDs, and disconnected targets are rejected without changing the host. The store performs authorization and ownership mutation together under its lock; it changes the host before broadcasting a `host_transferred` room update. Playback state and position are not modified by transfer. Each play, pause, or seek command is separately authorized against the current active host, so the old host loses control and the new host gains it as soon as the transfer is applied.

The current WebSocket protocol cannot distinguish a browser refresh from a permanent departure. On disconnect, the room is advertised as hostless and playback controls are unavailable, but ownership is not transferred or discarded. The original participant ID remains the owner internally; reconnecting with that same ID restores host status immediately. If the host never reconnects, the room remains hostless for its lifetime, and a participant cannot claim ownership automatically. No arbitrary timeout is used. Participant IDs are the existing session identity mechanism, not a full account/authentication system; production deployments would need stronger identity credentials.

## Movie discovery, shared playlists, and polls

`GET /api/movies` returns the curated catalogue and its category list. Optional `search` matches a title substring without case sensitivity; optional `category` filters by category. The catalogue is a small Python tuple in `src/vinnovatit_task/movies.py`: it contains stable IDs, title, category, year, a short description, playback URL, media type, and credit. It is static, not live movie data. The Sintel default is served from W3C as MP4; Big Buck Bunny and Tears of Steel use Wikimedia Commons 360p VP9 WebM transcodes of the Blender Foundation open movies. Their files are 35.8 MB and 55.4 MB respectively, and therefore can take time to buffer on slow connections. All three require an internet connection, and the WebM entries require browser WebM/VP9 support.

The previously configured Big Buck Bunny and Tears of Steel URLs used the Google GTV sample bucket, which returned HTTP 403 with `application/xml` during verification. On 17 October 2026, read-only `curl.exe -I -L` checks followed redirects and confirmed Sintel returns HTTP 200 `video/mp4`, and the new Big Buck Bunny and Tears of Steel URLs each return HTTP 200 `video/webm`. These checks confirm availability and media MIME type at that time, not that every browser/network can play every byte range. The movie source and license links are shown beneath the player after selection.

Browser-to-server WebSocket messages:

```json
{"type":"playlist_add","movie_id":"sintel-trailer"}
{"type":"playlist_remove","movie_id":"sintel-trailer"}
{"type":"playlist_select","movie_id":"sintel-trailer"}
{"type":"poll_create","question":"Which film?","options":["Sintel","Big Buck Bunny"]}
{"type":"poll_vote","poll_id":"...","option_id":"..."}
{"type":"poll_close","poll_id":"..."}
```

Any connected room participant can add a catalogue movie. IDs are checked against the server catalogue and duplicate IDs are rejected. The participant who added an entry or the current host may remove it. Only the current connected host may select a movie, and it must already be in that room's playlist. Selection uses a server-owned catalogue URL, sets the authoritative playback state to paused at time zero, increments its revision, and broadcasts `playback_state` with the selected movie metadata. The normal host-only `playback_command` authorization remains in force.

Playlist changes are broadcast as `playlist_updated` with the full playlist and a monotonically increasing `playlist_revision`. Initial `room_state` messages contain the playlist, so late joiners and reconnecting participants restore it. Every room owns separate in-memory playlist state.

Poll questions are trimmed and limited to 1-160 characters. A poll needs 2-5 distinct, non-empty options, each at most 80 characters. The form has a labelled Remove button beside every option. Those buttons are disabled at the two-option minimum; Add option is disabled at five. Adding/removing rows renumbers labels without changing other option values. These are form-only controls: deleting an option before submission sends no WebSocket message and cannot alter an existing poll. The creator and each option receive stable UUIDs. Each participant can vote once per poll; votes cannot be changed. The store tracks participant-to-option votes and sends updated totals, but does not include voter identities or choices in room-wide updates. The voter receives a targeted `poll_vote_recorded` acknowledgement so their page can mark their choice. The poll creator or current connected host can close a poll; closed polls reject further votes. Poll creation, totals, and closure are broadcast as `poll_created`, `poll_updated`, and `poll_closed`. Only open polls are included in initial room state; already-closed polls are not persisted as history for late joiners.

Malformed messages, unknown movie/poll/option IDs, duplicates, invalid option counts/text, repeat votes, closed polls, and unauthorized removals/closures/selections receive an `{ "type": "error", "detail": "..." }` event without closing the socket. `threading.Lock` protects every mutation so simultaneous conflicting requests resolve in a single order. Poll and playlist events are broadcast through the room-scoped connection manager, not to other rooms. User-authored question and option text is rendered with DOM `textContent`, not HTML injection.

## Manual playlist and poll check

1. Open one room in two browser windows. Search movie titles and change the category filter; try a title with no match.
2. Add a movie from each window and confirm both playlist views update. Add the same movie again and confirm the server rejects the duplicate. The adder and host can remove entries; another guest cannot.
3. As a guest, attempt `playlist_select` from the browser console/WebSocket and confirm an error with no playback change. As host, select a queued movie and confirm both windows load it paused at the beginning; then play/pause/seek using the usual host controls.
4. In the poll form, add options until Add option disables at 5. Remove the first, a middle, and the last option and confirm the remaining text stays intact and labels renumber. At 2 options, confirm both Remove buttons are disabled. Create a poll and confirm the form resets to 2 options. Submit invalid counts, blank options, repeated options, an overlong question, and an overlong option through the WebSocket; expect errors.
5. Vote from both participants and confirm totals update. Try voting twice from the same participant and confirm the second vote is rejected. Confirm a participant cannot close someone else's poll unless they are current host; creator and host can close it, after which voting is rejected.
6. Refresh a window and confirm the playlist and active poll totals return. Its own prior vote is shown as already submitted. Create a second room and verify its playlist and polls start empty.

These features are functionally covered by automated tests but have not been measured under concurrent browser load. No latency, participant capacity, or playback drift target is claimed as met.

## Manual host-transfer check

1. Start the app and open the same room in two browser windows, one as host and one as guest.
2. Confirm only the host window shows video controls and a “Make host” action beside the guest.
3. Pause playback at a known position, then transfer host to the guest. Confirm both participant lists update, the new host gets controls, the previous host loses them, and playback remains paused at the same position.
4. Try play, pause, and seek from the old host using the browser controls; the server should reject these commands. Confirm each action works from the new host.
5. From a guest, send a `host_transfer` command in the WebSocket DevTools panel; expect an error and no ownership change. Try a nonexistent/disconnected target and a self-transfer from the host; each should be rejected.
6. Refresh the current host window. Other participants should see the room as hostless while it reconnects; the same participant session should regain host controls after reconnect. The room does not auto-transfer on disconnect.

## Manual chat/reaction check

1. Start the app, open the landing page in two browser windows, create a room in one, and join it from the other using the invite link or code.
2. Send a message from each window. Confirm both windows show the sender, local display time, and message once. Try leading/trailing spaces and confirm they are removed.
3. Submit an empty message and a message longer than 500 characters through the browser console or WebSocket client; the server should return an error and keep the room connected. A blank form submission is also blocked in the UI.
4. Click each reaction button and confirm the sender/emoji appears in both windows below the message list, not over the video controls. Reactions disappear after a few seconds.
5. Create a second room in a third window and verify messages/reactions from the first room do not appear there.
6. Close one room window, send a message from the remaining participant, and confirm the room remains usable. Reopen the disconnected window: chat history is intentionally not restored, while room presence and playback continue to use their existing reconnect behavior.

Chat and reaction delivery has automated protocol tests but has not been measured in browser/network load conditions. No multi-user performance target is claimed as met.

## Next milestone

Measure correction results across browsers and network conditions before claiming playback reliability targets.
