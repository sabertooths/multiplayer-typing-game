from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

DEFAULT_TEXTS = [
    "Please review the remittance advice and confirm whether the beneficiary details match the invoice.",
    "After payment is released, we will inform MDM to re-block this vendor to avoid future postings.",
    "Kindly assist to approve the payment file. It has been fully signed off but is still pending approval.",
    "For SOA tracking, update the ticket status and include remarks for any exception handling required."
]


def now_mono() -> float:
    return time.monotonic()


async def safe_json_send(ws: WebSocket, payload: dict):
    await ws.send_text(json.dumps(payload))


@dataclass
class Player:
    name: str
    ws: WebSocket
    team: Optional[str] = None
    ready: bool = False

    progress: int = 0
    done: bool = False
    gross_wpm: float = 0.0
    net_wpm: float = 0.0
    accuracy: float = 0.0
    errors: int = 0


@dataclass
class RelayTeamState:
    name: str
    members: List[str] = field(default_factory=list)
    segments: List[str] = field(default_factory=list)
    runner_idx: int = 0
    segment_progress: int = 0
    finished: bool = False
    finish_time_sec: Optional[float] = None
    completed_chars: int = 0


@dataclass
class Room:
    room: str

    # Host is a SPECTATOR (not a player)
    host_name: Optional[str] = None
    host_ws: Optional[WebSocket] = None

    players: Dict[str, Player] = field(default_factory=dict)
    spectators: Dict[str, WebSocket] = field(default_factory=dict)

    started: bool = False
    mode: Optional[str] = None  # "ffa" or "relay"
    text: str = ""
    start_mono: float = 0.0

    relay_teams: Dict[str, RelayTeamState] = field(default_factory=dict)


rooms: Dict[str, Room] = {}


async def broadcast(room: Room, payload: dict):
    dead_players = []
    dead_specs = []

    # players
    for name, p in room.players.items():
        try:
            await safe_json_send(p.ws, payload)
        except Exception:
            dead_players.append(name)

    # spectators (including host if stored here)
    for name, ws in room.spectators.items():
        try:
            await safe_json_send(ws, payload)
        except Exception:
            dead_specs.append(name)

    # host_ws separate (optional redundancy; safe to send if exists)
    if room.host_ws is not None:
        try:
            await safe_json_send(room.host_ws, payload)
        except Exception:
            room.host_ws = None
            room.host_name = None

    for name in dead_players:
        room.players.pop(name, None)
    for name in dead_specs:
        room.spectators.pop(name, None)

    # If host spectator disconnected, keep room running but no host
    # (Players can still play; you can re-join host later)
    if room.host_name and room.host_ws is None:
        room.host_name = None


def room_snapshot(room: Room) -> dict:
    scores = {}
    for name, p in room.players.items():
        scores[name] = {
            "progress": p.progress,
            "done": p.done,
            "gross_wpm": round(p.gross_wpm, 2),
            "net_wpm": round(p.net_wpm, 2),
            "accuracy": round(p.accuracy, 1),
            "errors": p.errors,
            "team": p.team,
            "ready": p.ready,
        }

    relay = None
    if room.mode == "relay":
        relay = {}
        for tname, t in room.relay_teams.items():
            relay[tname] = {
                "members": t.members,
                "runner": t.members[t.runner_idx] if t.members and t.runner_idx < len(t.members) else None,
                "progress": min(100, round(((t.completed_chars + t.segment_progress) / max(1, len(room.text))) * 100)),
                "finished": t.finished,
                "finish_time_sec": t.finish_time_sec,
            }

    return {
        "type": "room",
        "room": room.room,
        "host": room.host_name,                 # host is spectator
        "started": room.started,
        "mode": room.mode,
        "text_len": len(room.text) if room.text else 0,
        "scores": scores,                       # ONLY players
        "spectators": len(room.spectators) + (1 if room.host_ws else 0),
        "relay": relay,
    }


def compute_stats(text: str, typed: str, elapsed_sec: float) -> dict:
    if elapsed_sec <= 0:
        elapsed_sec = 0.001
    target = text
    typed_eff = typed[: len(target)]
    correct = sum(1 for i, ch in enumerate(typed_eff) if i < len(target) and ch == target[i])
    errors = len(typed_eff) - correct
    minutes = elapsed_sec / 60.0
    gross_wpm = ((len(typed_eff) / 5.0) / minutes) if minutes > 0 else 0.0
    net_wpm = ((correct / 5.0) / minutes) if minutes > 0 else 0.0
    accuracy = (correct / len(typed_eff) * 100.0) if len(typed_eff) > 0 else 0.0
    return {
        "gross_wpm": gross_wpm,
        "net_wpm": net_wpm,
        "accuracy": accuracy,
        "errors": errors,
        "progress": len(typed_eff),
        "done": True,
    }


def auto_assign_teams(room: Room):
    names = list(room.players.keys())
    any_team = any(room.players[n].team for n in names)
    if any_team:
        for n in names:
            if not room.players[n].team:
                room.players[n].team = "A"
        return
    for i, n in enumerate(names):
        room.players[n].team = "A" if i % 2 == 0 else "B"


def build_relay_segments(text: str, n_segments: int) -> List[str]:
    words = text.split(" ")
    if n_segments <= 1 or len(words) <= 1:
        return [text]
    base = len(words) // n_segments
    rem = len(words) % n_segments
    segs = []
    idx = 0
    for k in range(n_segments):
        take = base + (1 if k < rem else 0)
        segs.append(" ".join(words[idx : idx + take]))
        idx += take
    return segs


async def start_ffa(room: Room, text: str):
    room.started = True
    room.mode = "ffa"
    room.text = text
    room.start_mono = now_mono()

    for p in room.players.values():
        p.ready = False
        p.progress = 0
        p.done = False
        p.gross_wpm = p.net_wpm = p.accuracy = 0.0
        p.errors = 0

    await broadcast(room, room_snapshot(room))
    await broadcast(room, {"type": "round", "mode": "ffa", "text": room.text})


async def start_relay(room: Room, text: str):
    room.started = True
    room.mode = "relay"
    room.text = text
    room.start_mono = now_mono()

    for p in room.players.values():
        p.ready = False
        p.progress = 0
        p.done = False
        p.gross_wpm = p.net_wpm = p.accuracy = 0.0
        p.errors = 0

    auto_assign_teams(room)
    room.relay_teams = {}

    for name, p in room.players.items():
        tname = p.team or "A"
        room.relay_teams.setdefault(tname, RelayTeamState(name=tname)).members.append(name)

    for tname, t in room.relay_teams.items():
        t.segments = build_relay_segments(text, max(1, len(t.members)))
        t.runner_idx = 0
        t.segment_progress = 0
        t.finished = False
        t.finish_time_sec = None
        t.completed_chars = 0

    await broadcast(room, room_snapshot(room))

    # Send initial segment only to runners
    for tname, t in room.relay_teams.items():
        if not t.members:
            continue
        runner = t.members[0]
        for member in t.members:
            if member == runner:
                await safe_json_send(room.players[member].ws, {
                    "type": "relay_segment",
                    "team": tname,
                    "runner": member,
                    "segment_index": 0,
                    "segment_total": len(t.segments),
                    "text": t.segments[0]
                })
            else:
                await safe_json_send(room.players[member].ws, {
                    "type": "relay_wait",
                    "team": tname,
                    "runner": runner
                })


async def advance_relay(room: Room, team_name: str):
    t = room.relay_teams[team_name]
    seg_text = t.segments[t.runner_idx]
    t.completed_chars += len(seg_text)
    t.segment_progress = 0
    t.runner_idx += 1

    if t.runner_idx >= len(t.segments):
        t.finished = True
        t.finish_time_sec = now_mono() - room.start_mono
        await broadcast(room, room_snapshot(room))
        if all(tt.finished for tt in room.relay_teams.values() if tt.members):
            room.started = False
            await broadcast(room, {"type": "relay_finished"})
        return

    runner = t.members[t.runner_idx]
    await broadcast(room, room_snapshot(room))
    for member in t.members:
        if member == runner:
            await safe_json_send(room.players[member].ws, {
                "type": "relay_segment",
                "team": team_name,
                "runner": runner,
                "segment_index": t.runner_idx,
                "segment_total": len(t.segments),
                "text": t.segments[t.runner_idx]
            })
        else:
            await safe_json_send(room.players[member].ws, {
                "type": "relay_wait",
                "team": team_name,
                "runner": runner
            })


# -------------------------
# Client HTML (adds Spectator checkbox)
# -------------------------
CLIENT_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Multiplayer Typing</title>
  <style>
    body { font-family: Segoe UI, Arial; max-width: 980px; margin: 24px auto; }
    .card { padding: 12px; border: 1px solid #ddd; border-radius: 10px; margin-top: 12px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
    input, select { padding: 6px; }
    button { padding: 8px 12px; margin-right: 8px; }
    .muted { color:#666; }
    #promptRender { font-family: Consolas, "Courier New", monospace; white-space: pre-wrap; line-height: 1.55; }
    .ch { padding: 0 0.2px; border-radius: 3px; }
    .ok { background: #e8f7ea; color: #1a7f2e; }
    .bad { background: #fde7e9; color: #b42318; }
    .cur { outline: 2px solid #2563eb; outline-offset: 1px; }
    .rest { color: #111; }
    .dim { color: #888; }
    textarea { width: 100%; font-size: 16px; padding: 10px; border-radius: 8px; border: 1px solid #ccc; }
    table { width:100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align:left; }
    progress { width: 160px; }
    .pill { display:inline-block; padding:2px 8px; border-radius: 999px; background:#f3f4f6; }
    .warn { color:#b45309; }
  </style>
</head>
<body>
  <h2>Multiplayer Typing (Host = Spectator)</h2>

  <div class="card">
    <div class="row">
      <label>Name <input id="name" value="Host"></label>
      <label>Room <input id="room" value="soa-recon"></label>
      <label>Team
        <select id="team">
          <option value="">Auto</option>
          <option value="A">A</option>
          <option value="B">B</option>
          <option value="C">C</option>
        </select>
      </label>

      <label class="pill">
        <input type="checkbox" id="isSpectator" checked>
        Spectator (Host)
      </label>

      <button id="joinBtn" onclick="join()">Join</button>
      <button id="readyBtn" onclick="toggleReady()" disabled>Ready</button>
      <button id="startFfaBtn" onclick="startRound('ffa')" disabled>Start FFA</button>
      <button id="startRelayBtn" onclick="startRound('relay')" disabled>Start Relay</button>

      <span class="muted" id="status">Not connected</span>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="pill" id="readyCount">Ready: 0/0</span>
      <span class="pill" id="modePill">Mode: -</span>
      <span class="pill" id="specCount">Spectators: 0</span>
      <span class="muted" id="hostInfo"></span>
    </div>
  </div>

  <div class="card" id="promptCard" style="display:none;">
    <h3>Prompt</h3>
    <div id="promptMeta" class="muted"></div>
    <div id="promptRender" class="card"></div>
    <p class="muted">Wrong characters are highlighted in red; typing continues.</p>
    <textarea id="input" rows="3" placeholder="Type here..." disabled></textarea>
    <div class="row">
      <button id="finishBtn" onclick="finish()" disabled>Finish</button>
      <span class="pill">Gross WPM: <b id="gWpm">0</b></span>
      <span class="pill">Net WPM: <b id="nWpm">0</b></span>
      <span class="pill">Acc: <b id="acc">0</b>%</span>
      <span class="pill warn">Errors: <b id="errs">0</b></span>
    </div>
  </div>

  <div class="card">
    <h3>Leaderboard (Players only)</h3>
    <table>
      <thead>
        <tr>
          <th>Player</th><th>Team</th><th>Ready</th><th>Progress</th>
          <th>Gross WPM</th><th>Net WPM</th><th>Accuracy</th><th>Errors</th><th>Status</th>
        </tr>
      </thead>
      <tbody id="board"></tbody>
    </table>

    <div id="relayBoard" class="card" style="display:none; margin-top:12px">
      <h4>Relay Teams</h4>
      <div id="relayInfo" class="muted"></div>
    </div>
  </div>

<script>
let ws;
let you = null;
let yourRole = "player"; // "player" or "spectator"
let roomState = { host:null, started:false, mode:null, text_len:0, scores:{}, spectators:0, relay:null };
let round = { mode:null, text:"", startAt:0 };

function setStatus(t){ document.getElementById("status").innerText = t; }
function setModePill(){ document.getElementById("modePill").innerText = "Mode: " + (roomState.mode || "-"); }
function setSpecCount(){ document.getElementById("specCount").innerText = "Spectators: " + (roomState.spectators || 0); }

function serverWsUrl(){
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

function renderReadyCount(){
  const scores = roomState.scores || {};
  const total = Object.keys(scores).length;
  const ready = Object.values(scores).filter(s => s.ready).length;
  document.getElementById("readyCount").innerText = `Ready: ${ready}/${total}`;
}

function renderBoard(){
  const tbody = document.getElementById("board");
  tbody.innerHTML = "";
  const entries = Object.entries(roomState.scores || {});
  entries.sort((a,b)=>{
    const A=a[1], B=b[1];
    if ((B.done?1:0)!==(A.done?1:0)) return (B.done?1:0)-(A.done?1:0);
    if ((B.net_wpm||0)!==(A.net_wpm||0)) return (B.net_wpm||0)-(A.net_wpm||0);
    return (B.progress||0)-(A.progress||0);
  });

  for(const [name,s] of entries){
    const pct = roomState.text_len ? Math.min(100, Math.round((s.progress||0)/roomState.text_len*100)) : 0;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${name}</td>
      <td>${s.team || ""}</td>
      <td>${s.ready ? "✅" : ""}</td>
      <td><progress max="100" value="${pct}"></progress> ${pct}%</td>
      <td>${s.gross_wpm ?? 0}</td>
      <td>${s.net_wpm ?? 0}</td>
      <td>${s.accuracy ?? 0}%</td>
      <td>${s.errors ?? 0}</td>
      <td>${s.done ? "✅ done" : "…"}</td>
    `;
    tbody.appendChild(tr);
  }

  renderReadyCount();
  setModePill();
  setSpecCount();

  const isHost = (you && roomState.host && you === roomState.host);
  document.getElementById("startFfaBtn").disabled = !isHost;
  document.getElementById("startRelayBtn").disabled = !isHost;

  document.getElementById("hostInfo").innerText =
    isHost ? "You are host (spectator)." : (roomState.host ? `Host: ${roomState.host}` : "");
}

function renderRelay(){
  const box = document.getElementById("relayBoard");
  const info = document.getElementById("relayInfo");
  if (roomState.mode !== "relay" || !roomState.relay){
    box.style.display = "none";
    return;
  }
  box.style.display = "block";
  const lines = [];
  for(const [tname, t] of Object.entries(roomState.relay)){
    lines.push(`Team ${tname}: runner=${t.runner || "-"}, progress=${t.progress}% ${t.finished ? "(finished)" : ""}`);
  }
  info.innerText = lines.join(" | ");
}

function computeLocalStats(target, typed){
  const t = target || "";
  const x = (typed || "").slice(0, t.length);
  let correct = 0;
  for(let i=0;i<x.length;i++){ if (x[i] === t[i]) correct++; }
  const errors = x.length - correct;
  const elapsedSec = Math.max(0.001, (Date.now()-round.startAt)/1000);
  const min = elapsedSec/60;
  const gross = (x.length/5)/min;
  const net = (correct/5)/min;
  const acc = x.length ? (correct/x.length*100) : 0;
  return {gross, net, acc, errors, progress:x.length};
}

function renderPrompt(target, typed){
  const el = document.getElementById("promptRender");
  const t = target || "";
  const x = (typed || "").slice(0, t.length);
  const cur = x.length;
  let html = "";
  for(let i=0;i<t.length;i++){
    const ch = t[i];
    let cls = "ch rest";
    if (i < x.length) cls = (x[i] === ch) ? "ch ok" : "ch bad";
    else if (i === cur) cls = "ch rest cur";
    else cls = "ch dim";
    const safe = ch === "<" ? "&lt;" : (ch === ">" ? "&gt;" : (ch === "&" ? "&amp;" : ch));
    html += `<span class="${cls}">${safe}</span>`;
  }
  el.innerHTML = html;
}

function setTypingEnabled(on){
  document.getElementById("input").disabled = !on;
  document.getElementById("finishBtn").disabled = !on;
  if (on) setTimeout(()=>document.getElementById("input").focus(), 50);
}

function join(){
  const url = serverWsUrl();
  ws = new WebSocket(url);

  ws.onopen = () => {
    you = document.getElementById("name").value.trim() || "User";
    const room = document.getElementById("room").value.trim() || "default";
    const team = document.getElementById("team").value.trim();
    yourRole = document.getElementById("isSpectator").checked ? "spectator" : "player";

    ws.send(JSON.stringify({action:"join", name:you, room:room, team:team, role:yourRole}));
    setStatus("Connected");
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if(msg.type === "joined"){
      you = msg.you;
      document.getElementById("promptCard").style.display = "block";

      // Spectator cannot Ready or type
      document.getElementById("readyBtn").disabled = (yourRole !== "player");
      if (yourRole === "spectator"){
        setTypingEnabled(false);
        document.getElementById("promptMeta").innerText = "Spectator view (Host).";
      }
      setStatus(`Joined room: ${msg.room} as ${yourRole}`);
    }

    if(msg.type === "room"){
      roomState = msg;
      renderBoard();
      renderRelay();
    }

    // Players receive round events; spectators also receive them but won't type
    if(msg.type === "round"){
      round.mode = msg.mode;
      round.text = msg.text;
      round.startAt = Date.now();

      document.getElementById("promptMeta").innerText = "FFA: Everyone types the full text.";
      document.getElementById("input").value = "";
      renderPrompt(round.text, "");

      if (yourRole === "player"){
        setTypingEnabled(true);
      } else {
        setTypingEnabled(false);
      }
      setStatus("FFA started!");
    }

    if(msg.type === "relay_segment"){
      round.mode = "relay";
      round.text = msg.text;
      round.startAt = Date.now();

      document.getElementById("promptMeta").innerText =
        `Relay: Team ${msg.team} | ${yourRole === "player" ? "Your turn" : "Spectator view"} (${msg.segment_index+1}/${msg.segment_total})`;
      document.getElementById("input").value = "";
      renderPrompt(round.text, "");

      if (yourRole === "player"){
        setTypingEnabled(true);
      } else {
        setTypingEnabled(false);
      }
      setStatus("Relay segment started!");
    }

    if(msg.type === "relay_wait"){
      round.mode = "relay";
      document.getElementById("promptMeta").innerText =
        `Relay: Team ${msg.team} | Waiting... Current runner: ${msg.runner}`;
      document.getElementById("input").value = "";
      renderPrompt("", "");
      setTypingEnabled(false);
      setStatus("Waiting...");
    }

    if(msg.type === "relay_finished"){
      setTypingEnabled(false);
      setStatus("Relay finished ✅");
    }

    if(msg.type === "error"){
      alert(msg.message);
    }
  };

  ws.onclose = () => setStatus("Disconnected");
}

function toggleReady(){
  ws.send(JSON.stringify({action:"ready"}));
}

function startRound(mode){
  ws.send(JSON.stringify({action:"start", mode:mode}));
}

let lastProgSent = 0;
function sendProgress(p){
  const now = Date.now();
  if(now - lastProgSent < 160) return;
  lastProgSent = now;
  ws.send(JSON.stringify({action:"progress", progress:p}));
}

document.getElementById("input").addEventListener("input", (e)=>{
  if (yourRole !== "player") return; // spectators never type

  const typed = e.target.value;
  renderPrompt(round.text, typed);

  const s = computeLocalStats(round.text, typed);
  document.getElementById("gWpm").innerText = s.gross.toFixed(1);
  document.getElementById("nWpm").innerText = s.net.toFixed(1);
  document.getElementById("acc").innerText = s.acc.toFixed(1);
  document.getElementById("errs").innerText = s.errors;

  sendProgress(s.progress);
});

function finish(){
  if (yourRole !== "player") return;
  const typed = document.getElementById("input").value || "";
  ws.send(JSON.stringify({action:"finish", typed:typed}));
  setTypingEnabled(false);
  setStatus("Submitted ✅");
}
</script>
</body>
</html>
"""

@app.get("/")
def home():
    return HTMLResponse(CLIENT_HTML)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    room: Optional[Room] = None
    name: Optional[str] = None
    role: str = "player"

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")

            if action == "join":
                room_name = (msg.get("room") or "default").strip()
                name = (msg.get("name") or "User").strip()
                team = (msg.get("team") or "").strip() or None
                role = (msg.get("role") or "player").strip().lower()

                room = rooms.get(room_name)
                if not room:
                    room = Room(room=room_name)
                    rooms[room_name] = room

                # unique name among BOTH players & spectators & host
                base = name
                i = 2
                occupied = set(room.players.keys()) | set(room.spectators.keys())
                if room.host_name:
                    occupied.add(room.host_name)

                while name in occupied:
                    name = f"{base}{i}"
                    i += 1

                if role == "spectator":
                    # If no host yet, first spectator becomes host
                    if room.host_name is None:
                        room.host_name = name
                        room.host_ws = ws
                    else:
                        room.spectators[name] = ws
                else:
                    room.players[name] = Player(name=name, ws=ws, team=team)

                await safe_json_send(ws, {"type": "joined", "you": name, "room": room_name})
                await broadcast(room, room_snapshot(room))
                continue

            if not room or not name:
                await safe_json_send(ws, {"type": "error", "message": "Join a room first."})
                continue

            # host check (host is spectator)
            is_host = (room.host_name == name) and (room.host_ws == ws)

            if action == "start":
                if not is_host:
                    await safe_json_send(ws, {"type": "error", "message": "Only host (spectator) can start."})
                    continue

                mode = (msg.get("mode") or "ffa").lower()
                if not room.text:
                    DEFAULT_TEXTS.append(DEFAULT_TEXTS.pop(0))
                    room.text = DEFAULT_TEXTS[0]

                if mode == "ffa":
                    await start_ffa(room, room.text)
                elif mode == "relay":
                    await start_relay(room, room.text)
                else:
                    await safe_json_send(ws, {"type": "error", "message": "Unknown mode."})
                continue

            # The rest are PLAYER-only actions
            if name not in room.players:
                # spectators/host cannot ready/progress/finish
                await safe_json_send(ws, {"type": "error", "message": "Spectators cannot perform this action."})
                continue

            player = room.players[name]

            if action == "ready":
                player.ready = not player.ready
                await broadcast(room, room_snapshot(room))

            elif action == "progress":
                prog = int(msg.get("progress") or 0)
                player.progress = max(0, prog)
                await broadcast(room, room_snapshot(room))

                if room.mode == "relay" and room.started:
                    team_name = player.team or "A"
                    if team_name in room.relay_teams:
                        t = room.relay_teams[team_name]
                        current_runner = t.members[t.runner_idx] if t.members else None
                        if current_runner == name and not t.finished:
                            t.segment_progress = min(len(t.segments[t.runner_idx]), prog)
                            await broadcast(room, room_snapshot(room))
                            if t.segment_progress >= len(t.segments[t.runner_idx]):
                                await advance_relay(room, team_name)

            elif action == "finish":
                typed = msg.get("typed") or ""
                if room.mode == "ffa" and room.started:
                    elapsed = now_mono() - room.start_mono
                    stats = compute_stats(room.text, typed, elapsed)
                    player.progress = stats["progress"]
                    player.done = True
                    player.gross_wpm = stats["gross_wpm"]
                    player.net_wpm = stats["net_wpm"]
                    player.accuracy = stats["accuracy"]
                    player.errors = stats["errors"]
                    await broadcast(room, room_snapshot(room))
                    if all(p.done for p in room.players.values()):
                        room.started = False
                        await broadcast(room, {"type": "ffa_finished"})

                elif room.mode == "relay" and room.started:
                    team_name = player.team or "A"
                    if team_name not in room.relay_teams:
                        continue
                    t = room.relay_teams[team_name]
                    current_runner = t.members[t.runner_idx] if t.members else None
                    if current_runner != name:
                        continue

                    elapsed = now_mono() - room.start_mono
                    seg_text = t.segments[t.runner_idx]
                    stats = compute_stats(seg_text, typed, elapsed)
                    player.done = True
                    player.gross_wpm = stats["gross_wpm"]
                    player.net_wpm = stats["net_wpm"]
                    player.accuracy = stats["accuracy"]
                    player.errors = stats["errors"]
                    player.progress = len(seg_text)

                    await broadcast(room, room_snapshot(room))
                    await advance_relay(room, team_name)

            else:
                await safe_json_send(ws, {"type": "error", "message": "Unknown action."})

    except WebSocketDisconnect:
        pass
    finally:
        # cleanup on disconnect
        if room and name:
            if room.host_name == name and room.host_ws == ws:
                room.host_ws = None
                room.host_name = None
            room.players.pop(name, None)
            room.spectators.pop(name, None)
            await broadcast(room, room_snapshot(room))
