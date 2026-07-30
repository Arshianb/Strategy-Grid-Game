# main.py
import time
import uuid
from collections import deque
from fastapi import FastAPI, Form, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

sessions = {}
games = {}
EXPIRATION = 48 * 3600


class GameState:
    def __init__(self, rows: int = 9, cols: int = 9, max_walls: int = 2):
        self.rows = rows
        self.cols = cols
        self.max_walls = max_walls  # 1, 2, or 3
        mid = cols // 2
        self.players = {
            "red":  (rows - 1, max(0, mid - 2)),
            "blue": (rows - 1, min(cols - 1, mid + 2)),
        }
        self.turn = "red"
        self.walls = set()
        self.pending_walls: list = []
        self.pending_direction = None  # "H" or "V"
        self.connections: list = []
        self.color_to_session: dict = {}
        self.winner = None

    def to_dict(self):
        return {
            "rows": self.rows,
            "cols": self.cols,
            "max_walls": self.max_walls,
            "players": {k: list(v) for k, v in self.players.items()},
            "turn": self.turn,
            "walls": [list(map(list, w)) for w in self.walls],
            "pending_walls": [list(map(list, w)) for w in self.pending_walls],
            "pending_direction": self.pending_direction,
            "winner": self.winner,
        }


def wall_direction(a, b):
    return "H" if a[0] == b[0] else "V"


def bfs_can_reach_row0(pos, walls, rows, cols):
    queue = deque([pos])
    visited = {pos}
    while queue:
        r, c = queue.popleft()
        if r == 0:
            return True
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                edge = frozenset([(r, c), (nr, nc)])
                if edge not in walls and (nr, nc) not in visited:
                    visited.add((nr, nc))
                    queue.append((nr, nc))
    return False


def verify_user(username, password):
    try:
        with open("users.txt") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    u, p = line.split(":", 1)
                    if u == username and p == password:
                        return True
    except FileNotFoundError:
        pass
    return False


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                 grid_rows: int = Form(9), grid_cols: int = Form(9), max_walls: int = Form(2)):
    if not verify_user(username, password):
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "Invalid username or password"})

    grid_rows = max(5, min(15, grid_rows))
    grid_cols = max(5, min(15, grid_cols))
    max_walls = max_walls if max_walls in (1, 2, 3) else 2

    game_id = str(uuid.uuid4())
    games[game_id] = GameState(grid_rows, grid_cols, max_walls)

    sid_red = str(uuid.uuid4())
    sid_blue = str(uuid.uuid4())
    now = time.time()
    sessions[sid_red]  = {"created_at": now, "game_id": game_id, "username": username, "color": "red"}
    sessions[sid_blue] = {"created_at": now, "game_id": game_id, "username": username, "color": "blue"}

    return templates.TemplateResponse("game.html", {
        "request": request,
        "session_id": sid_red,
        "player2_link": f"/game/{sid_blue}",
        "player1_link": f"/game/{sid_red}",
        "is_creator": True,
    })


@app.get("/game/{session_id}", response_class=HTMLResponse)
async def game_page(request: Request, session_id: str):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    if time.time() - s["created_at"] > EXPIRATION:
        sessions.pop(session_id, None)
        raise HTTPException(403, "Link expired")
    return templates.TemplateResponse("game.html", {
        "request": request,
        "session_id": session_id,
        "player2_link": None,
        "player1_link": None,
        "is_creator": False,
    })


@app.websocket("/ws/{session_id}")
async def ws_endpoint(websocket: WebSocket, session_id: str):
    s = sessions.get(session_id)
    if not s:
        await websocket.close(code=4001)
        return
    game = games.get(s["game_id"])
    if not game:
        await websocket.close(code=4002)
        return

    my_color = s["color"]
    await websocket.accept()
    game.connections.append(websocket)
    game.color_to_session[my_color] = session_id

    await websocket.send_json({"type": "init", "state": game.to_dict(), "my_color": my_color})

    async def broadcast():
        dead = []
        for conn in game.connections:
            try:
                await conn.send_json({"type": "update", "state": game.to_dict()})
            except Exception:
                dead.append(conn)
        for d in dead:
            game.connections.remove(d)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if game.winner:
                continue

            if action == "move":
                player = data["player"]
                nr, nc = data["pos"]
                if game.turn != player or player != my_color:
                    continue
                if game.pending_walls:
                    await websocket.send_json({"type": "error", "msg": "Commit or cancel pending walls first"})
                    continue
                cr, cc = game.players[player]
                if abs(nr - cr) + abs(nc - cc) != 1:
                    continue
                if not (0 <= nr < game.rows and 0 <= nc < game.cols):
                    continue
                edge = frozenset([(cr, cc), (nr, nc)])
                if edge in game.walls:
                    await websocket.send_json({"type": "error", "msg": "A wall blocks that path"})
                    continue
                game.players[player] = (nr, nc)
                if nr == 0:
                    game.winner = player
                else:
                    game.turn = "blue" if player == "red" else "red"
                await broadcast()

            elif action == "wall":
                player = data["player"]
                if game.turn != player or player != my_color:
                    continue
                a, b = tuple(data["w1"]), tuple(data["w2"])
                if a == b or abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1:
                    continue

                direction = wall_direction(a, b)
                if game.pending_direction and direction != game.pending_direction:
                    await websocket.send_json({"type": "error", "msg": "All walls in a turn must face the same direction"})
                    continue

                edge = frozenset([a, b])
                all_walls = game.walls | set(game.pending_walls)
                if edge in all_walls:
                    await websocket.send_json({"type": "error", "msg": "Wall already placed there"})
                    continue

                temp = all_walls | {edge}
                if not bfs_can_reach_row0(game.players["red"], temp, game.rows, game.cols) or \
                   not bfs_can_reach_row0(game.players["blue"], temp, game.rows, game.cols):
                    await websocket.send_json({"type": "error", "msg": "That wall would block a player's path"})
                    continue

                game.pending_walls.append(edge)
                game.pending_direction = direction

                if len(game.pending_walls) >= game.max_walls:
                    for w in game.pending_walls:
                        game.walls.add(w)
                    game.pending_walls = []
                    game.pending_direction = None
                    game.turn = "blue" if player == "red" else "red"

                await broadcast()

            elif action == "end_wall_turn":
                player = data["player"]
                if game.turn != player or player != my_color:
                    continue
                if not game.pending_walls:
                    continue
                for w in game.pending_walls:
                    game.walls.add(w)
                game.pending_walls = []
                game.pending_direction = None
                game.turn = "blue" if player == "red" else "red"
                await broadcast()

            elif action == "cancel_wall_turn":
                player = data["player"]
                if game.turn != player or player != my_color:
                    continue
                game.pending_walls = []
                game.pending_direction = None
                await broadcast()

    except WebSocketDisconnect:
        if websocket in game.connections:
            game.connections.remove(websocket)
