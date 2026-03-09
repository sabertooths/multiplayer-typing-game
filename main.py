import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# Fallback texts (used only when host doesn't set a custom text)
TEXTS = [
    "SOA reconciliation improves completeness and reduces operational risk.",
    "Automation helps track SOA tickets even when some were not downloaded.",
    "Accurate SOA recon needs visibility, status tracking, and timely follow-up.",
    "Reducing manual updates lowers error risk and saves daily processing time.",
]

DEFAULT_COUNTDOWN = 5  # seconds

# ------------------- Game State -------------------
@dataclass
class PlayerState:
    name: str
    ready: bool = False
    progress: int = 0
    done: bool = False
    gross_wpm: float = 0.0
    net_wpm: float = 0.0
    accuracy: float = 0.0

@dataclass
class RoomState:
    clients: Set[WebSocket] = field(default_factory=set)
    players_by_ws: Dict[WebSocket, str] = field(default_factory=dict)
    players: Dict[str, PlayerState] = field(default_factory=dict)

    host: Optional[str] = None

    # text_override: if set, always use this text for new rounds until cleared
    text_override: str = ""

    # current round text
    text: str = ""
    start_time: float = 0.0
    started: bool = False

    countdown_task: Optional[asyncio.Task] = None
    countdown_active: bool = False
    countdown_remaining: int = 0

rooms: Dict[str, RoomState] = {}


def compute_metrics(prompt: str, typed: str, elapsed_sec: float):
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


def connected_player_names(room: RoomState) -> Set[str]:
    return set(room.players_by_ws.values())


def ready_count(room: RoomState) -> int:
    connected = connected_player_names(room)
    return sum(1 for n in connected if room.players.get(n) and room.players[n].ready)


def connected_count(room: RoomState) -> int:
    return len(connected_player_names(room))


def all_connected_ready(room: RoomState) -> bool:
    cc = connected_count(room)
    return cc > 0 and ready_count(room) == cc


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
        "scores": {
            name: {
                "ready": ps.ready,
                "progress": ps.progress,
                "done": ps.done,
                "gross_wpm": ps.gross_wpm,
                "net_wpm": ps.net_wpm,
                "accuracy": ps.accuracy,
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

        # Start the round
        room = rooms.get(room_id)
        if not room:
            return

        # Use custom text if provided; otherwise fallback to random
        if room.text_override.strip():
            room.text = room.text_override.strip()
        else:
            room.text = random.choice(TEXTS)

        room.started = True
        room.start_time = time.time()
        room.countdown_active = False
        room.countdown_remaining = 0

        # Reset round stats and reset ready to require Ready again next round
        for ps in room.players.values():
            ps.progress = 0
            ps.done = False
            ps.gross_wpm = ps.net_wpm = ps.accuracy = 0.0
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


# ------------------- One-URL Web Page -------------------
HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Multiplayer Typing Game</title>
  <style>
    body { font-family: Segoe UI, Arial; max-width: 980px; margin: 24px auto; }
    .card { padding: 12px; border: 1px solid #ddd; border-radius: 8px; margin-top: 12px; }
    #prompt { background: #f5f5f5; white-space: pre-wrap; }
    textarea { width: 100%; font-size: 16px; padding: 10px; }
    button { padding: 8px 12px; margin-right: 8px; }
    .muted { color:#666; }
    .row { display:flex; gap:12px; flex-wrap: wrap; align-items:center; }
    input { padding: 6px; }
    table { width:100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align:left; }
    progress { width: 180px; }
    .badge { display:inline-block; padding:2px 8px; border-radius: 999px; font-size: 12px; }
    .badge-ready { background:#e8fff0; color:#0b6b2a; border:1px solid #b7f0c9; }
    .badge-notready { background:#fff3f3; color:#8a1f1f; border:1px solid #ffd0d0; }
    .countdown { font-weight:700; font-size: 18px; }
    .right { margin-left:auto; }
  </style>
</head>
<body>
  <h2>Multiplayer Typing Game (Custom Text + Ready Count)</h2>

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
    <div class="muted">Share this link to others: <b>http://HOST_IP:8000</b></div>
  </div>

  <div class="card" id="hostPanel" style="display:none;">
    <h3>Host Controls (Change typing text without editing code)</h3>
    <div class="muted">
      Paste any text below. It will be used for the <b>next round</b> (and stays until you clear it).
    </div>
    <textarea id="customText" rows="4" placeholder="Paste your paragraph here..."></textarea>
    <div class="row">
      <button onclick="updateText()">Update Text</button>
      <button onclick="clearText()">Clear Custom Text</button>
      <span class="muted" id="textMode">Mode: Random</span>
    </div>
  </div>

  <div class="card" id="promptCard" style="display:none;">
    <h3>Prompt</h3>
    <div id="prompt" class="card"></div>
    <p class="muted">Wait for countdown → type → click Finish.</p>
    <textarea id="input" rows="3" placeholder="Type here..." disabled></textarea>
    <div class="row">
      <button id="finishBtn" onclick="finish()" disabled>Finish</button>
      <span class="muted" id="hostInfo"></span>
    </div>
  </div>

  <div class="card">
    <h3>Leaderboard</h3>
    <table>
      <thead>
        <tr>
          <th>Player</th>
          <th>Ready</th>
          <th>Progress</th>
          <th>Gross WPM</th>
          <th>Net WPM</th>
          <th>Accuracy</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="board"></tbody>
    </table>
  </div>

<script>
let ws;
let you = null;
let isReady = false;
let state = { host:null, text_len:0, scores:{}, countdown_active:false, countdown_remaining:0,
              ready_count:0, connected_count:0, has_custom_text:false };

function setStatus(t){ document.getElementById("status").innerText = t; }
function setCountdownText(t){ document.getElementById("cd").innerText = t ? ("⏳ " + t) : ""; }

function renderBoard(){
  const tbody = document.getElementById("board");
  tbody.innerHTML = "";
  const entries = Object.entries(state.scores || {});
  entries.sort((a,b)=>{
    const A=a[1], B=b[1];
    if ((B.done?1:0) !== (A.done?1:0)) return (B.done?1:0)-(A.done?1:0);
    if ((B.net_wpm||0) !== (A.net_wpm||0)) return (B.net_wpm||0)-(A.net_wpm||0);
    return (B.progress||0)-(A.progress||0);
  });

  for(const [name, s] of entries){
    const pct = state.text_len ? Math.min(100, Math.round((s.progress||0)/state.text_len*100)) : 0;
    const readyBadge = s.ready
      ? '<span class="badge badge-ready">READY</span>'
      : '<span class="badge badge-notready">NO</span>';
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${name}${name===state.host ? " 👑" : ""}</td>
      <td>${readyBadge}</td>
      <td><progress max="100" value="${pct}"></progress> ${pct}%</td>
      <td>${s.gross_wpm ?? 0}</td>
      <td>${s.net_wpm ?? 0}</td>
      <td>${s.accuracy ?? 0}%</td>
      <td>${s.done ? "✅ done" : "…"}</td>
    `;
    tbody.appendChild(tr);
  }
}

function wsUrl(){
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const host = window.location.host; // e.g. typing-game.onrender.com
  const r = encodeURIComponent(document.getElementById("room").value.trim());
  const n = encodeURIComponent(document.getElementById("name").value.trim());
  return `${protocol}://${host}/ws?room=${r}&name=${n}`;
}

function join(){
  ws = new WebSocket(wsUrl());
  ws.onopen = () => { setStatus("Connected"); };
  ws.onclose = () => { setStatus("Disconnected"); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if(msg.type === "welcome"){
      you = msg.you;
      document.getElementById("promptCard").style.display = "block";
      document.getElementById("readyBtn").disabled = false;
    }

    if(msg.type === "room" || msg.type === "leaderboard"){
      state.host = msg.host;
      state.text_len = msg.text_len || 0;
      state.scores = msg.scores || {};
      state.countdown_active = msg.countdown_active || false;
      state.countdown_remaining = msg.countdown_remaining || 0;
      state.ready_count = msg.ready_count || 0;
      state.connected_count = msg.connected_count || 0;
      state.has_custom_text = msg.has_custom_text || false;

      document.getElementById("readyCount").innerText =
        `Ready: ${state.ready_count}/${state.connected_count}`;

      const host = (you && you === state.host);
      document.getElementById("startBtn").disabled = !host;
      document.getElementById("hostPanel").style.display = host ? "block" : "none";
      document.getElementById("textMode").innerText = state.has_custom_text ? "Mode: Custom text" : "Mode: Random";

      document.getElementById("hostInfo").innerText =
        host ? "You are host. Start countdown when Ready = Connected." :
        (state.host ? `Host: ${state.host}` : "");

      if(state.countdown_active && state.countdown_remaining){
        setCountdownText(state.countdown_remaining + "s");
      } else {
        setCountdownText("");
      }

      renderBoard();
    }

    if(msg.type === "countdown"){
      setCountdownText(msg.seconds + "s");
      setStatus("Countdown...");
      document.getElementById("input").disabled = true;
      document.getElementById("finishBtn").disabled = true;
    }

    if(msg.type === "round"){
      setCountdownText("");
      document.getElementById("prompt").innerText = msg.text;
      const inp = document.getElementById("input");
      inp.disabled = false;
      document.getElementById("finishBtn").disabled = false;
      inp.value = "";
      inp.focus();
      setStatus("GO!");

      // reset local ready state for next round
      isReady = false;
      document.getElementById("readyBtn").innerText = "Ready ✅";
    }

    if(msg.type === "progress"){
      if(state.scores && state.scores[msg.name]){
        state.scores[msg.name].progress = msg.progress;
        renderBoard();
      }
    }

    if(msg.type === "text_updated"){
      setStatus("Text updated ✅ (next round)");
    }

    if(msg.type === "error"){
      alert(msg.message);
    }
  };
}

function toggleReady(){
  if(!ws) return;
  isReady = !isReady;
  ws.send(JSON.stringify({action:"ready", ready: isReady}));
  document.getElementById("readyBtn").innerText = isReady ? "Unready ❌" : "Ready ✅";
}

function startCountdown(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"start_countdown"}));
}

function updateText(){
  if(!ws) return;
  const t = document.getElementById("customText").value;
  ws.send(JSON.stringify({action:"set_text", text: t}));
}

function clearText(){
  if(!ws) return;
  ws.send(JSON.stringify({action:"clear_text"}));
}

let lastProgSent = 0;
function sendProgress(p){
  const now = Date.now();
  if(now - lastProgSent < 160) return;
  lastProgSent = now;
  ws.send(JSON.stringify({action:"progress", progress: p}));
}

document.addEventListener("input", (e)=>{
  if(e.target && e.target.id === "input"){
    sendProgress(e.target.value.length);
  }
});

function finish(){
  const typed = document.getElementById("input").value;
  ws.send(JSON.stringify({action:"finish", typed}));
  document.getElementById("input").disabled = true;
  document.getElementById("finishBtn").disabled = true;
  setStatus("Submitted ✅");
}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Standard FastAPI WebSocket lifecycle: accept -> loop receive/send [1](https://fastapi.tiangolo.com/advanced/websockets/)
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
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only host can change text."}))
                    continue

                new_text = (data.get("text") or "").strip()
                if len(new_text) < 10:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Text too short (min 10 chars)."}))
                    continue

                room.text_override = new_text
                await broadcast(room_id, {"type": "text_updated"})
                await broadcast(room_id, {"type": "room", **snapshot(room)})

            elif action == "clear_text":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only host can clear text."}))
                    continue
                room.text_override = ""
                await broadcast(room_id, {"type": "text_updated"})
                await broadcast(room_id, {"type": "room", **snapshot(room)})

            elif action == "start_countdown":
                starter = room.players_by_ws.get(websocket)
                if starter != room.host:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only host can start countdown."}))
                    continue

                if room.countdown_task and not room.countdown_task.done():
                    await websocket.send_text(json.dumps({"type": "error", "message": "Countdown already running."}))
                    continue

                if not all_connected_ready(room):
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Not everyone is READY yet (Ready must equal Connected)."
                    }))
                    continue

                room.countdown_task = asyncio.create_task(
                    run_countdown_then_start(room_id, DEFAULT_COUNTDOWN)
                )

            elif action == "progress":
                pname = room.players_by_ws.get(websocket)
                if not pname:
                    continue
                prog = int(data.get("progress", 0))
                room.players[pname].progress = max(0, prog)
                await broadcast(room_id, {"type": "progress", "name": pname, "progress": room.players[pname].progress})

            elif action == "finish":
                pname = room.players_by_ws.get(websocket)
                if not pname or not room.started:
                    continue
                typed = data.get("typed", "")
                elapsed = max(0.001, time.time() - room.start_time)

                gwpm, nwpm, acc = compute_metrics(room.text, typed, elapsed)
                ps = room.players[pname]
                ps.done = True
                ps.gross_wpm = gwpm
                ps.net_wpm = nwpm
                ps.accuracy = acc
                ps.progress = len(typed)

                await broadcast(room_id, {"type": "leaderboard", **snapshot(room)})

    except WebSocketDisconnect:
        pass
    finally:
        room.clients.discard(websocket)
        room.players_by_ws.pop(websocket, None)

        # If host left, pick another (first existing player)
        if room.host == name:
            room.host = next(iter(room.players.keys()), None)

        await broadcast(room_id, {"type": "room", **snapshot(room)})
