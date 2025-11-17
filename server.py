#!/usr/bin/env python3
"""
FULL FEATURE SERVER.PY (with server-console colors + improved /createroom & /help)

Features:
- Registration & Login (password stored locally)
- Rooms (create, join, edit open_join/visible)
- Join requests (admin approves/denies)
- DMs
- Typing indicators
- Presence tracking (online/idle/offline)
- Message history per room
- Room shutdown
- /help, /rooms, /who, /history
- JSON persistence across restarts
- Full logging
- Server-side colored console output (ONLY console)
"""

import asyncio
import websockets
import json
import time
import logging
import os
from pathlib import Path
from typing import Any

# ---------------- CONFIG ----------------
HOST = "0.0.0.0"
PORT = 8765

DATA_DIR = Path(".")
USERS_FILE = DATA_DIR / "users.json"
ROOMS_FILE = DATA_DIR / "rooms.json"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = DATA_DIR / "server.log"

IDLE_TIMEOUT = 300  # seconds -> mark idle after this period (5 minutes)
HISTORY_LIMIT = 200  # per room

# ---------------- COLOURS (SERVER-CONSOLE ONLY) ----------------
CSI = "\033["
RESET = CSI + "0m"
COLORS = {
    "info": CSI + "1;34m",      # bright blue
    "success": CSI + "1;32m",   # bright green
    "warn": CSI + "1;33m",      # bright yellow
    "error": CSI + "1;31m",     # bright red
    "cmd": CSI + "1;36m",       # bright cyan
    "debug": CSI + "2;37m",     # dim gray
}

def cprint(kind: str, msg: str):
    """Print a colored message to the server console (does not affect log file)."""
    color = COLORS.get(kind, "")
    try:
        print(f"{color}{msg}{RESET}")
    except Exception:
        # fallback to plain print if console doesn't support colours
        print(msg)

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
console.setFormatter(formatter)
logging.getLogger().addHandler(console)

# ---------------- STATE ----------------
USERS: dict[str, dict[str, Any]] = {}  # username -> {password, ws, last_active, status, activity}
SOCKET_TO_USER: dict[websockets.WebSocketServerProtocol, str] = {}
ROOMS: dict[str, dict[str, Any]] = {}   # room -> {admin, open_join, visible, members:set, pending:set, shutdown}
HISTORY: dict[str, list[dict[str, Any]]] = {}  # room -> list of messages
TYPING: dict[str, set[str]] = {}  # room -> set of usernames typing

# ---------------- HELPERS ----------------
def now() -> int:
    return int(time.time())

def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to read {path}: {e}")
            return default
    return default

def save_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.error(f"Failed saving {path}: {e}")

def persist():
    """Save USERS (passwords), ROOMS (metadata) and HISTORY to disk."""
    try:
        # USERS: store only password
        users_dump = {u: {"password": USERS[u]["password"]} for u in USERS}
        save_json(USERS_FILE, users_dump)

        # ROOMS: convert sets to lists for JSON
        rooms_dump = {}
        for r, info in ROOMS.items():
            rooms_dump[r] = {
                "admin": info.get("admin"),
                "open_join": bool(info.get("open_join", True)),
                "visible": bool(info.get("visible", True)),
                "members": list(info.get("members", [])),
                "pending": list(info.get("pending", [])),
                "shutdown": bool(info.get("shutdown", False)),
            }
        save_json(ROOMS_FILE, rooms_dump)

        # HISTORY
        save_json(HISTORY_FILE, HISTORY)
    except Exception:
        logging.exception("persist() failed")

def restore():
    """Load USERS, ROOMS, HISTORY from disk into memory. Convert lists to sets where needed."""
    # USERS
    data_users = load_json(USERS_FILE, {})
    for u, info in data_users.items():
        USERS[u] = {
            "password": info.get("password", ""),
            "ws": None,
            "last_active": 0,
            "status": "offline",
            "activity": ""
        }

    # ROOMS
    data_rooms = load_json(ROOMS_FILE, {})
    for r, info in data_rooms.items():
        ROOMS[r] = {
            "admin": info.get("admin"),
            "open_join": bool(info.get("open_join", True)),
            "visible": bool(info.get("visible", True)),
            # convert members/pending back to sets
            "members": set(info.get("members", [])),
            "pending": set(info.get("pending", [])),
            "shutdown": bool(info.get("shutdown", False)),
        }

    # HISTORY
    hist = load_json(HISTORY_FILE, {})
    for r, msgs in hist.items():
        HISTORY[r] = msgs[:HISTORY_LIMIT]

def safe_send(ws: websockets.WebSocketServerProtocol | None, obj: dict):
    """Send JSON to ws if open. Returns coroutine; caller should await it."""
    async def _send():
        if not ws:
            return
        try:
            if ws.open:
                await ws.send(json.dumps(obj))
        except Exception:
            # ignore, caller should handle disconnections
            pass
    return _send()

async def broadcast(room: str, obj: dict):
    """Broadcast a JSON object to all members of a room."""
    if room not in ROOMS:
        return
    # copy to avoid modification during iteration
    members = list(ROOMS[room].get("members", []))
    for username in members:
        ws = USERS.get(username, {}).get("ws")
        if ws:
            await safe_send(ws, obj)

def ensure_room(room: str):
    """Create room with defaults if it doesn't exist."""
    if room not in ROOMS:
        ROOMS[room] = {
            "admin": None,
            "open_join": True,
            "visible": True,
            "members": set(),
            "pending": set(),
            "shutdown": False
        }
        HISTORY.setdefault(room, [])

def add_history(room: str, msg: dict):
    HISTORY.setdefault(room, [])
    HISTORY[room].append(msg)
    if len(HISTORY[room]) > HISTORY_LIMIT:
        HISTORY[room].pop(0)

def parse_bool_token(token: str) -> bool | None:
    """Parse common true/false tokens. Return None if invalid."""
    if token is None:
        return None
    t = str(token).strip().lower()
    if t in ("true", "1", "yes", "y", "on", "open"):
        return True
    if t in ("false", "0", "no", "n", "off", "closed"):
        return False
    return None

# ---------------- BACKGROUND TASKS ----------------
async def idle_checker():
    """Periodically update user statuses to online/idle/offline and broadcast changes."""
    cprint("debug", "idle_checker started")
    while True:
        try:
            ts = now()
            for username, info in list(USERS.items()):
                ws = info.get("ws")
                prev_status = info.get("status", "offline")
                if ws and getattr(ws, "open", False):
                    last = info.get("last_active", 0)
                    if ts - last > IDLE_TIMEOUT:
                        if prev_status != "idle":
                            info["status"] = "idle"
                            logging.info(f"{username} set to idle")
                            # broadcast presence update to rooms where user is a member
                            for rname, rinfo in ROOMS.items():
                                if username in rinfo.get("members", set()):
                                    asyncio.create_task(broadcast(rname, {"type": "presence_update", "user": username, "status": "idle"}))
                            cprint("info", f"[presence] {username} → idle")
                    else:
                        if prev_status != "online":
                            info["status"] = "online"
                            logging.info(f"{username} set to online")
                            for rname, rinfo in ROOMS.items():
                                if username in rinfo.get("members", set()):
                                    asyncio.create_task(broadcast(rname, {"type": "presence_update", "user": username, "status": "online"}))
                            cprint("info", f"[presence] {username} → online")
                else:
                    if prev_status != "offline":
                        info["status"] = "offline"
                        logging.info(f"{username} offline")
                        cprint("warn", f"[presence] {username} → offline")
        except Exception:
            logging.exception("idle_checker error")
        await asyncio.sleep(5)

# ---------------- HELPER: HELP TEXT ----------------
def get_help_text() -> str:
    """Return a well-formatted help string for /help (server sends this to the client)."""
    lines = [
        "Available commands (usage):",
        "",
        "Authentication:",
        "  /login <username> <password>",
        "  /register <username> <password> <password>",
        "",
        "Rooms & membership:",
        "  /rooms",
        "  /createroom <room> <open_join:true|false> <visible:true|false>",
        "    - Example: /createroom coding true true",
        "  /editroom <room> <open_join:true|false> <visible:true|false>",
        "    - Example: /editroom coding false true",
        "  /join <room>",
        "  /approve <room> <user>    # admin approves pending user",
        "  /deny <room> <user>       # admin denies pending user",
        "  /shutdown <room>          # admin only",
        "",
        "Messaging:",
        "  /dm <user> <message>      # direct message",
        "  /history [room]           # get last messages for room (default current)",
        "",
        "Presence & info:",
        "  /who [room]               # list users and statuses in room (default current)",
        "  /help                     # show this help text",
        "",
        "Misc:",
        "  /quit                     # disconnect",
        ""
    ]
    return "\n".join(lines)

# ---------------- MAIN HANDLER ----------------
async def handler(ws: websockets.WebSocketServerProtocol, path: str):
    logging.info("New connection")
    cprint("debug", f"[conn] new websocket connection")
    username: str | None = None
    authed = False
    current_room = "general"
    ensure_room("general")

    # send initial info to client
    await safe_send(ws, {"type": "info", "msg": "Connected. Please /login or /register."})

    try:
        async for raw in ws:
            # Expect JSON messages from clients. Ignore non-json safely.
            try:
                data = json.loads(raw)
            except Exception:
                logging.debug("Received non-json message; ignoring")
                continue

            typ = data.get("type")
            # ---------- AUTH ----------
            if typ == "auth":
                action = data.get("action")
                u = data.get("username")
                p = data.get("password")
                logging.info(f"AUTH action={action} user={u}")

                if not u or not p:
                    await safe_send(ws, {"type": "error", "msg": "username/password required"})
                    continue

                # REGISTER
                if action == "register":
                    if u in USERS:
                        await safe_send(ws, {"type": "error", "msg": "username exists"})
                        continue
                    # register new user
                    USERS[u] = {
                        "password": p,
                        "ws": ws,
                        "last_active": now(),
                        "status": "online",
                        "activity": ""
                    }
                    SOCKET_TO_USER[ws] = u
                    username = u
                    authed = True
                    ensure_room("general")
                    ROOMS["general"]["members"].add(u)
                    logging.info(f"Registered & logged in {u}")
                    cprint("success", f"[auth] registered: {u}")
                    await safe_send(ws, {"type": "auth_ok", "msg": f"Logged in as {u}"})
                    # notify general room
                    await broadcast("general", {"type": "room_join", "room": "general", "username": u})
                    persist()
                    continue

                # LOGIN
                if action == "login":
                    if u not in USERS or USERS[u].get("password", "") != p:
                        await safe_send(ws, {"type": "auth_fail", "msg": "invalid credentials"})
                        cprint("warn", f"[auth fail] {u}")
                        continue
                    # attach socket & mark online
                    USERS[u]["ws"] = ws
                    USERS[u]["last_active"] = now()
                    USERS[u]["status"] = "online"
                    SOCKET_TO_USER[ws] = u
                    username = u
                    authed = True
                    ensure_room("general")
                    ROOMS["general"]["members"].add(u)
                    logging.info(f"User logged in: {u}")
                    cprint("success", f"[auth] logged in: {u}")
                    await safe_send(ws, {"type": "auth_ok", "msg": f"Logged in as {u}"})
                    await broadcast("general", {"type": "room_join", "room": "general", "username": u})
                    continue

            # require auth for everything else
            if not authed:
                await safe_send(ws, {"type": "error", "msg": "Please authenticate first (/login or /register)"})
                continue

            # update last_active timestamp and activity
            if username:
                USERS[username]["last_active"] = now()
                USERS[username]["activity"] = data.get("activity", "")

            # ---------- MESSAGE ----------
            if typ == "message":
                room = data.get("room", "general") or "general"
                text = data.get("text", "") or ""
                # if user typed /help as a chat message, return the full help text
                if text.strip() == "/help":
                    help_msg = get_help_text()
                    await safe_send(ws, {"type": "info", "msg": help_msg})
                    continue

                # normal message flow
                ensure_room(room)
                msg = {"type": "message", "room": room, "username": username, "text": text, "ts": now()}
                add_history(room, msg)
                await broadcast(room, msg)
                logging.info(f"MSG room={room} user={username} text={text[:80]}")
                continue

            # ---------- DM ----------
            if typ == "dm":
                to = data.get("to")
                text = data.get("text", "")
                if not to or to not in USERS:
                    await safe_send(ws, {"type": "error", "msg": "user not found"})
                    continue
                target_ws = USERS[to].get("ws")
                if not target_ws:
                    await safe_send(ws, {"type": "error", "msg": "user offline"})
                    continue
                await safe_send(target_ws, {"type": "dm", "from": username, "text": text})
                await safe_send(ws, {"type": "dm_sent", "to": to, "text": text})
                logging.info(f"DM from {username} to {to}")
                cprint("cmd", f"[dm] {username} → {to}: {text[:60]}")
                continue

            # ---------- CREATEROOM ----------
            if typ == "createroom":
                room = data.get("room")
                open_join_token = data.get("open_join", True)
                visible_token = data.get("visible", True)

                if not room or not isinstance(room, str) or not room.strip():
                    await safe_send(ws, {"type": "error", "msg": "room name required"})
                    continue
                room = room.strip()

                if room in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room already exists"})
                    continue

                # parse booleans flexibly
                if isinstance(open_join_token, bool):
                    open_join = open_join_token
                else:
                    open_join_parsed = parse_bool_token(open_join_token)
                    open_join = True if open_join_parsed is None else open_join_parsed

                if isinstance(visible_token, bool):
                    visible = visible_token
                else:
                    visible_parsed = parse_bool_token(visible_token)
                    visible = True if visible_parsed is None else visible_parsed

                # create the room and preserve other structures
                ROOMS[room] = {
                    "admin": username,
                    "open_join": open_join,
                    "visible": visible,
                    "members": {username},
                    "pending": set(),
                    "shutdown": False
                }
                HISTORY.setdefault(room, [])
                logging.info(f"Room created: {room} admin={username} open={open_join} visible={visible}")
                cprint("success", f"[room created] {room} (admin={username}) open={open_join} visible={visible}")
                await safe_send(ws, {"type": "room_created", "room": room})
                persist()
                continue

            # ---------- EDITROOM ----------
            if typ == "editroom":
                room = data.get("room")
                if not room or room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                if ROOMS[room].get("admin") != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can edit"})
                    continue

                open_join_token = data.get("open_join", ROOMS[room]["open_join"])
                visible_token = data.get("visible", ROOMS[room]["visible"])

                if isinstance(open_join_token, bool):
                    open_join = open_join_token
                else:
                    parsed = parse_bool_token(open_join_token)
                    if parsed is None:
                        open_join = ROOMS[room]["open_join"]
                    else:
                        open_join = parsed

                if isinstance(visible_token, bool):
                    visible = visible_token
                else:
                    parsed = parse_bool_token(visible_token)
                    if parsed is None:
                        visible = ROOMS[room]["visible"]
                    else:
                        visible = parsed

                # update properties but preserve members/pending
                ROOMS[room]["open_join"] = open_join
                ROOMS[room]["visible"] = visible

                logging.info(f"Room edited: {room} by {username} open={open_join} visible={visible}")
                cprint("info", f"[room edit] {room} open={open_join} visible={visible}")
                await safe_send(ws, {"type": "room_updated", "room": room})
                persist()
                continue

            # ---------- JOIN ----------
            if typ == "join":
                room = data.get("room")
                if not room or room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                rinfo = ROOMS[room]
                if rinfo.get("shutdown", False):
                    await safe_send(ws, {"type": "error", "msg": "room is shutdown"})
                    continue

                if rinfo.get("open_join", True):
                    rinfo["members"].add(username)
                    await safe_send(ws, {"type": "joined", "room": room})
                    await broadcast(room, {"type": "room_join", "room": room, "username": username})
                    logging.info(f"{username} joined {room}")
                    cprint("cmd", f"[join] {username} → {room}")
                else:
                    # add to pending and notify admin
                    rinfo["pending"].add(username)
                    admin = rinfo.get("admin")
                    admin_ws = USERS.get(admin, {}).get("ws")
                    if admin_ws:
                        await safe_send(admin_ws, {"type": "join_request", "room": room, "user": username})
                    await safe_send(ws, {"type": "request_ack", "room": room})
                    logging.info(f"{username} requested to join {room} (pending admin approval)")
                    cprint("warn", f"[join request] {username} → {room} (admin={admin})")
                persist()
                continue

            # ---------- APPROVE ----------
            if typ == "approve":
                room = data.get("room")
                user = data.get("user")
                if not room or room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                if ROOMS[room].get("admin") != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can approve"})
                    continue
                if user not in ROOMS[room].get("pending", set()):
                    await safe_send(ws, {"type": "error", "msg": "user not pending"})
                    continue
                ROOMS[room]["pending"].discard(user)
                ROOMS[room]["members"].add(user)
                user_ws = USERS.get(user, {}).get("ws")
                if user_ws:
                    await safe_send(user_ws, {"type": "joined", "room": room})
                logging.info(f"{username} approved {user} for {room}")
                cprint("success", f"[approve] {username} approved {user} for {room}")
                persist()
                continue

            # ---------- DENY ----------
            if typ == "deny":
                room = data.get("room")
                user = data.get("user")
                if not room or room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                if ROOMS[room].get("admin") != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can deny"})
                    continue
                ROOMS[room]["pending"].discard(user)
                logging.info(f"{username} denied {user} for {room}")
                cprint("info", f"[deny] {username} denied {user} for {room}")
                persist()
                continue

            # ---------- ROOMS (list) ----------
            if typ == "rooms":
                out = []
                for r, info in ROOMS.items():
                    if info.get("visible", True):
                        out.append({"room": r, "admin": info.get("admin"), "open_join": info.get("open_join"), "visible": info.get("visible")})
                await safe_send(ws, {"type": "rooms_list", "rooms": out})
                continue

            # ---------- WHO ----------
            if typ == "who":
                room = data.get("room", current_room) or current_room
                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                users_info = []
                for u in ROOMS[room].get("members", set()):
                    info = USERS.get(u, {})
                    users_info.append({"username": u, "status": info.get("status", "offline"), "activity": info.get("activity", "")})
                await safe_send(ws, {"type": "presence", "room": room, "users": users_info})
                continue

            # ---------- TYPING ----------
            if typ == "typing":
                room = data.get("room", current_room) or current_room
                state = data.get("state", True)
                TYPING.setdefault(room, set())
                if state:
                    TYPING[room].add(username)
                else:
                    TYPING[room].discard(username)
                await broadcast(room, {"type": "typing", "room": room, "users": list(TYPING.get(room, set()))})
                continue

            # ---------- HISTORY ----------
            if typ == "history":
                room = data.get("room", current_room) or current_room
                msgs = HISTORY.get(room, [])
                await safe_send(ws, {"type": "history", "room": room, "messages": msgs})
                continue

            # ---------- SHUTDOWN ROOM ----------
            if typ == "shutdown":
                room = data.get("room")
                if not room or room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                if ROOMS[room].get("admin") != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can shutdown"})
                    continue
                ROOMS[room]["shutdown"] = True
                await broadcast(room, {"type": "info", "msg": f"Room {room} is shutdown by admin"})
                logging.info(f"Room {room} shutdown by {username}")
                cprint("warn", f"[shutdown] {room} by {username}")
                persist()
                continue

            # ---------- UNKNOWN ----------
            await safe_send(ws, {"type": "error", "msg": f"unknown command {typ}"})

    except websockets.ConnectionClosed:
        logging.info("Connection closed")
    except Exception:
        logging.exception("Unhandled exception in handler")
    finally:
        # cleanup on disconnect
        try:
            if username:
                USERS[username]["ws"] = None
                USERS[username]["status"] = "offline"
                cprint("warn", f"[disconnect] {username} disconnected")
                for rname, rinfo in ROOMS.items():
                    if username in rinfo.get("members", set()):
                        asyncio.create_task(broadcast(rname, {"type": "info", "msg": f"{username} disconnected"}))
        except Exception:
            logging.exception("cleanup error")
        persist()

# ---------------- MAIN ----------------
async def main():
    restore()
    # Ensure general room exists and persist if newly created
    ensure_room("general")
    persist()
    logging.info(f"Starting server on {HOST}:{PORT}")
    cprint("info", f"[SERVER] Chat server running at ws://{HOST}:{PORT}")
    # start background idle checker
    asyncio.create_task(idle_checker())
    # start websocket server
    async with websockets.serve(handler, HOST, PORT, ping_interval=None, ping_timeout=None):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        persist()
        logging.info("Server stopped")
        cprint("info", "Server stopped by KeyboardInterrupt")
    except Exception:
        logging.exception("Server crashed")
        cprint("error", "Server crashed; check server.log for details")
        persist()
