import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Set, Optional, List, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# Fallback texts (used only when host doesn't set pool or single text)
TEXTS = [
    "SOA reconciliation improves completeness and reduces operational risk.",
    "Automation helps track SOA tickets even when some were not downloaded.",
    "Accurate SOA recon needs visibility, status tracking, and timely follow-up.",
    "Reducing manual updates lowers error risk and saves daily processing time.",
]

DEFAULT_COUNTDOWN = 5  # seconds


# ------------------- Helpers -------------------
def parse_paragraphs(raw: str) -> List[str]:
    """
    Split a pasted list into paragraphs by blank lines.
    Also supports lines containing only --- as separators.
    """
    raw = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    raw = raw.replace("\n---\n", "\n\n")
    parts = [p.strip() for p in raw.split("\n\n") if p.strip()]
    return [p for p in parts if len(p) >= 10]


def correct_prefix_len(prompt: str, typed: str) -> int:
    """Number of characters matching prompt from start (TypeRacer-like)."""
    m = min(len(prompt), len(typed))
    i = 0
    while i < m and prompt[i] == typed[i]:
        i += 1
    return i


def compute_final_metrics(prompt: str, typed: str, elapsed_sec: float):
    """
    Simple final scoring:
    - Gross WPM: (chars/5)/minutes
    - Net WPM: gross - (errors/minute)
    - Accuracy: correct/prompt_len
    """
    minutes = max(elapsed_sec / 60.0, 1e-9)

    correct = 0
    mismatches = 0
    for a, b in zip(prompt, typed):
        if a == b:
            correct += 1
        else:
            mismatches += 1
    length_diff = abs(len(prompt) - len(typed))
    errors = mismatches + length_diff

    gross_wpm = (len(typed) / 5.0) / minutes
    net_wpm = max(gross_wpm - (errors / minutes), 0.0)
    accuracy = (correct / max(len(prompt), 1)) * 100.0

    return round(gross_wpm, 1), round(net_wpm, 1), round(accuracy, 1)


# ------------------- Game State -------------------
@dataclass
class PlayerState:
    name: str
    ready: bool = False

    # TypeRacer progress: correct prefix length
    progress: int = 0
    done: bool = False

    # live metrics
    wpm_live: float = 0.0

    # final metrics after finish
    gross_wpm: float = 0.0
    net_wpm: float = 0.0
    accuracy: float = 0.0

    # finish time for podium
    finish_time: Optional[float] = None  # seconds since round start


@dataclass
class RoomState:
    clients: Set[WebSocket] = field(default_factory=set)
    players_by_ws: Dict[WebSocket, str] = field(default_factory=dict)
    players: Dict[str, PlayerState] = field(default_factory=dict)

    host: Optional[str] = None

    # Single custom text (host)
    text_override: str = ""

    # Multi-paragraph pool (host pasted list)
    text_pool: List[str] = field(default_factory=list)
    # No-repeat bag drawn from pool (shuffled)
    text_bag: List[str] = field(default_factory=list)

    # Current round text
    text: str = ""
    start_time: float = 0.0
    started: bool = False

    countdown_task: Optional[asyncio.Task] = None
    countdown_active: bool = False
    countdown_remaining: int = 0

    # Whether we already broadcast podium for this round
    podium_broadcasted: bool = False


rooms: Dict[str, RoomState] = {}


def connected_player_names(room: RoomState) -> Set[str]:
    return set(room.players_by_ws.values())


def connected_count(room: RoomState) -> int:
    return len(connected_player_names(room))


def ready_count(room: RoomState) -> int:
    connected = connected_player_names(room)
    return sum(1 for n in connected if room.players.get(n) and room.players[n].ready)


def all_connected_ready(room: RoomState) -> bool:
    cc = connected_count(room)
    return cc > 0 and ready_count(room) == cc


def compute_podium(room: RoomState) -> List[Tuple[str, PlayerState]]:
    """Return top-3 based on finish_time (fastest wins)."""
    finished = [(name, ps) for name, ps in room.players.items() if ps.done and ps.finish_time is not None]
    finished.sort(key=lambda x: x[1].finish_time)  # ascending time
    return finished[:3]


def snapshot(room: RoomState):
    return {
        "host": room.host,
        "started": room.started,
        "text_len": len(room.text),
        "countdown_active": room.countdown_active,
        "countdown_remaining": room.countdown_remaining,
        "connected_count": connected_count(room),
        "ready_count": ready_count(room),
        "has_custom_text": bool(room.text_override.strip()),
        "pool_size": len(room.text_pool),
        "using_pool": bool(room.text_pool),
        "scores": {
            name: {
                "ready": ps.ready,
                "progress": ps.progress,
                "done": ps.done,
                "wpm_live": ps.wpm_live,
                "gross_wpm": ps.gross_wpm,
                "net_wpm": ps.net_wpm,
                "accuracy": ps.accuracy,
                "finish_time": ps.finish_time,
            } for name, ps in room.players.items()
        }
    }


async def broadcast(room_id: str, payload: dict):
    room = rooms.get(room_id)
    if not room:
        return
    msg = json.dumps(payload)
    dead = []
    for ws in list(room.clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room.clients.discard(ws)
        room.players_by_ws.pop(ws, None)


def pick_round_text(room: RoomState) -> str:
    # Priority: pool -> single override -> fallback list
    if room.text_pool:
        if not room.text_bag:
            room.text_bag = room.text_pool.copy()
            random.shuffle(room.text_bag)
        return room.text_bag.pop()
    if room.text_override.strip():
        return room.text_override.strip()
    return random.choice(TEXTS)


async def run_countdown_then_start(room_id: str, seconds: int):
    room = rooms.get(room_id)
    if not room:
        return

    room.countdown_active = True
    room.countdown_remaining = seconds
    await broadcast(room_id, {"type": "room", **snapshot(room)})

    try:
        for t in range(seconds, 0, -1):
            room = rooms.get(room_id)
            if not room:
                return
            room.countdown_remaining = t
            await broadcast(room_id, {"type": "countdown", "seconds": t})
            await broadcast(room_id, {"type": "room", **snapshot(room)})
            await asyncio.sleep(1)

        room = rooms.get(room_id)
        if not room:
            return

        room.text = pick_round_text(room)
        room.started = True
        room.start_time = time.time()
        room.countdown_active = False
        room.countdown_remaining = 0
        room.podium_broadcasted = False

        # Reset per-round stats and require Ready again
        for ps in room.players.values():
            ps.progress = 0
            ps.done = False
            ps.wpm_live = 0.0
            ps.gross_wpm = ps.net_wpm = ps.accuracy = 0.0
            ps.finish_time = None
            ps.ready = False

        await broadcast(room_id, {"type": "round", "text": room.text})
        await broadcast(room_id, {"type": "room", **snapshot(room)})

    finally:
        room = rooms.get(room_id)
        if room:
            room.countdown_task = None
            room.countdown_active = False
            room.countdown_remaining = 0
            await broadcast(room_id, {"type": "room", **snapshot(room)})


# ------------------- One-URL Web Page (TypeRacer-like) -------------------
HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Typing Race</title>
  <style>
    body { font-family: Segoe UI, Arial; max-width: 1100px; margin: 24px auto; }
    .card { padding: 12px; border: 1px solid #ddd; border-radius: 12px; margin-top: 12px; }
    .row { display:flex; gap:12px; flex-wrap: wrap; align-items:center; }
    input, textarea { padding: 8px; font-size: 14px; }
    textarea { width: 100%; font-size: 16px; padding: 10px; }
    button { padding: 8px 12px; }
    .muted { color:#666; }
    .right { margin-left:auto; }
    .countdown { font-weight:700; font-size: 18px; }
    .badge { display:inline-block; padding:2px 8px; border-radius: 999px; font-size: 12px; margin-left:6px;}
    .badge-ready { background:#e8fff0; color:#0b6b2a; border:1px solid #b7f0c9; }
    .badge-notready { background:#fff3f3; color:#8a1f1f; border:1px solid #ffd0d0; }

    /* prompt highlighting */
    #promptBox { font-size: 18px; line-height: 1.35; background:#f6f6f6; }
    .ok { color: #0b6b2a; }
    .next { text-decoration: underline; text-decoration-thickness: 3px; }
    .rest { color: #444; }

    /* racetrack */
    .track { position: relative; height: 30px; background: #f0f3ff; border: 1px solid #d6dcff; border-radius: 999px; overflow: hidden; }
    .car { position: absolute; top: 3px; left: 0%; transition: left 0.08s linear; }
    .lane { display:flex; gap:10px; align-items:center; margin: 8px 0; }
    .laneName { width: 220px; overflow:hidden; text-overflow: ellipsis; white-space: nowrap; }
    .laneStats { width: 280px; text-align:right; }

    #input { border: 2px solid #ddd; border-radius: 10px; }
    #input.bad { border-color: #d33; background: #fff3f3; }

    /* podium overlay */
    #podiumOverlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:9999; }
    #podiumCard { max-width:560px; margin:10vh auto; background:#fff; border-radius:16px; padding:18px; }
    #podiumList { font-size:16px; line-height:1.7; }
    #confetti { position:fixed; inset:0; pointer-events:none; }
  </style>
</head>
<body>
  <h2>Typing Race (TypeRacer‑style)</h2>

  <div class="card">
    <div class="row">
      <label>Name <input id="name" value="Andy"></label>
      <label>Room <input id="room" value="Typing"></label>
      <button onclick="join()">Join</button>

      <button id="readyBtn" onclick="toggleReady()" disabled>Ready ✅</button>
      <button id="startBtn" onclick="startCountdown()" disabled>Start Countdown (Host)</button>

      <span class="muted" id="status">Not connected</span>
      <span class="countdown" id="cd"></span>

      <span class="right muted" id="readyCount">Ready: 0/0</span>
    </div>
    <div class="muted">Share this same URL to others (Render URL / LAN URL).</div>
  </div>

  <div class="card" id="hostPanel" style="display:none;">
    <h3>Host Controls</h3>

    <div class="muted">Single text (used every round unless paragraph bank is set):</div>
    <textarea id="customText" rows="3" placeholder="Paste single paragraph here..."></textarea>
    <div class="row">
      <button onclick="updateText()">Update Single Text</button>
      <button onclick="clearText()">Clear Single Text</button>
      <span class="muted" id="textMode">Mode: Random fallback</span>
    </div>

    <hr style="border:none;border-top:1px solid #eee; margin:14px 0;">

    <h4>Paragraph Bank (Random each round, no-repeat until exhausted)</h4>
    <div class="muted">Paste multiple paragraphs separated by a blank line. Each round picks one randomly.</div>
    <textarea id="textList" rows="8" placeholder="Paragraph 1...

Paragraph 2...

Paragraph 3..."></textarea>
    <div class="row">
      <button onclick="updatePool()">Update Paragraph Bank</button>
      <button onclick="clearPool()">Clear Paragraph Bank</button>
      <span class="muted" id="poolInfo">Pool: 0</span>
    </div>
  </div>

  <div class="card" id="racePanel" style="display:none;">
    <h3>Prompt</h3>
    <div id="promptBox" class="card"></div>
    <div class="muted">Type until the end. Wrong character is rejected (TypeRacer-style).</div>
    <textarea id="input" rows="2" placeholder="Type here..." disabled></textarea>
  </div>

  <div class="card">
    <h3>Race Track</h3>
    <div id="lanes"></div>
  </div>

  <div id="podiumOverlay">
    <canvas id="confetti"></canvas>
    <div id="podiumCard">
      <h2 style="margin:0 0 10px;">🏆 Results</h2>
      <div id="podiumList"></div>
      <div class="row" style="margin-top:12px;">
        <button onclick="closePodium()">Close</button>
        <span class="muted">Host can start next round after everyone Ready again.</span>
      </div>
    </div>
  </div>

<script>
let ws;
let you = null;
let isReady = false;

let promptText = "";
let lastGood = "";          // local correct prefix string
let podiumShown = false;

let state = {
  host: null,
  text_len: 0,
  scores: {},
  ready_count: 0,
  connected_count: 0,
  has_custom_text: false,
  pool_size: 0,
  using_pool: false
};

function setStatus(t){ document.getElementById("status").innerText = t; }
function setCountdownText(t){ document.getElementById("cd").innerText = t ? ("⏳ " + t) : ""; }

function escapeHtml(s){
  return s.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
}

function correctPrefix(prompt, typed){
  const m = Math.min(prompt.length, typed.length);
  let i = 0;
  while(i < m && prompt[i] === typed[i]) i++;
  return i;
}

function renderPromptHighlight(){
  const i = correctPrefix(promptText, lastGood);
  const ok = escapeHtml(promptText.slice(0, i));
  const nextChar = promptText[i] ?? "";
  const next = escapeHtml(nextChar);
  const rest = escapeHtml(promptText.slice(i + (nextChar ? 1 : 0)));

  let html = "";
  html += `<span class="ok">${ok}</span>`;
  if(nextChar) html += `<span class="rest next">${next}</span>`;
  html += `<span class="rest">${rest}</span>`;
  document.getElementById("promptBox").innerHTML = html;
}

function wsUrl(){
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host;
  const r = encodeURIComponent(document.getElementById("room").value.trim());
  const n = encodeURIComponent(document.getElementById("name").value.trim());
  return `${protocol}://${host}/ws?room=${r}&name=${n}`;
}

function renderLanes(){
  const lanes = document.getElementById("lanes");
  lanes.innerHTML = "";
  const scores = state.scores || {};
  const names = Object.keys(scores);

  // sort by progress desc; finished with time bubble up
  names.sort((a,b)=>{
    const A = scores[a], B = scores[b];
    const fa = A.finish_time ?? Infinity;
    const fb = B.finish_time ?? Infinity;
    if(fa !== fb) return fa - fb;        // finished earlier first
    return (B.progress||0) - (A.progress||0);
  });

  for(const name of names){
    const s = scores[name];
    const pct = state.text_len ? Math.min(100, Math.round((s.progress||0)/state.text_len*100)) : 0;
    const readyBadge = s.ready
      ? '<span class="badge badge-ready">READY</span>'
      : '<span class="badge badge-notready">NO</span>';

    const carLeft = `calc(${pct}% - 10px)`;
    const w = (s.net_wpm ?? s.wpm_live ?? 0);
    const ft = (s.finish_time !== null && s.finish_time !== undefined) ? ` | ${s.finish_time.toFixed(2)}s` : "";

    const lane = document.createElement("div");
    lane.className = "lane";
    lane.innerHTML = `
      <div class="laneName">${escapeHtml(name)}${name===state.host ? " 👑" : ""}${readyBadge}</div>
      <div class="track" style="flex:1;">
        <div class="car" style="left:${carLeft};">🚗</div>
      </div>
      <div class="laneStats">
        ${pct}% | WPM: ${Number(w).toFixed(1)}${ft} ${s.done ? "✅" : ""}
      </div>
    `;
    lanes.appendChild(lane);
  }
}

function updateUIFromRoom(msg){
  state.host = msg.host;
  state.text_len = msg.text_len || 0;
  state.scores = msg.scores || {};
  state.ready_count = msg.ready_count || 0;
  state.connected_count = msg.connected_count || 0;
  state.has_custom_text = msg.has_custom_text || false;
  state.pool_size = msg.pool_size || 0;
  state.using_pool = msg.using_pool || false;

  document.getElementById("readyCount").innerText = `Ready: ${state.ready_count}/${state.connected_count}`;
  document.getElementById("poolInfo").innerText = `Pool: ${state.pool_size}`;

  const isHost = (you && you === state.host);
  document.getElementById("startBtn").disabled = !isHost;
  document.getElementById("hostPanel").style.display = isHost ? "block" : "none";

  let mode = "Mode: Random fallback";
  if(state.using_pool) mode = "Mode: Paragraph Bank (Random)";
  else if(state.has_custom_text) mode = "Mode: Single custom text";
  document.getElementById("textMode").innerText = mode;

  renderLanes();
}

function join(){
  ws = new WebSocket(wsUrl());
  ws.onopen = () => { setStatus("Connected"); };
  ws.onclose = () => { setStatus("Disconnected"); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if(msg.type === "welcome"){
      you = msg.you;
      document.getElementById("racePanel").style.display = "block";
      document.getElementById("readyBtn").disabled = false;
    }

    if(msg.type === "room" || msg.type === "state" || msg.type === "leaderboard"){
      updateUIFromRoom(msg);

      // If server broadcasts podium, show to everyone
      if(msg.type === "leaderboard" && msg.podium){
        if(!podiumShown){
          showPodium(msg.podium);
        }
      }
    }

    if(msg.type === "countdown"){
      setCountdownText(msg.seconds + "s");
      setStatus("Countdown...");
      document.getElementById("input").disabled = true;
    }

    if(msg.type === "round"){
      setCountdownText("");
      promptText = msg.text;
      lastGood = "";
      podiumShown = false;
      renderPromptHighlight();

      const inp = document.getElementById("input");
      inp.value = "";
      inp.disabled = false;
      inp.classList.remove("bad");
      inp.focus();

      setStatus("GO!");
      isReady = false;
      document.getElementById("readyBtn").innerText = "Ready ✅";
    }

    if(msg.type === "pool_updated"){
      document.getElementById("poolInfo").innerText = `Pool: ${msg.pool_size}`;
      setStatus("Paragraph bank updated ✅");
    }

    if(msg.type === "text_updated"){
      setStatus("Single text updated ✅ (next round)");
    }

    if(msg.type === "podium"){
      if(!podiumShown){
        showPodium(msg.podium);
      }
    }

    if(msg.type === "error"){
      alert(msg.message);
    }
  };
}

function toggleReady(){
  if(!ws) return;
  isReady = !isReady;
  ws.send(JSON.stringify({action:"ready", ready:isReady}));
  document.getElementById("readyBtn").innerText = isReady ? "Unready ❌" : "Ready ✅";
}

function startCountdown(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"start_countdown"}));
}

function updateText(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"set_text", text: document.getElementById("customText").value}));
}

function clearText(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"clear_text"}));
}

function updatePool(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"set_pool", text_list: document.getElementById("textList").value}));
}

function clearPool(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"clear_pool"}));
}

let lastSent = 0;
function sendTypeUpdate(typed){
  const now = Date.now();
  if(now - lastSent < 80) return; // throttle
  lastSent = now;
  ws.send(JSON.stringify({action:"type", typed}));
}

document.getElementById("input").addEventListener("input", (e)=>{
  if(!promptText) return;
  let typed = e.target.value;

  const i = correctPrefix(promptText, typed);
  if(i < typed.length){
    e.target.classList.add("bad");
    typed = typed.slice(0, i);
    e.target.value = typed;
  } else {
    e.target.classList.remove("bad");
  }

  lastGood = typed;
  renderPromptHighlight();
  sendTypeUpdate(typed);

  if(promptText && typed.length === promptText.length){
    ws.send(JSON.stringify({action:"finish", typed}));
    e.target.disabled = true;
  }
});


/* ---------- Podium + Confetti ---------- */
let confettiAnim = null;
let confettiParts = [];

function showPodium(podium){
  const medals = ["🥇","🥈","🥉"];
  const lines = (podium || []).map((p, idx)=>{
    const name = p.name;
    const t = (p.finish_time !== null && p.finish_time !== undefined) ? `${p.finish_time.toFixed(2)}s` : "DNF";
    const w = (p.net_wpm ?? p.wpm_live ?? 0);
    return `${medals[idx]} <b>${escapeHtml(name)}</b> — ${t} — WPM: ${Number(w).toFixed(1)}`;
  }).join("<br>");

  document.getElementById("podiumList").innerHTML = lines || "No results yet.";
  document.getElementById("podiumOverlay").style.display = "block";
  podiumShown = true;
  startConfetti();
}

function closePodium(){
  document.getElementById("podiumOverlay").style.display = "none";
  stopConfetti();
}

function startConfetti(){
  const canvas = document.getElementById("confetti");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(window.innerWidth * dpr);
  canvas.height = Math.floor(window.innerHeight * dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);

  const colors = ["#ff4d4d","#ffd24d","#4dff88","#4dd2ff","#9b4dff","#ff4df2"];
  confettiParts = Array.from({length: 200}, () => ({
    x: Math.random()*window.innerWidth,
    y: -20 - Math.random()*window.innerHeight*0.2,
    vx: (Math.random()-0.5)*6,
    vy: 3 + Math.random()*6,
    size: 6 + Math.random()*7,
    rot: Math.random()*Math.PI,
    vr: (Math.random()-0.5)*0.3,
    color: colors[Math.floor(Math.random()*colors.length)],
  }));

  const start = performance.now();
  function frame(now){
    ctx.clearRect(0,0,window.innerWidth,window.innerHeight);
    for(const p of confettiParts){
      p.x += p.vx; p.y += p.vy; p.rot += p.vr;
      if(p.y > window.innerHeight + 40) p.y = -40;
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.size/2, -p.size/2, p.size, p.size);
      ctx.restore();
    }
    if(now - start < 3000){
      confettiAnim = requestAnimationFrame(frame);
    } else {
      stopConfetti();
    }
  }
  stopConfetti();
  confettiAnim = requestAnimationFrame(frame);
}

function stopConfetti(){
  if(confettiAnim) cancelAnimationFrame(confettiAnim);
  confettiAnim = null;
  confettiParts = [];
  const canvas = document.getElementById("confetti");
  if(canvas){
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0,0,canvas.width,canvas.height);
  }
}
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


# Optional: eliminate Render health-check HEAD 405 noise
@app.head("/")
async def head_root():
    return ""


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    room_id = (websocket.query_params.get("room") or "default").strip()
    name = (websocket.query_params.get("name") or "Player").strip()[:30]

    room = rooms.setdefault(room_id, RoomState())
    room.clients.add(websocket)
    room.players_by_ws[websocket] = name
    room.players.setdefault(name, PlayerState(name=name))

    if room.host is None:
        room.host = name

    await websocket.send_text(json.dumps({"type": "welcome", "room": room_id, "you": name}))
    await broadcast(room_id, {"type": "room", **snapshot(room)})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            if action == "ready":
                pname = room.players_by_ws.get(websocket)
                if not pname:
                    continue
                room.players[pname].ready = bool(data.get("ready", False))
                await broadcast(room_id, {"type": "room", **snapshot(room)})

            elif action == "set_text":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only host can change single text."}))
                    continue

                new_text = (data.get("text") or "").strip()
                if len(new_text) < 10:
                    await websocket.send_text(json.dumps({"type":"error","message":"Text too short (min 10 chars)."}))
                    continue

                room.text_override = new_text
                await broadcast(room_id, {"type": "text_updated"})
                await broadcast(room_id, {"type": "room", **snapshot(room)})

            elif action == "clear_text":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type":"error","message":"Only host can clear single text."}))
                    continue
                room.text_override = ""
                await broadcast(room_id, {"type": "text_updated"})
                await broadcast(room_id, {"type": "room", **snapshot(room)})

            elif action == "set_pool":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type":"error","message":"Only host can set paragraph bank."}))
                    continue

                raw_list = data.get("text_list") or ""
                pool = parse_paragraphs(raw_list)
                if not pool:
                    await websocket.send_text(json.dumps({"type":"error","message":"No valid paragraphs. Separate by blank line."}))
                    continue

                room.text_pool = pool
                room.text_bag = []
                await broadcast(room_id, {"type":"pool_updated", "pool_size": len(room.text_pool)})
                await broadcast(room_id, {"type":"room", **snapshot(room)})

            elif action == "clear_pool":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type":"error","message":"Only host can clear paragraph bank."}))
                    continue
                room.text_pool = []
                room.text_bag = []
                await broadcast(room_id, {"type":"pool_updated", "pool_size": 0})
                await broadcast(room_id, {"type":"room", **snapshot(room)})

            elif action == "start_countdown":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type":"error","message":"Only host can start countdown."}))
                    continue

                if room.countdown_task and not room.countdown_task.done():
                    await websocket.send_text(json.dumps({"type":"error","message":"Countdown already running."}))
                    continue

                if not all_connected_ready(room):
                    await websocket.send_text(json.dumps({
                        "type":"error",
                        "message":"Not everyone is READY yet (Ready must equal Connected)."
                    }))
                    continue

                room.countdown_task = asyncio.create_task(
                    run_countdown_then_start(room_id, DEFAULT_COUNTDOWN)
                )

            elif action == "type":
                # live typing update
                pname = room.players_by_ws.get(websocket)
                if not pname or not room.started or not room.text:
                    continue

                typed = data.get("typed", "")
                ps = room.players[pname]

                ps.progress = correct_prefix_len(room.text, typed)

                elapsed = max(0.001, time.time() - room.start_time)
                minutes = elapsed / 60.0
                ps.wpm_live = round(((ps.progress / 5.0) / minutes), 1)

                await broadcast(room_id, {"type": "state", **snapshot(room)})

            elif action == "finish":
                pname = room.players_by_ws.get(websocket)
                if not pname or not room.started:
                    continue

                typed = data.get("typed", "")
                ps = room.players[pname]

                elapsed = max(0.001, time.time() - room.start_time)
                ps.progress = correct_prefix_len(room.text, typed)
                ps.done = (ps.progress == len(room.text))

                if ps.done and ps.finish_time is None:
                    ps.finish_time = elapsed
                    gwpm, nwpm, acc = compute_final_metrics(room.text, typed, elapsed)
                    ps.gross_wpm, ps.net_wpm, ps.accuracy = gwpm, nwpm, acc

                # Broadcast leaderboard update
                snap = snapshot(room)

                # If top-3 exists OR everyone finished -> broadcast podium once
                podium = compute_podium(room)
                connected = connected_player_names(room)
                all_done_connected = connected and all(room.players.get(n) and room.players[n].done for n in connected)

                if (len(podium) >= 3 or all_done_connected) and not room.podium_broadcasted:
                    room.podium_broadcasted = True
                    snap["podium"] = [
                        {
                            "name": n,
                            "finish_time": ps.finish_time,
                            "net_wpm": ps.net_wpm,
                            "wpm_live": ps.wpm_live,
                        } for (n, ps) in podium
                    ]

                await broadcast(room_id, {"type": "leaderboard", **snap})

    except WebSocketDisconnect:
        pass
    finally:
        room.clients.discard(websocket)
        room.players_by_ws.pop(websocket, None)

        if room.host == name:
            room.host = next(iter(room.players.keys()), None)

        await broadcast(room_id, {"type": "room", **snapshot(room)})
