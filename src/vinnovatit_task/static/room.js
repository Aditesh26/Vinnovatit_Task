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
const DRIFT_THRESHOLD_SECONDS = Number(document.body.dataset.driftThresholdSeconds) || 0.75;
const DRIFT_CHECK_INTERVAL_MS = Number(document.body.dataset.driftCheckIntervalMs) || 1000;
const DRIFT_CORRECTION_COOLDOWN_MS = Number(document.body.dataset.driftCorrectionCooldownMs) || 2500;
let reconnectTimer;
let socket;
let applyingRemoteState = false;
let authoritativePlayback = null;
let remoteSeekTarget = null;
let pendingPlaybackState = null;
let pendingForceCorrection = false;
let restoreOnNextPlaybackState = true;
let lastDriftCorrectionAt = 0;

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
    restoreOnNextPlaybackState = true;
    socket = new WebSocket(`${protocol}://${window.location.host}/ws/rooms/${roomCode}?participant_id=${encodeURIComponent(participantId)}&name=${encodeURIComponent(participantName)}`);
    socket.onopen = () => {
        status.textContent = "Connected";
        status.classList.add("online");
        if (playbackNotice.textContent.startsWith("Reconnecting")) playbackNotice.textContent = "";
    };
    socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); }
        catch { message.textContent = "Received an unreadable room update."; return; }
        if (data.type === "room_state") renderParticipants(data.participants, data.host_id);
        if (data.type === "playback_state") receivePlaybackState(data);
        if (data.type === "participant_joined") message.textContent = `${data.participant.name} joined the room.`;
        if (data.type === "participant_left") message.textContent = "A participant left the room.";
        if (data.type === "error") message.textContent = data.detail;
    };
    socket.onclose = () => {
        status.textContent = "Reconnecting...";
        status.classList.remove("online");
        playbackNotice.textContent = "Room connection lost. Playback will resync when reconnected.";
        reconnectTimer = window.setTimeout(connect, 1500);
    };
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

function receivePlaybackState(state) {
    const revision = Number(state.revision);
    if (!Number.isInteger(revision) || !Number.isFinite(state.updated_at) || !Number.isFinite(state.position)) return;
    if (authoritativePlayback) {
        if (revision < authoritativePlayback.revision) return;
        if (revision === authoritativePlayback.revision && state.updated_at <= authoritativePlayback.updated_at) return;
    }
    const forceCorrection = restoreOnNextPlaybackState || state.action === "seek";
    restoreOnNextPlaybackState = false;
    authoritativePlayback = {
        paused: state.paused,
        position: state.position,
        updated_at: state.updated_at,
        revision,
        action: state.action,
    };
    pendingPlaybackState = authoritativePlayback;
    pendingForceCorrection = forceCorrection;
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
        applyPlaybackState(authoritativePlayback, forceCorrection);
    }
}

function estimateAuthoritativePosition(state, nowMs = Date.now()) {
    const elapsed = state.paused ? 0 : Math.max(0, nowMs / 1000 - state.updated_at);
    const position = Math.max(0, state.position + elapsed);
    return Number.isFinite(video.duration) ? Math.min(position, video.duration) : position;
}

function calculatePlaybackDrift(localPosition, state, nowMs = Date.now()) {
    return localPosition - estimateAuthoritativePosition(state, nowMs);
}

function seekToAuthoritativePosition(position) {
    try {
        remoteSeekTarget = position;
        video.currentTime = position;
        return true;
    } catch {
        remoteSeekTarget = null;
        playbackNotice.textContent = "The video is not ready to seek yet. It will resync when loading finishes.";
        return false;
    }
}

function applyPlaybackState(state, forceCorrection = false) {
    if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
        pendingPlaybackState = state;
        pendingForceCorrection = pendingForceCorrection || forceCorrection;
        return;
    }
    const target = estimateAuthoritativePosition(state);
    const drift = calculatePlaybackDrift(video.currentTime, state);
    const mustSeek = forceCorrection || state.action === "seek" || Math.abs(drift) > DRIFT_THRESHOLD_SECONDS;
    applyingRemoteState = true;
    if (mustSeek && Math.abs(drift) > 0.02) seekToAuthoritativePosition(target);
    if (state.paused) {
        video.pause();
        if (!mustSeek) playbackNotice.textContent = "";
    } else {
        video.play().then(() => {
            if (!playbackNotice.textContent.startsWith("Room connection")) playbackNotice.textContent = "";
        }).catch(() => {
            playbackNotice.textContent = "Your browser blocked synced autoplay. Press play in the video controls to start; your action will sync with the room.";
        });
    }
    applyingRemoteState = false;
    pendingPlaybackState = null;
    pendingForceCorrection = false;
}

function checkPlaybackDrift() {
    if (!authoritativePlayback || authoritativePlayback.paused || video.paused || video.seeking) return;
    if (video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA || !socket || socket.readyState !== WebSocket.OPEN) return;
    const drift = calculatePlaybackDrift(video.currentTime, authoritativePlayback);
    if (Math.abs(drift) <= DRIFT_THRESHOLD_SECONDS) return;
    const now = performance.now();
    if (now - lastDriftCorrectionAt < DRIFT_CORRECTION_COOLDOWN_MS) return;
    const target = estimateAuthoritativePosition(authoritativePlayback);
    if (seekToAuthoritativePosition(target)) {
        lastDriftCorrectionAt = now;
        playbackNotice.textContent = `Playback resynchronized (${Math.abs(drift).toFixed(1)}s drift).`;
    }
}

video.addEventListener("loadedmetadata", () => {
    if (pendingPlaybackState) applyPlaybackState(pendingPlaybackState, pendingForceCorrection);
});
video.addEventListener("waiting", () => { playbackNotice.textContent = "Buffering video..."; });
video.addEventListener("canplay", () => {
    if (playbackNotice.textContent === "Buffering video...") playbackNotice.textContent = "";
});
video.addEventListener("error", () => {
    playbackNotice.textContent = "The sample video could not be loaded. Check your connection or try again later.";
});
video.addEventListener("play", () => sendPlaybackCommand("play"));
video.addEventListener("pause", () => sendPlaybackCommand("pause"));
video.addEventListener("seeked", () => {
    if (remoteSeekTarget !== null && Math.abs(video.currentTime - remoteSeekTarget) <= 0.5) {
        remoteSeekTarget = null;
        return;
    }
    remoteSeekTarget = null;
    sendPlaybackCommand("seek");
});
window.setInterval(checkPlaybackDrift, DRIFT_CHECK_INTERVAL_MS);

document.querySelector("#copy-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(window.location.href.split("?")[0]); message.textContent = "Invite link copied to your clipboard."; }
    catch { message.textContent = "Copy the link from your browser address bar."; }
});
window.addEventListener("beforeunload", () => window.clearTimeout(reconnectTimer));
connect();
