const errorBox = document.querySelector("#form-error");

function showError(message) { errorBox.textContent = message; }

async function requestRoom(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Something went wrong. Please try again.");
  return data;
}

function enterRoom(room) {
  sessionStorage.setItem(`watch-party:${room.code}`, JSON.stringify(room));
  window.location.assign(`${room.room_url}?participant_id=${encodeURIComponent(room.participant_id)}`);
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
  try { enterRoom(await requestRoom(`/api/rooms/${code}/participants`, { name: event.currentTarget.name.value })); }
  catch (error) { showError(error.message); }
});
