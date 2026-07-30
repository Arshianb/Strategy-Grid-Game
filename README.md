# Grid Strategy Game

## Setup
```bash
pip install fastapi uvicorn jinja2 python-multipart

## Run

bash
uvicorn main:app --host 0.0.0.0 --port 8000

Access at `http://<your-local-ip>:8000`

## users.txt

One user per line in `username:password` format:

alice:mypassword
bob:otherpass

## How to Play

1. Player 1 logs in → gets a unique game URL → shares it with Player 2
2. Player 2 opens the same URL
3. First connection = Red, second = Blue
4. Click your piece to see move hints (movement mode) or wall hints (wall mode)
5. First to reach row 0 wins

## Deploy

Use any ASGI host (Railway, Render, Fly.io). Set `PORT` env var and run:
bash
uvicorn main:app --host 0.0.0.0 --port $PORT


---

Key improvements over the original:
- Each login creates a **unique game room** (not shared `room1`)
- **Color assignment** by connection order (first=red, second=blue, rest=spectator)
- **Win detection** when a player reaches row 0
- **Configurable grid size** (5–15) from login form
- Canvas-based rendering with proper wall drawing on cell edges
- Wall hints show all available edges; clicking near an edge places it
- BFS pathfinding check preserved and uses `frozenset` for reliable wall lookup
- Mobile-friendly canvas with touch support

The previous response was already complete. All files (`main.py`, `templates/login.html`, `templates/game.html`, `users.txt`, `README.md`) were fully delivered.

