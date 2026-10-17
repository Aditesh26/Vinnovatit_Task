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
video.controls = false;
const playbackNotice = document.querySelector("#playback-notice");
const startSyncedPlaybackButton = document.querySelector("#start-synced-playback");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const chatMessages = document.querySelector("#chat-messages");
const reactionFeed = document.querySelector("#reaction-feed");
const socialStatus = document.querySelector("#social-status");
const MAX_CHAT_LENGTH = 500;
const DRIFT_THRESHOLD_SECONDS = Number(document.body.dataset.driftThresholdSeconds) || 0.75;
const DRIFT_CHECK_INTERVAL_MS = Number(document.body.dataset.driftCheckIntervalMs) || 1000;
const DRIFT_CORRECTION_COOLDOWN_MS = Number(document.body.dataset.driftCorrectionCooldownMs) || 2500;
let reconnectTimer;
let socket;
let applyingRemoteState = false;
let localGesturePlayEventPending = false;
let authoritativePlayback = null;
let remoteSeekTarget = null;
let pendingPlaybackState = null;
let pendingForceCorrection = false;
let restoreOnNextPlaybackState = true;
let lastDriftCorrectionAt = 0;
let currentHostId = null;

function renderParticipants(participants, hostId) {
    currentHostId = hostId;
    video.controls = hostId === participantId;
    if (video.controls) hideSyncedPlaybackButton();
    if (hostId === null) message.textContent = "Room is hostless until the original host reconnects.";
    else if (message.textContent.startsWith("Room is hostless")) message.textContent = "";
    const connected = participants.filter((participant) => participant.connected);
    count.textContent = connected.length;
    list.replaceChildren(...connected.map((participant) => {
        const item = document.createElement("li"); item.textContent = participant.name;
        if (participant.id === hostId) { const label = document.createElement("span"); label.textContent = "Host"; item.append(label); }
        else if (hostId === participantId) {
            const transferButton = document.createElement("button");
            transferButton.type = "button";
            transferButton.className = "transfer-host";
            transferButton.textContent = "Make host";
            transferButton.addEventListener("click", () => sendHostTransfer(participant.id));
            item.append(transferButton);
        }
        return item;
    }));
}

function hideSyncedPlaybackButton() {
    startSyncedPlaybackButton.hidden = true;
    startSyncedPlaybackButton.disabled = true;
}

function showSyncedPlaybackButton() {
    if (!authoritativePlayback || authoritativePlayback.paused || currentHostId === participantId) {
        hideSyncedPlaybackButton();
        return;
    }
    startSyncedPlaybackButton.hidden = false;
    startSyncedPlaybackButton.disabled = video.readyState < HTMLMediaElement.HAVE_METADATA || !socket || socket.readyState !== WebSocket.OPEN;
    playbackNotice.textContent = startSyncedPlaybackButton.disabled
        ? "The video or room connection is still loading. Try again when it is ready."
        : "Your browser needs a click to start the room's synchronized playback.";
}

function startSynchronizedPlaybackFromGesture() {
    const state = authoritativePlayback;
    if (!state || state.paused) {
        hideSyncedPlaybackButton();
        if (state) applyPlaybackState(state, true);
        return;
    }
    if (currentHostId === participantId) {
        hideSyncedPlaybackButton();
        return;
    }
    if (video.readyState < HTMLMediaElement.HAVE_METADATA || !socket || socket.readyState !== WebSocket.OPEN) {
        showSyncedPlaybackButton();
        return;
    }

    const targetPosition = estimateAuthoritativePosition(state);
    if (Number.isFinite(state.playback_rate)) video.playbackRate = state.playback_rate;
    if (Math.abs(video.currentTime - targetPosition) > 0.02) {
        seekToAuthoritativePosition(targetPosition);
    } else {
        remoteSeekTarget = null;
    }
    // This is a local, user-gesture playback attempt only. sendPlaybackCommand
    // also checks the current host role, so its media events cannot mutate room state.
    applyingRemoteState = true;
    localGesturePlayEventPending = true;
    let playPromise;
    try {
        playPromise = video.play();
    } catch {
        applyingRemoteState = false;
        localGesturePlayEventPending = false;
        showSyncedPlaybackButton();
        return;
    }
    applyingRemoteState = false;
    Promise.resolve(playPromise).then(() => {
        // A pause may arrive while the user-gesture play promise is pending.
        // Reapply the newest authoritative state instead of letting an old
        // promise restart a room that is now paused.
        if (authoritativePlayback?.paused) {
            applyPlaybackState(authoritativePlayback, true);
            return;
        }
        hideSyncedPlaybackButton();
        playbackNotice.textContent = "";
    }).catch(() => {
        localGesturePlayEventPending = false;
        showSyncedPlaybackButton();
    });
}

function sendHostTransfer(targetParticipantId) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        message.textContent = "Room connection is unavailable. Reconnect before transferring the host role.";
        return;
    }
    socket.send(JSON.stringify({ type: "host_transfer", target_participant_id: targetParticipantId }));
}

function sendSocialCommand(command) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        socialStatus.textContent = "Room connection is unavailable. Reconnecting...";
        return false;
    }
    try {
        socket.send(JSON.stringify(command));
        socialStatus.textContent = "";
        return true;
    } catch {
        socialStatus.textContent = "Could not send that message. Check your room connection.";
        return false;
    }
}

function renderChatMessage(data) {
    if (typeof data.sender !== "string" || typeof data.text !== "string" || typeof data.timestamp !== "string") return;
    const item = document.createElement("li");
    item.className = "chat-message";
    const header = document.createElement("div");
    header.className = "chat-message-header";
    const sender = document.createElement("strong");
    sender.textContent = data.sender;
    const time = document.createElement("time");
    const parsedTime = new Date(data.timestamp);
    time.dateTime = data.timestamp;
    time.textContent = Number.isNaN(parsedTime.getTime()) ? "" : parsedTime.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const text = document.createElement("p");
    text.textContent = data.text;
    header.append(sender, time);
    item.append(header, text);
    chatMessages.append(item);
    while (chatMessages.children.length > 100) chatMessages.firstElementChild.remove();
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function renderReaction(data) {
    if (typeof data.sender !== "string" || typeof data.emoji !== "string") return;
    const item = document.createElement("span");
    item.className = "reaction-notice";
    item.textContent = `${data.emoji} ${data.sender}`;
    reactionFeed.append(item);
    window.setTimeout(() => item.remove(), 5000);
}

function connect() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    restoreOnNextPlaybackState = true;
    socket = new WebSocket(`${protocol}://${window.location.host}/ws/rooms/${roomCode}?participant_id=${encodeURIComponent(participantId)}&name=${encodeURIComponent(participantName)}`);
    socket.onopen = () => {
        status.textContent = "Connected";
        status.classList.add("online");
        if (!startSyncedPlaybackButton.hidden) showSyncedPlaybackButton();
        if (playbackNotice.textContent.startsWith("Reconnecting")) playbackNotice.textContent = "";
    };
    socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); }
        catch { message.textContent = "Received an unreadable room update."; return; }
        if (data.type === "room_state") renderParticipants(data.participants, data.host_id);
        if (data.type === "host_transferred") {
            renderParticipants(data.participants, data.host_id);
            message.textContent = data.host_id === participantId ? "You are now the host." : "The host role was transferred.";
        }
        if (data.type === "playback_state") receivePlaybackState(data);
        if (data.type === "participant_joined") message.textContent = `${data.participant.name} joined the room.`;
        if (data.type === "participant_left") message.textContent = "A participant left the room.";
        if (data.type === "chat_message") renderChatMessage(data);
        if (data.type === "reaction") renderReaction(data);
        if (data.type === "error") socialStatus.textContent = data.detail;
    };
    socket.onclose = () => {
        status.textContent = "Reconnecting...";
        status.classList.remove("online");
        if (!startSyncedPlaybackButton.hidden) showSyncedPlaybackButton();
        playbackNotice.textContent = "Room connection lost. Playback will resync when reconnected.";
        reconnectTimer = window.setTimeout(connect, 1500);
    };
}

function sendPlaybackCommand(action) {
    if (applyingRemoteState || currentHostId !== participantId) return;
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
        playback_rate: Number.isFinite(state.playback_rate) ? state.playback_rate : undefined,
        updated_at: state.updated_at,
        revision,
        action: state.action,
    };
    pendingPlaybackState = authoritativePlayback;
    pendingForceCorrection = forceCorrection;
    if (authoritativePlayback.paused) hideSyncedPlaybackButton();
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
        hideSyncedPlaybackButton();
        video.pause();
        if (!mustSeek) playbackNotice.textContent = "";
    } else {
        if (Number.isFinite(state.playback_rate)) video.playbackRate = state.playback_rate;
        video.play().then(() => {
            hideSyncedPlaybackButton();
            if (!playbackNotice.textContent.startsWith("Room connection")) playbackNotice.textContent = "";
        }).catch(() => {
            if (authoritativePlayback?.revision !== state.revision) return;
            if (currentHostId !== participantId) showSyncedPlaybackButton();
            else playbackNotice.textContent = "Your browser blocked synced autoplay. Press play in the video controls to start.";
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
    if (!startSyncedPlaybackButton.hidden) showSyncedPlaybackButton();
});
video.addEventListener("waiting", () => { playbackNotice.textContent = "Buffering video..."; });
video.addEventListener("canplay", () => {
    if (playbackNotice.textContent === "Buffering video...") playbackNotice.textContent = "";
});
video.addEventListener("error", () => {
    hideSyncedPlaybackButton();
    playbackNotice.textContent = "The sample video could not be loaded. Check your connection or try again later.";
});
video.addEventListener("play", () => {
    if (localGesturePlayEventPending) {
        localGesturePlayEventPending = false;
        return;
    }
    sendPlaybackCommand("play");
});
video.addEventListener("pause", () => sendPlaybackCommand("pause"));
video.addEventListener("seeked", () => {
    if (remoteSeekTarget !== null && Math.abs(video.currentTime - remoteSeekTarget) <= 0.5) {
        remoteSeekTarget = null;
        return;
    }
    remoteSeekTarget = null;
    sendPlaybackCommand("seek");
});

chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = chatInput.value.trim();
    if (!text) {
        socialStatus.textContent = "Write a message before sending.";
        return;
    }
    if (text.length > MAX_CHAT_LENGTH) {
        socialStatus.textContent = `Messages must be ${MAX_CHAT_LENGTH} characters or fewer.`;
        return;
    }
    if (sendSocialCommand({ type: "chat_send", text })) chatInput.value = "";
});

document.querySelectorAll(".reaction-button").forEach((button) => {
    button.addEventListener("click", () => {
        sendSocialCommand({ type: "reaction_send", emoji: button.dataset.emoji });
    });
});
window.setInterval(checkPlaybackDrift, DRIFT_CHECK_INTERVAL_MS);
startSyncedPlaybackButton.addEventListener("click", startSynchronizedPlaybackFromGesture);

function copyUsingBrowserFallback(text) {
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.left = "-9999px";
    document.body.append(field);
    field.select();
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } finally {
        field.remove();
    }
    if (!copied) throw new Error("Clipboard copy was not permitted.");
}

document.querySelector("#copy-code").addEventListener("click", async () => {
    try {
        if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(roomCode);
        } else {
            copyUsingBrowserFallback(roomCode);
        }
        message.textContent = "Invite code copied to your clipboard.";
    } catch {
        try {
            copyUsingBrowserFallback(roomCode);
            message.textContent = "Invite code copied to your clipboard.";
        } catch {
            message.textContent = `Copy this invite code: ${roomCode}`;
        }
    }
});
window.addEventListener("beforeunload", () => window.clearTimeout(reconnectTimer));
connect();
