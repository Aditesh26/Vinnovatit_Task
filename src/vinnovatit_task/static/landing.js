const errorBox = document.querySelector("#form-error");

function showError(message) { errorBox.textContent = message; }

function identityKey(code) { return `watch-party:identity:${code.trim().toUpperCase()}`; }
function hostRecoveryKey(code) { return `watch-party:host-recovery:${code.trim().toUpperCase()}`; }

function readSavedIdentity(code) {
    try {
        const saved = JSON.parse(localStorage.getItem(identityKey(code)) || "null");
        if (saved && typeof saved.participant_id === "string" && saved.participant_id
            && typeof saved.participant_name === "string" && saved.participant_name.trim()) return saved;
    } catch { /* A bad browser entry should behave like no saved identity. */ }
    return null;
}

function saveRoomIdentity(room) {
    const code = room.code.trim().toUpperCase();
    try {
        localStorage.setItem(identityKey(code), JSON.stringify({
            participant_id: room.participant_id,
            participant_name: room.participant_name,
        }));
        if (room.host_recovery_token) localStorage.setItem(hostRecoveryKey(code), room.host_recovery_token);
        return true;
    } catch { /* Private browsing/storage limits must not prevent room entry. */ return false; }
}

async function requestRoom(url, body) {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Something went wrong. Please try again.");
    return data;
}

function enterRoom(room) {
    const saved = saveRoomIdentity(room);
    const destination = new URL(room.room_url, window.location.origin);
    destination.searchParams.set("participant_id", room.participant_id);
    if (!saved && room.host_recovery_token) {
        // Use a one-navigation fallback when storage is blocked; room.js removes
        // this credential from the visible URL after the socket opens.
        destination.searchParams.set("recovery_token", room.host_recovery_token);
    }
    window.location.assign(destination.href);
}

document.querySelector("#create-form").addEventListener("submit", async (event) => {
    event.preventDefault(); showError("");
    try { enterRoom(await requestRoom("/api/rooms", { name: event.currentTarget.name.value })); }
    catch (error) { showError(error.message); }
});

document.querySelector("#join-form").addEventListener("submit", async (event) => {
    event.preventDefault(); showError("");
    const code = event.currentTarget.code.value.trim().toUpperCase();
    if (!/^[A-Z0-9]{6}$/.test(code)) { showError("Enter the six-character room code from your invite."); return; }
    const savedIdentity = readSavedIdentity(code);
    if (savedIdentity) {
        enterRoom({ code, ...savedIdentity, room_url: `/rooms/${code}` });
        return;
    }
    try { enterRoom(await requestRoom(`/api/rooms/${code}/participants`, { name: event.currentTarget.name.value })); }
    catch (error) { showError(error.message); }
});

const roomCodeInput = document.querySelector("#room-code");
const guestNameInput = document.querySelector("#guest-name");
if (roomCodeInput && guestNameInput) {
    const restoreName = () => {
        const saved = readSavedIdentity(roomCodeInput.value);
        if (saved) guestNameInput.value = saved.participant_name;
    };
    roomCodeInput.addEventListener("input", restoreName);
    const requestedCode = new URLSearchParams(window.location.search).get("room_code");
    if (requestedCode) { roomCodeInput.value = requestedCode.toUpperCase(); restoreName(); guestNameInput.focus(); }
}
