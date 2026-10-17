const roomCode = document.body.dataset.roomCode;
const params = new URLSearchParams(window.location.search);
const normalizedRoomCode = roomCode.trim().toUpperCase();
const identityStorageKey = `watch-party:identity:${normalizedRoomCode}`;
const recoveryStorageKey = `watch-party:host-recovery:${normalizedRoomCode}`;
function readStoredIdentity() {
    try {
        const saved = JSON.parse(localStorage.getItem(identityStorageKey) || "null");
        if (saved && typeof saved.participant_id === "string" && saved.participant_id
            && typeof saved.participant_name === "string" && saved.participant_name.trim()) return saved;
    } catch { /* Ignore malformed or unavailable browser storage. */ }
    return null;
}
function readHostRecoveryToken() {
    try { return localStorage.getItem(recoveryStorageKey) || ""; }
    catch { return ""; }
}
function saveStoredIdentity(id, name) {
    try { localStorage.setItem(identityStorageKey, JSON.stringify({ participant_id: id, participant_name: name })); }
    catch { /* The room still works for this page even if storage is unavailable. */ }
}
let storedRoom = readStoredIdentity();
const participantId = storedRoom?.participant_id || params.get("participant_id") || crypto.randomUUID();
const participantName = storedRoom?.participant_name || params.get("name") || "Guest";
let hostRecoveryToken = readHostRecoveryToken() || params.get("recovery_token") || "";
const status = document.querySelector("#connection-status");
const list = document.querySelector("#participant-list");
const count = document.querySelector("#participant-count");
const message = document.querySelector("#room-message");
const identityRecoveryLink = document.querySelector("#identity-recovery");
const video = document.querySelector("#shared-video");
video.controls = false;
const playbackNotice = document.querySelector("#playback-notice");
const startSyncedPlaybackButton = document.querySelector("#start-synced-playback");
const chatForm = document.querySelector("#chat-form");
const chatInput = document.querySelector("#chat-input");
const chatMessages = document.querySelector("#chat-messages");
const reactionFeed = document.querySelector("#reaction-feed");
const socialStatus = document.querySelector("#social-status");
const movieSearch = document.querySelector("#movie-search");
const movieCategory = document.querySelector("#movie-category");
const movieCatalogue = document.querySelector("#movie-catalogue");
const playlistList = document.querySelector("#playlist-list");
const playlistStatus = document.querySelector("#playlist-status");
const pollForm = document.querySelector("#poll-form");
const pollQuestion = document.querySelector("#poll-question");
const pollOptionsInputs = document.querySelector("#poll-options-inputs");
const addPollOptionButton = document.querySelector("#add-poll-option");
const pollStatus = document.querySelector("#poll-status");
const pollList = document.querySelector("#poll-list");
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
let currentMovieId = "sintel-trailer";
let currentMovieTitle = "Sintel";
let playlistRevision = -1;
const myVotes = new Map();
const pollStates = new Map();

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

async function loadMovieCatalogue() {
    const query = new URLSearchParams();
    if (movieSearch.value.trim()) query.set("search", movieSearch.value.trim());
    if (movieCategory.value) query.set("category", movieCategory.value);
    try {
        const response = await fetch(`/api/movies?${query.toString()}`);
        if (!response.ok) throw new Error("Catalogue unavailable");
        const data = await response.json();
        movieCatalogue.replaceChildren();
        const allOption = document.createElement("option"); allOption.value = ""; allOption.textContent = "All categories";
        movieCategory.replaceChildren(allOption);
        for (const category of data.categories) {
            const option = document.createElement("option"); option.value = category; option.textContent = category;
            movieCategory.append(option);
        }
        // Keep the selected filter when refreshing on a search keystroke.
        if (data.categories.includes(movieCategory.dataset.selectedCategory)) movieCategory.value = movieCategory.dataset.selectedCategory;
        for (const movie of data.movies) {
            const item = document.createElement("li");
            item.className = "movie-card";
            const details = document.createElement("div");
            const title = document.createElement("strong"); title.textContent = `${movie.title} (${movie.year})`;
            const category = document.createElement("p"); category.textContent = movie.category;
            const description = document.createElement("small"); description.textContent = movie.description;
            details.append(title, category, description);
            const addButton = document.createElement("button");
            addButton.type = "button"; addButton.textContent = "Add to playlist";
            addButton.addEventListener("click", () => sendSocialCommand({ type: "playlist_add", movie_id: movie.id }));
            item.append(details, addButton); movieCatalogue.append(item);
        }
        if (!data.movies.length) {
            const empty = document.createElement("li"); empty.className = "movie-empty";
            empty.textContent = "No catalogue movies match those filters."; movieCatalogue.append(empty);
        }
        playlistStatus.textContent = "";
    } catch {
        movieCatalogue.replaceChildren();
        const error = document.createElement("li"); error.className = "movie-empty";
        error.textContent = "Movie catalogue could not be loaded. Check the room connection and try again.";
        movieCatalogue.append(error);
    }
}

function renderPlaylist(playlist, revision = playlistRevision) {
    if (Number.isInteger(revision) && revision < playlistRevision) return;
    if (Number.isInteger(revision)) playlistRevision = revision;
    playlistList.replaceChildren();
    for (const entry of playlist) {
        const item = document.createElement("li"); item.className = "movie-card";
        const details = document.createElement("div");
        const title = document.createElement("strong"); title.textContent = `${entry.title} (${entry.year})`;
        const byline = document.createElement("p"); byline.textContent = `${entry.category} · added by ${entry.added_by_name}`;
        details.append(title, byline);
        const actions = document.createElement("div");
        if (currentHostId === participantId) {
            const selectButton = document.createElement("button"); selectButton.type = "button";
            selectButton.textContent = "Play this movie";
            selectButton.addEventListener("click", () => sendSocialCommand({ type: "playlist_select", movie_id: entry.movie_id }));
            actions.append(selectButton);
        }
        if (entry.added_by_id === participantId || currentHostId === participantId) {
            const removeButton = document.createElement("button"); removeButton.type = "button";
            removeButton.className = "secondary"; removeButton.textContent = "Remove";
            removeButton.addEventListener("click", () => sendSocialCommand({ type: "playlist_remove", movie_id: entry.movie_id }));
            actions.append(removeButton);
        }
        item.append(details, actions); playlistList.append(item);
    }
    if (!playlist.length) {
        const empty = document.createElement("li"); empty.className = "movie-empty";
        empty.textContent = "The shared playlist is empty. Add a movie from discovery."; playlistList.append(empty);
    }
}

function renderPolls() {
    pollList.replaceChildren();
    for (const poll of pollStates.values()) {
        const card = document.createElement("article"); card.className = "poll-card";
        const question = document.createElement("h3"); question.textContent = poll.question;
        const meta = document.createElement("p"); meta.className = "poll-meta";
        meta.textContent = `${poll.creator_name} · ${poll.total_votes} vote${poll.total_votes === 1 ? "" : "s"} · ${poll.status}`;
        const voteHint = document.createElement("p");
        voteHint.className = "poll-meta";
        voteHint.textContent = poll.status === "open" ? "You can change your vote until this poll closes." : "Voting is closed.";
        card.append(question, meta, voteHint);
        const myVote = myVotes.get(poll.id) || poll.my_vote || null;
        for (const option of poll.options) {
            const row = document.createElement("div"); row.className = "poll-choice";
            const voteButton = document.createElement("button"); voteButton.type = "button";
            voteButton.textContent = myVote === option.id ? `Your choice: ${option.text}` : option.text;
            voteButton.disabled = poll.status !== "open";
            voteButton.addEventListener("click", () => sendSocialCommand({ type: "poll_vote", poll_id: poll.id, option_id: option.id }));
            const count = document.createElement("span"); count.textContent = `${option.votes} vote${option.votes === 1 ? "" : "s"}`;
            row.append(voteButton, count); card.append(row);
        }
        if (poll.status === "open" && (poll.creator_id === participantId || currentHostId === participantId)) {
            const closeButton = document.createElement("button"); closeButton.type = "button";
            closeButton.className = "poll-close"; closeButton.textContent = "Close poll";
            closeButton.addEventListener("click", () => sendSocialCommand({ type: "poll_close", poll_id: poll.id }));
            card.append(closeButton);
        }
        pollList.append(card);
    }
}

function acceptRoomState(data) {
    renderParticipants(data.participants, data.host_id);
    renderPlaylist(data.playlist || [], data.playlist_revision);
    for (const poll of data.polls || []) {
        const existingPoll = pollStates.get(poll.id);
        if (existingPoll && Number(poll.revision) < Number(existingPoll.revision)) continue;
        pollStates.set(poll.id, poll);
        if (Object.hasOwn(poll, "my_vote")) {
            if (poll.my_vote) myVotes.set(poll.id, poll.my_vote);
            else myVotes.delete(poll.id);
        }
    }
    renderPolls();
}

function applyPollEvent(data) {
    if (!data.poll || typeof data.poll.id !== "string") return;
    const existingPoll = pollStates.get(data.poll.id);
    if (existingPoll && Number(data.poll.revision) < Number(existingPoll.revision)) return;
    pollStates.set(data.poll.id, data.poll);
    renderPolls();
}

function applyPollVoteAcknowledgement(data) {
    if (typeof data.poll_id !== "string" || typeof data.option_id !== "string") return;
    const currentPoll = pollStates.get(data.poll_id);
    if (data.poll && typeof data.poll.id === "string") {
        if (currentPoll && Number(data.poll.revision) < Number(currentPoll.revision)) return;
        if (data.poll.my_vote !== data.option_id) return;
        pollStates.set(data.poll.id, data.poll);
    }
    myVotes.set(data.poll_id, data.option_id);
    renderPolls();
}

function renderReaction(data) {
    if (typeof data.sender !== "string" || typeof data.emoji !== "string") return;
    const item = document.createElement("span");
    item.className = "reaction-notice";
    item.textContent = `${data.emoji} ${data.sender}`;
    reactionFeed.append(item);
    window.setTimeout(() => item.remove(), 5000);
}

function renderVideoCredit(movie) {
    const credit = document.querySelector("#video-credit");
    if (!credit) return;
    const attribution = document.createElement("span");
    attribution.textContent = `${movie.title} · ${movie.credit} · `;
    const source = document.createElement("a");
    source.href = movie.source_page;
    source.target = "_blank";
    source.rel = "noreferrer";
    source.textContent = "source";
    const license = document.createElement("a");
    license.href = "https://creativecommons.org/licenses/by/3.0/";
    license.target = "_blank";
    license.rel = "noreferrer";
    license.textContent = "CC BY 3.0";
    credit.replaceChildren(attribution, source, document.createTextNode(" · "), license);
}

function connect() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    restoreOnNextPlaybackState = true;
    const credentials = new URLSearchParams({ participant_id: participantId, name: participantName });
    if (hostRecoveryToken) credentials.set("recovery_token", hostRecoveryToken);
    socket = new WebSocket(`${protocol}://${window.location.host}/ws/rooms/${roomCode}?${credentials.toString()}`);
    socket.onopen = () => {
        if (params.has("recovery_token") && window.history?.replaceState) {
            window.history.replaceState(null, "", `/rooms/${normalizedRoomCode}`);
        }
        status.textContent = "Connected";
        status.classList.add("online");
        if (!startSyncedPlaybackButton.hidden) showSyncedPlaybackButton();
        if (playbackNotice.textContent.startsWith("Reconnecting")) playbackNotice.textContent = "";
    };
    socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); }
        catch { message.textContent = "Received an unreadable room update."; return; }
        if (data.type === "room_state") {
            saveStoredIdentity(participantId, participantName);
            acceptRoomState(data);
        }
        if (data.type === "host_transferred") {
            if (data.host_id !== participantId) {
                hostRecoveryToken = "";
                try { localStorage.removeItem(recoveryStorageKey); } catch { /* Storage can be disabled. */ }
            }
            renderParticipants(data.participants, data.host_id);
            renderPlaylist(data.playlist || []);
            renderPolls();
            message.textContent = data.host_id === participantId ? "You are now the host." : "The host role was transferred.";
        }
        if (data.type === "host_recovery_credential" && typeof data.token === "string") {
            hostRecoveryToken = data.token;
            try { localStorage.setItem(recoveryStorageKey, hostRecoveryToken); } catch { /* Host can use this connection; refresh recovery may be unavailable. */ }
        }
        if (data.type === "playback_state") receivePlaybackState(data);
        if (data.type === "participant_joined") message.textContent = `${data.participant.name} joined the room.`;
        if (data.type === "participant_left") message.textContent = "A participant left the room.";
        if (data.type === "chat_message") renderChatMessage(data);
        if (data.type === "reaction") renderReaction(data);
        if (data.type === "playlist_updated") { renderPlaylist(data.playlist || [], data.playlist_revision); playlistStatus.textContent = ""; }
        if (data.type === "poll_created" || data.type === "poll_updated" || data.type === "poll_closed") applyPollEvent(data);
        if (data.type === "poll_vote_recorded") applyPollVoteAcknowledgement(data);
        if (data.type === "error") socialStatus.textContent = data.detail;
    };
    socket.onclose = (event) => {
        status.textContent = "Reconnecting...";
        status.classList.remove("online");
        if ([4403, 4409, 4404].includes(event.code)) {
            status.textContent = "Room identity needs attention";
            if (event.code === 4404) {
                message.textContent = "This room is no longer available. Return home to create or join another room.";
                identityRecoveryLink.href = "/";
                identityRecoveryLink.textContent = "Return home";
            } else {
                message.textContent = event.reason || "Your saved room identity could not be verified. Re-enter your name to join as a guest.";
                identityRecoveryLink.href = `/?room_code=${encodeURIComponent(normalizedRoomCode)}`;
                identityRecoveryLink.textContent = "Re-enter your name";
            }
            identityRecoveryLink.hidden = false;
            try {
                localStorage.removeItem(identityStorageKey);
                localStorage.removeItem(recoveryStorageKey);
            } catch { /* Leave the recovery page available even if cleanup fails. */ }
            return;
        }
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
    const selectedMovie = state.movie;
    const movieChanged = selectedMovie && typeof selectedMovie.id === "string" && selectedMovie.id !== currentMovieId;
    if (movieChanged && typeof selectedMovie.video_url === "string") {
        currentMovieId = selectedMovie.id;
        currentMovieTitle = selectedMovie.title;
        video.src = selectedMovie.video_url;
        video.load();
        renderVideoCredit(selectedMovie);
    }
    const forceCorrection = restoreOnNextPlaybackState || state.action === "seek" || state.action === "select" || movieChanged;
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
    playbackNotice.textContent = `${currentMovieTitle} could not be loaded. Check your connection or choose another movie from the playlist.`;
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
movieSearch.addEventListener("input", loadMovieCatalogue);
movieCategory.addEventListener("change", () => {
    movieCategory.dataset.selectedCategory = movieCategory.value;
    loadMovieCatalogue();
});
function refreshPollOptionControls() {
    const rows = pollOptionsInputs.querySelectorAll(".poll-option-row");
    rows.forEach((row, index) => {
        row.querySelector(".poll-option-label").textContent = `Option ${index + 1}`;
        const removeButton = row.querySelector(".remove-poll-option");
        removeButton.disabled = rows.length <= 2;
        removeButton.setAttribute("aria-label", `Remove option ${index + 1}`);
    });
    addPollOptionButton.disabled = rows.length >= 5;
}

function createPollOptionRow(value = "") {
    const row = document.createElement("div"); row.className = "poll-option-row";
    const label = document.createElement("label");
    const optionLabel = document.createElement("span"); optionLabel.className = "poll-option-label";
    const input = document.createElement("input"); input.className = "poll-option-input"; input.maxLength = 80; input.required = true; input.value = value;
    label.append(optionLabel, input);
    const removeButton = document.createElement("button"); removeButton.type = "button";
    removeButton.className = "remove-poll-option"; removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => {
        if (pollOptionsInputs.querySelectorAll(".poll-option-row").length <= 2) return;
        row.remove();
        refreshPollOptionControls();
    });
    row.append(label, removeButton);
    return row;
}

pollOptionsInputs.querySelectorAll(".poll-option-row").forEach((row) => {
    row.querySelector(".remove-poll-option").addEventListener("click", () => {
        if (pollOptionsInputs.querySelectorAll(".poll-option-row").length <= 2) return;
        row.remove();
        refreshPollOptionControls();
    });
});
refreshPollOptionControls();
addPollOptionButton.addEventListener("click", () => {
    const currentOptions = pollOptionsInputs.querySelectorAll(".poll-option-row");
    if (currentOptions.length >= 5) { pollStatus.textContent = "Polls can have at most 5 options."; return; }
    pollOptionsInputs.append(createPollOptionRow());
    refreshPollOptionControls();
});
pollForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const options = Array.from(pollOptionsInputs.querySelectorAll(".poll-option-input"), (input) => input.value.trim()).filter(Boolean);
    if (!pollQuestion.value.trim() || options.length < 2 || options.length > 5) {
        pollStatus.textContent = "Enter a question and between 2 and 5 non-empty options."; return;
    }
    if (pollQuestion.value.trim().length > 160 || options.some((option) => option.length > 80)) {
        pollStatus.textContent = "Questions allow 160 characters and options allow 80 characters."; return;
    }
    if (sendSocialCommand({ type: "poll_create", question: pollQuestion.value.trim(), options })) {
        pollQuestion.value = "";
        pollOptionsInputs.replaceChildren(createPollOptionRow(), createPollOptionRow());
        refreshPollOptionControls();
        pollStatus.textContent = "";
    }
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
loadMovieCatalogue();
