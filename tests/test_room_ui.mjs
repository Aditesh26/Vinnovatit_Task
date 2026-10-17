import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const roomScript = readFileSync(
  new URL("../src/vinnovatit_task/static/room.js", import.meta.url),
  "utf8",
);

class FakeElement {
  constructor() {
    this.listeners = new Map();
    this.children = [];
    this.textContent = "";
    this.classList = { add() {}, remove() {} };
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
  }

  addEventListener(type, callback) {
    this.listeners.set(type, callback);
  }

  emit(type, event = {}) {
    this.listeners.get(type)?.({ preventDefault() {}, ...event });
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  remove() {}
}

class FakeVideo extends FakeElement {
  constructor() {
    super();
    this.readyState = 1;
    this.duration = 120;
    this.currentTime = 0;
    this.playbackRate = 1;
    this.paused = true;
    this.seeking = false;
    this.playCalls = 0;
    this.playResults = [new Error("autoplay blocked"), null];
  }

  play() {
    this.playCalls += 1;
    const result = this.playResults.shift();
    if (result instanceof Error) return Promise.reject(result);
    return Promise.resolve().then(() => {
      this.paused = false;
      this.emit("play");
    });
  }

  pause() {
    this.paused = true;
    this.emit("pause");
  }
}

class FakeWebSocket extends FakeElement {
  static OPEN = 1;
  static latest;

  constructor(url) {
    super();
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    this.sent = [];
    FakeWebSocket.latest = this;
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }
}

function createGuestPage() {
  const selectors = new Map([
    ["#connection-status", new FakeElement()],
    ["#participant-list", new FakeElement()],
    ["#participant-count", new FakeElement()],
    ["#room-message", new FakeElement()],
    ["#shared-video", new FakeVideo()],
    ["#playback-notice", new FakeElement()],
    ["#start-synced-playback", new FakeElement()],
    ["#chat-form", new FakeElement()],
    ["#chat-input", new FakeElement()],
    ["#chat-messages", new FakeElement()],
    ["#reaction-feed", new FakeElement()],
    ["#social-status", new FakeElement()],
    ["#copy-code", new FakeElement()],
  ]);
  const document = {
    body: {
      dataset: {
        roomCode: "ABC123",
        driftThresholdSeconds: "0.75",
        driftCheckIntervalMs: "1000",
        driftCorrectionCooldownMs: "2500",
      },
    },
    querySelector: (selector) => selectors.get(selector),
    querySelectorAll: () => [],
    createElement: () => new FakeElement(),
  };
  const window = {
    location: { protocol: "http:", host: "localhost:8000", href: "http://localhost:8000/rooms/ABC123" },
    addEventListener() {},
    setInterval() { return 1; },
    setTimeout,
    clearTimeout,
  };
  const context = {
    document,
    window,
    WebSocket: FakeWebSocket,
    HTMLMediaElement: { HAVE_METADATA: 1, HAVE_FUTURE_DATA: 3 },
    URLSearchParams,
    sessionStorage: { getItem: () => null, setItem() {} },
    crypto: { randomUUID: () => "guest-id" },
    performance: { now: () => 5000 },
    Promise,
    Date,
    JSON,
    Math,
    Number,
  };
  vm.runInNewContext(roomScript, context);
  const socket = FakeWebSocket.latest;
  socket.onopen();
  socket.onmessage({ data: JSON.stringify({
    type: "room_state",
    host_id: "host-id",
    participants: [
      { id: "host-id", name: "Host", connected: true },
      { id: "guest-id", name: "Guest", connected: true },
    ],
  }) });
  return {
    socket,
    video: selectors.get("#shared-video"),
    button: selectors.get("#start-synced-playback"),
    notice: selectors.get("#playback-notice"),
  };
}

test("autoplay fallback starts locally at server position without sending playback commands", async () => {
  const { socket, video, button, notice } = createGuestPage();
  video.readyState = 0;
  const playingState = {
    type: "playback_state",
    revision: 1,
    action: "play",
    paused: false,
    position: 10,
    updated_at: Date.now() / 1000 - 3,
    playback_rate: 1.25,
  };

  socket.onmessage({ data: JSON.stringify(playingState) });
  assert.equal(video.playCalls, 0, "waits until video metadata is available");
  assert.equal(button.hidden, true);

  video.readyState = 1;
  video.emit("loadedmetadata");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, false);
  assert.match(notice.textContent, /click to start/i);
  assert.equal(video.playbackRate, 1.25);

  video.currentTime = 1;
  button.emit("click");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(video.currentTime > 12 && video.currentTime < 15, "seeks to the current server-estimated position");
  assert.equal(video.paused, false);
  assert.equal(button.hidden, true);
  assert.deepEqual(socket.sent.filter((message) => message.type === "playback_command"), []);

  const playCount = video.playCalls;
  socket.onmessage({ data: JSON.stringify({
    ...playingState,
    revision: 2,
    action: "pause",
    paused: true,
    position: 20,
    updated_at: Date.now() / 1000,
  }) });
  assert.equal(video.paused, true);
  button.emit("click"); // even a stale/queued click must not resume a paused room
  assert.equal(video.playCalls, playCount);
  assert.deepEqual(socket.sent.filter((message) => message.type === "playback_command"), []);
});
