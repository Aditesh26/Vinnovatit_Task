const roomCode = document.body.dataset.roomCode;
const params = new URLSearchParams(window.location.search);
const storedRoom = JSON.parse(sessionStorage.getItem(`watch-party:${roomCode}`) || "null");
const participantId = params.get("participant_id") || storedRoom?.participant_id || crypto.randomUUID();
const participantName = storedRoom?.participant_name || "Guest";
const status = document.querySelector("#connection-status");
const list = document.querySelector("#participant-list");
const count = document.querySelector("#participant-count");
const message = document.querySelector("#room-message");
let reconnectTimer;

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
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/rooms/${roomCode}?participant_id=${encodeURIComponent(participantId)}&name=${encodeURIComponent(participantName)}`);
  socket.onopen = () => { status.textContent = "Connected"; status.classList.add("online"); };
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "room_state") renderParticipants(data.participants, data.host_id);
    if (data.type === "participant_joined") message.textContent = `${data.participant.name} joined the room.`;
    if (data.type === "participant_left") message.textContent = "A participant left the room.";
    if (data.type === "error") message.textContent = data.detail;
  };
  socket.onclose = () => { status.textContent = "Reconnecting…"; status.classList.remove("online"); reconnectTimer = window.setTimeout(connect, 1500); };
}

document.querySelector("#copy-link").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(window.location.href.split("?")[0]); message.textContent = "Invite link copied to your clipboard."; }
  catch { message.textContent = "Copy the link from your browser address bar."; }
});
window.addEventListener("beforeunload", () => window.clearTimeout(reconnectTimer));
connect();
