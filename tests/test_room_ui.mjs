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
    this.tagName = "div";
    this.listeners = new Map();
    this.children = [];
    this.textContent = "";
    this.value = "";
    this.attributes = new Map();
    this.className = "";
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
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
  }

  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = children;
    for (const child of children) child.parentElement = this;
  }

  querySelectorAll(selector) {
    const className = selector.startsWith(".") ? selector.slice(1) : null;
    const matches = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (className && child.className.split(/\s+/).includes(className)) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
    this.parentElement = null;
  }
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
    this.loadCalls = 0;
    this.playResults = [new Error("autoplay blocked"), null];
  }

  load() {
    this.loadCalls += 1;
    this.readyState = 0;
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
    ["#video-credit", new FakeElement()],
    ["#playback-notice", new FakeElement()],
    ["#start-synced-playback", new FakeElement()],
    ["#chat-form", new FakeElement()],
    ["#chat-input", new FakeElement()],
    ["#chat-messages", new FakeElement()],
    ["#reaction-feed", new FakeElement()],
    ["#social-status", new FakeElement()],
    ["#copy-code", new FakeElement()],
    ["#movie-search", new FakeElement()],
    ["#movie-category", new FakeElement()],
    ["#movie-catalogue", new FakeElement()],
    ["#playlist-list", new FakeElement()],
    ["#playlist-status", new FakeElement()],
    ["#poll-form", new FakeElement()],
    ["#poll-question", new FakeElement()],
    ["#poll-options-inputs", new FakeElement()],
    ["#add-poll-option", new FakeElement()],
    ["#poll-status", new FakeElement()],
    ["#poll-list", new FakeElement()],
  ]);
  for (let index = 1; index <= 2; index += 1) {
    const row = new FakeElement(); row.className = "poll-option-row";
    const label = new FakeElement();
    const text = new FakeElement(); text.className = "poll-option-label"; text.textContent = `Option ${index}`;
    const input = new FakeElement(); input.className = "poll-option-input";
    label.append(text, input);
    const remove = new FakeElement(); remove.className = "remove-poll-option"; remove.disabled = true;
    row.append(label, remove);
    selectors.get("#poll-options-inputs").append(row);
  }
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
    createElement: (tagName) => { const element = new FakeElement(); element.tagName = tagName; return element; },
    createTextNode: (text) => { const node = new FakeElement(); node.textContent = text; return node; },
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
    fetch: async () => ({ ok: true, json: async () => ({ movies: [], categories: [] }) }),
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
    credit: selectors.get("#video-credit"),
    options: selectors.get("#poll-options-inputs"),
    addOption: selectors.get("#add-poll-option"),
    pollStatus: selectors.get("#poll-status"),
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

test("poll option controls enforce limits, renumber rows, and preserve other inputs", () => {
  const { options, addOption } = createGuestPage();
  const rows = () => options.querySelectorAll(".poll-option-row");
  const rowValues = () => rows().map((row) => row.querySelector(".poll-option-input").value);
  const labels = () => rows().map((row) => row.querySelector(".poll-option-label").textContent);

  rows()[0].querySelector(".poll-option-input").value = "Alpha";
  rows()[1].querySelector(".poll-option-input").value = "Beta";
  assert.deepEqual(rows().map((row) => row.querySelector(".remove-poll-option").disabled), [true, true]);

  for (const value of ["Gamma", "Delta", "Epsilon"]) {
    addOption.emit("click");
    rows().at(-1).querySelector(".poll-option-input").value = value;
  }
  assert.equal(rows().length, 5);
  assert.equal(addOption.disabled, true);
  assert.deepEqual(labels(), ["Option 1", "Option 2", "Option 3", "Option 4", "Option 5"]);

  rows()[0].querySelector(".remove-poll-option").emit("click");
  assert.deepEqual(rowValues(), ["Beta", "Gamma", "Delta", "Epsilon"]);
  assert.deepEqual(labels(), ["Option 1", "Option 2", "Option 3", "Option 4"]);
  assert.equal(rows()[0].querySelector(".remove-poll-option").getAttribute("aria-label"), "Remove option 1");

  rows()[1].querySelector(".remove-poll-option").emit("click");
  assert.deepEqual(rowValues(), ["Beta", "Delta", "Epsilon"]);
  assert.deepEqual(labels(), ["Option 1", "Option 2", "Option 3"]);

  rows().at(-1).querySelector(".remove-poll-option").emit("click");
  assert.deepEqual(rowValues(), ["Beta", "Delta"]);
  assert.deepEqual(labels(), ["Option 1", "Option 2"]);
  assert.equal(addOption.disabled, false);
  rows()[0].querySelector(".remove-poll-option").emit("click");
  assert.equal(rows().length, 2, "the minimum option count cannot be crossed");
});

test("host-selected catalogue media changes the local source and reports source errors", () => {
  const { socket, video, notice, credit } = createGuestPage();
  const bigBuckUrl = "https://upload.wikimedia.org/wikipedia/commons/transcoded/8/88/Big_Buck_Bunny_alt.webm/Big_Buck_Bunny_alt.webm.360p.vp9.webm";
  socket.onmessage({ data: JSON.stringify({
    type: "playback_state",
    revision: 1,
    action: "select",
    paused: true,
    position: 0,
    updated_at: Date.now() / 1000,
    movie: {
      id: "big-buck-bunny",
      title: "Big Buck Bunny",
      video_url: bigBuckUrl,
      media_type: "video/webm",
      credit: "Blender Foundation, CC BY 3.0",
      source_page: "https://commons.wikimedia.org/wiki/File:Big_Buck_Bunny_alt.webm",
    },
  }) });
  assert.equal(video.src, bigBuckUrl);
  assert.equal(video.loadCalls, 1);
  assert.equal(video.playCalls, 0, "selecting a movie leaves the room paused");
  assert.equal(credit.children[0].textContent, "Big Buck Bunny · Blender Foundation, CC BY 3.0 · ");

  video.emit("error");
  assert.match(notice.textContent, /Big Buck Bunny could not be loaded.*choose another movie/i);
});
