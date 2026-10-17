const roomCode = document.body.dataset.roomCode;
const params = new URLSearchParams(window.location.search);
const storedRoom = JSON.parse(sessionStorage.getItem(`watch-party:${roomCode}`) || "null");
const participantId = params.get("participant_id") || storedRoom?.participant_id || crypto.randomUUID();
const participantName = storedRoom?.participant_name || "Guest";
const status = document.querySelector("#connection-status");
const list = document.querySelector("#participant-list");
const count = document.querySelector("#participant-count");
const message = document.querySelector("#room-message");
const video = document.querySelector("#shared-video");
const playbackNotice = document.querySelector("#playback-notice");
let reconnectTimer;
let socket;
let applyingRemoteState = false;
let authoritativePlayback = null;
let remoteSeekTarget = null;
let pendingPlaybackState = null;

function renderParticipants(participants, hostId) {
    const connected = participants.filter((participant) => participant.connected);
    count.textContent = connected.length;
    list.replaceChildren(...connected.map((participant) => {
        const item = document.createElement("li"); item.textContent = participant.name;
        if (participant.id === hostId) { const label = document.createElement("span"); label.textContent = "Host"; item.append(label); }
        return item;
    }));
}

function connect() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${protocol}://${window.location.host}/ws/rooms/${roomCode}?participant_id=${encodeURIComponent(participantId)}&name=${encodeURIComponent(participantName)}`);
    socket.onopen = () => { status.textContent = "Connected"; status.classList.add("online"); };
    socket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "room_state") renderParticipants(data.participants, data.host_id);
        if (data.type === "playback_state") applyPlaybackState(data);
        if (data.type === "participant_joined") message.textContent = `${data.participant.name} joined the room.`;
        if (data.type === "participant_left") message.textContent = "A participant left the room.";
        if (data.type === "error") message.textContent = data.detail;
    };
    socket.onclose = () => { status.textContent = "Reconnecting..."; status.classList.remove("online"); reconnectTimer = window.setTimeout(connect, 1500); };
}

function sendPlaybackCommand(action) {
    if (applyingRemoteState) return;
    if (action === "play" && authoritativePlayback?.paused === false) return;
    if (action === "pause" && authoritativePlayback?.paused === true) return;
    if (action === "seek" && remoteSeekTarget !== null && Math.abs(video.currentTime - remoteSeekTarget) < 0.5) {
        remoteSeekTarget = null;
        return;
    }
    remoteSeekTarget = null;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        playbackNotice.textContent = "Reconnecting to the room. Playback controls will sync when connected.";
        return;
    }
    socket.send(JSON.stringify({ type: "playback_command", action, position: video.currentTime }));
}

function applyPlaybackState(state) {
    authoritativePlayback = { paused: state.paused, position: state.position, updated_at: state.updated_at };
    pendingPlaybackState = state;
    if (video.readyState < HTMLMediaElement.HAVE_METADATA) return;
    const elapsed = state.paused ? 0 : Math.max(0, Date.now() / 1000 - state.updated_at);
    const position = Math.max(0, state.position + elapsed);
    applyingRemoteState = true;
    if (Math.abs(video.currentTime - position) > 0.5) {
        remoteSeekTarget = position;
        video.currentTime = position;
    }
    if (state.paused) {
        video.pause();
        playbackNotice.textContent = "";
    } else {
        video.play().then(() => {
            playbackNotice.textContent = "";
        }).catch(() => {
            playbackNotice.textContent = "Your browser blocked synced autoplay. Press play in the video controls to start; your action will sync with the room.";
        });
    }
    applyingRemoteState = false;
    pendingPlaybackState = null;
}

video.addEventListener("loadedmetadata", () => {
    if (pendingPlaybackState) applyPlaybackState(pendingPlaybackState);
});
video.addEventListener("error", () => {
    playbackNotice.textContent = "The sample video could not be loaded. Check your connection or try again later.";
});
video.addEventListener("play", () => sendPlaybackCommand("play"));
video.addEventListener("pause", () => sendPlaybackCommand("pause"));
video.addEventListener("seeked", () => sendPlaybackCommand("seek"));

document.querySelector("#copy-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(window.location.href.split("?")[0]); message.textContent = "Invite link copied to your clipboard."; }
    catch { message.textContent = "Copy the link from your browser address bar."; }
});
window.addEventListener("beforeunload", () => window.clearTimeout(reconnectTimer));
connect();
