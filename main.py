from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from collections import deque
import uuid, time, json, asyncio, os

app = FastAPI()
templates = Jinja2Templates(directory="templates")

sessions = {}  # session_id -> {game_id, color, username, created}
games = {}     # game_id -> GameState

PLAYER_COLORS = {"red": "#e74c3c", "blue": "#3498db"}
WALL_COLORS   = {"red": "#c0392b", "blue": "#2980b9"}

SESSION_TTL   = 3600 * 24   # 24h  — remove from file storage
RAM_TTL       = 3600        # 1h   — evict from RAM

SESSIONS_FILE = "sessions_store.json"

# ── persistence helpers ────────────────────────────────────────────────────────

def _load_sessions_from_disk():
    """Load non-expired sessions from file into RAM on startup."""
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
        now = time.time()
        for sid, s in data.items():
            if now - s["created"] < SESSION_TTL:
                sessions[sid] = s
    except Exception:
        pass

def _save_sessions_to_disk():
    """Persist all in-memory sessions to file."""
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(sessions, f)
    except Exception:
        pass

def _flush_expired_from_disk():
    """Remove sessions older than SESSION_TTL (24h) from the file."""
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
        now = time.time()
        cleaned = {sid: s for sid, s in data.items() if now - s["created"] < SESSION_TTL}
        with open(SESSIONS_FILE, "w") as f:
            json.dump(cleaned, f)
    except Exception:
        pass

# ── background cleanup task ────────────────────────────────────────────────────

async def _cleanup_loop():
    """Every hour: evict sessions older than 1h from RAM; purge >24h from disk."""
    while True:
        await asyncio.sleep(RAM_TTL)
        now = time.time()
        expired_ram = [sid for sid, s in sessions.items() if now - s["created"] > RAM_TTL]
        for sid in expired_ram:
            sessions.pop(sid, None)
        _flush_expired_from_disk()

@app.on_event("startup")
async def startup():
    _load_sessions_from_disk()
    asyncio.create_task(_cleanup_loop())

# ── GameState ──────────────────────────────────────────────────────────────────

class GameState:
    def __init__(self, rows, cols, max_walls, mode, wall_limit=None):
        self.wall_limit = wall_limit
        self.walls_used = {"red": 0, "blue": 0}
        self.rows = rows
        self.cols = cols
        self.max_walls = max_walls
        self.mode = mode
        mid = cols // 2
        if mode == "same":
            self.players = {
                "red":  {"row": rows - 1, "col": max(0, mid - 1)},
                "blue": {"row": rows - 1, "col": min(cols - 1, mid + 1)},
            }
            self.goals = {"red": 0, "blue": 0}
        else:
            self.players = {
                "red":  {"row": rows - 1, "col": mid},
                "blue": {"row": 0,        "col": mid},
            }
            self.goals = {"red": 0, "blue": rows - 1}
        self.turn = "red"
        self.walls = []
        self.pending_walls = []
        self.pending_direction = None
        self.winner = None
        self.connections = {}
        self.chat = []

    def get_reachable_moves(self, color):
        pos = self.players[color]
        r, c = pos["row"], pos["col"]
        opponent = "blue" if color == "red" else "red"
        opp = self.players[opponent]
        blocked = self.existing_wall_keys()

        def wall_between(r1, c1, r2, c2):
            return self.wall_key(r1, c1, r2, c2) in blocked

        def in_bounds(rr, cc):
            return 0 <= rr < self.rows and 0 <= cc < self.cols

        moves = []
        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            nr, nc = r + dr, c + dc
            if not in_bounds(nr, nc) or wall_between(r, c, nr, nc):
                continue
            if nr == opp["row"] and nc == opp["col"]:
                jr, jc = nr + dr, nc + dc
                if in_bounds(jr, jc) and not wall_between(nr, nc, jr, jc):
                    moves.append([jr, jc])
                else:
                    perp = [(-1, 0), (1, 0)] if dr == 0 else [(0, -1), (0, 1)]
                    for pdr, pdc in perp:
                        dr2, dc2 = nr + pdr, nc + pdc
                        if in_bounds(dr2, dc2) and not wall_between(nr, nc, dr2, dc2):
                            moves.append([dr2, dc2])
            else:
                moves.append([nr, nc])
        return moves

    def wall_key(self, r1, c1, r2, c2):
        return tuple(sorted([(r1, c1), (r2, c2)]))

    def existing_wall_keys(self):
        return {self.wall_key(w["r1"],w["c1"],w["r2"],w["c2"]) for w in self.walls}

    def path_exists(self, color, extra_walls):
        pos = self.players[color]
        goal_row = self.goals[color]
        blocked = self.existing_wall_keys() | {self.wall_key(w[0],w[1],w[2],w[3]) for w in extra_walls}
        visited = set()
        q = deque([(pos["row"], pos["col"])])
        while q:
            r, c = q.popleft()
            if r == goal_row:
                return True
            if (r, c) in visited:
                continue
            visited.add((r, c))
            for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
                nr, nc = r+dr, c+dc
                if 0 <= nr < self.rows and 0 <= nc < self.cols:
                    if self.wall_key(r,c,nr,nc) not in blocked:
                        q.append((nr, nc))
        return False

    def to_dict(self):
        return {
            "players": self.players,
            "turn": self.turn,
            "walls": self.walls,
            "pending_walls": self.pending_walls,
            "pending_direction": self.pending_direction,
            "winner": self.winner,
            "mode": self.mode,
            "goals": self.goals,
            "rows": self.rows,
            "cols": self.cols,
            "max_walls": self.max_walls,
            "player_colors": PLAYER_COLORS,
            "wall_colors": WALL_COLORS,
            "wall_limit": self.wall_limit,
            "walls_used": self.walls_used,
            "chat": self.chat[-50:],
        }

# ── routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    rows: int = Form(9),
    cols: int = Form(9),
    max_walls: int = Form(1),
    game_mode: str = Form("opposite"),
    wall_limit_enabled: str = Form(None),
    total_walls: int = Form(10),
):
    rows = max(5, min(15, rows))
    cols = max(5, min(15, cols))
    max_walls = max(1, min(5, max_walls))
    wall_limit = max(1, min(50, total_walls)) if wall_limit_enabled else None

    valid = False
    try:
        with open("users.txt") as f:
            for line in f:
                u, p = line.strip().split(":", 1)
                if u == username and p == password:
                    valid = True
                    break
    except FileNotFoundError:
        valid = True

    if not valid:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})

    game_id = str(uuid.uuid4())
    games[game_id] = GameState(rows, cols, max_walls, game_mode, wall_limit)

    sid_red  = str(uuid.uuid4())
    sid_blue = str(uuid.uuid4())
    now = time.time()
    sessions[sid_red]  = {"game_id": game_id, "color": "red",  "username": username,        "created": now}
    sessions[sid_blue] = {"game_id": game_id, "color": "blue", "username": username + "_2", "created": now}

    _save_sessions_to_disk()   # persist new sessions immediately

    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": None,
        "player1_link": f"/game/{sid_red}",
        "player2_link": f"/game/{sid_blue}",
    })

@app.get("/game/{session_id}", response_class=HTMLResponse)
async def game_page(request: Request, session_id: str):
    s = sessions.get(session_id)
    if not s or time.time() - s["created"] > SESSION_TTL:
        return RedirectResponse("/")
    return templates.TemplateResponse("game.html", {"request": request, "session_id": session_id})

# ── websocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    s = sessions.get(session_id)
    if not s:
        await websocket.close(); return
    await websocket.accept()
    game = games.get(s["game_id"])
    if not game:
        await websocket.close(); return
    color = s["color"]
    game.connections[color] = websocket

    async def broadcast(data):
        for ws in list(game.connections.values()):
            try: await ws.send_json(data)
            except: pass

    await websocket.send_json({"type": "init", "state": game.to_dict(), "my_color": color})

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "chat":
                text = str(data.get("text", "")).strip()[:200]
                if text:
                    msg = {"sender": s["username"], "color": color, "text": text}
                    game.chat.append(msg)
                    await broadcast({"type": "chat", "msg": msg})
                continue

            if game.winner or color != game.turn:
                continue

            if action == "move":
                r, c = data["row"], data["col"]
                pr, pc = game.players[color]["row"], game.players[color]["col"]
                if 0 <= r < game.rows and 0 <= c < game.cols:
                    valid_moves = game.get_reachable_moves(color)
                    if [r, c] in valid_moves:
                        game.players[color] = {"row": r, "col": c}
                        if r == game.goals[color]:
                            game.winner = color
                        else:
                            game.turn = "blue" if color == "red" else "red"
                        await broadcast({"type": "state", "state": game.to_dict()})

            elif action == "wall":
                r1,c1,r2,c2 = data["r1"],data["c1"],data["r2"],data["c2"]
                if abs(r1-r2)+abs(c1-c2) != 1: continue
                direction = "h" if r1 != r2 else "v"
                if game.pending_direction and direction != game.pending_direction: continue
                wk = game.wall_key(r1,c1,r2,c2)
                if wk in game.existing_wall_keys(): continue
                if any(game.wall_key(w[0],w[1],w[2],w[3]) == wk for w in game.pending_walls): continue
                if game.wall_limit is not None:
                    used = game.walls_used[color]
                    pending_count = len(game.pending_walls)
                    if used + pending_count + 1 > game.wall_limit:
                        continue
                new_pending = game.pending_walls + [[r1,c1,r2,c2]]
                if not game.path_exists("red", new_pending) or not game.path_exists("blue", new_pending):
                    await websocket.send_json({"type": "error", "msg": "Wall would block a player's path!"})
                    continue
                game.pending_walls = new_pending
                game.pending_direction = direction
                await broadcast({"type": "state", "state": game.to_dict()})

            elif action == "end_wall_turn":
                if 0 < len(game.pending_walls) <= game.max_walls:
                    for w in game.pending_walls:
                        game.walls.append({"r1":w[0],"c1":w[1],"r2":w[2],"c2":w[3],"owner":color})
                    game.walls_used[color] += len(game.pending_walls)
                    game.pending_walls = []
                    game.pending_direction = None
                    game.turn = "blue" if color == "red" else "red"
                    await broadcast({"type": "state", "state": game.to_dict()})

            elif action == "cancel_wall_turn":
                game.pending_walls = []
                game.pending_direction = None
                await broadcast({"type": "state", "state": game.to_dict()})

    except WebSocketDisconnect:
        game.connections.pop(color, None)
