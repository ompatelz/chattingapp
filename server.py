#!/usr/bin/env python3
"""
FULL FEATURE SERVER.PY
----------------------

FEATURES:
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
- Cross-platform

WORKS WITH YOUR FINAL client.py
"""

import asyncio
import websockets
import json
import time
import logging
import os
from pathlib import Path

# ---------------- CONFIG ----------------
HOST = "0.0.0.0"
PORT = 8765

DATA_DIR = Path(".")
USERS_FILE   = DATA_DIR / "users.json"
ROOMS_FILE   = DATA_DIR / "rooms.json"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE     = DATA_DIR / "server.log"

IDLE_TIMEOUT = 300           # 5 min
HISTORY_LIMIT = 200

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
USERS = {}              # username -> {password, ws, last_active, status, activity}
SOCKET_TO_USER = {}     # websocket -> username
ROOMS = {}              # room -> {admin, open_join, visible, members:set, pending:set, shutdown}
HISTORY = {}            # room -> list of messages
TYPING = {}             # room -> set of usernames typing


# ---------------- HELPERS ----------------

def now():
    return int(time.time())

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except:
        pass

def persist():
    # USERS
    users_dump = {
        u: {"password": USERS[u]["password"]}
        for u in USERS
    }
    save_json(USERS_FILE, users_dump)

    # ROOMS
    rooms_dump = {}
    for r, info in ROOMS.items():
        rooms_dump[r] = {
            "admin": info["admin"],
            "open_join": info["open_join"],
            "visible": info["visible"],
            "members": list(info["members"]),
            "pending": list(info["pending"]),
            "shutdown": info["shutdown"]
        }
    save_json(ROOMS_FILE, rooms_dump)

    # HISTORY
    save_json(HISTORY_FILE, HISTORY)


def restore():
    # USERS
    data_users = load_json(USERS_FILE, {})
    for u, info in data_users.items():
        USERS[u] = {
            "password": info["password"],
            "ws": None,
            "last_active": 0,
            "status": "offline",
            "activity": ""
        }

    # ROOMS
    data_rooms = load_json(ROOMS_FILE, {})
    for r, info in data_rooms.items():
        ROOMS[r] = {
            "admin": info["admin"],
            "open_join": info.get("open_join", True),
            "visible": info.get("visible", True),
            "members": set(info.get("members", [])),
            "pending": set(info.get("pending", [])),
            "shutdown": info.get("shutdown", False)
        }

    # HISTORY
    hist = load_json(HISTORY_FILE, {})
    for r, msgs in hist.items():
        HISTORY[r] = msgs[:HISTORY_LIMIT]


async def safe_send(ws, obj):
    try:
        if ws and ws.open:
            await ws.send(json.dumps(obj))
    except:
        pass

async def broadcast(room, obj):
    if room not in ROOMS:
        return
    for u in ROOMS[room]["members"]:
        ws = USERS.get(u, {}).get("ws")
        if ws:
            await safe_send(ws, obj)

def ensure_room(room):
    if room not in ROOMS:
        ROOMS[room] = {
            "admin": None,
            "open_join": True,
            "visible": True,
            "members": set(),
            "pending": set(),
            "shutdown": False
        }
        HISTORY[room] = []


def add_history(room, msg):
    HISTORY.setdefault(room, [])
    HISTORY[room].append(msg)
    if len(HISTORY[room]) > HISTORY_LIMIT:
        HISTORY[room].pop(0)


# ---------------- BACKGROUND TASK ----------------
async def idle_checker():
    while True:
        try:
            t = now()
            for u, data in USERS.items():
                ws = data["ws"]
                prev = data["status"]

                if ws and ws.open:
                    if t - data["last_active"] > IDLE_TIMEOUT:
                        if prev != "idle":
                            data["status"] = "idle"
                            for r in ROOMS:
                                if u in ROOMS[r]["members"]:
                                    asyncio.create_task(
                                        broadcast(r, {"type": "presence_update", "user": u, "status": "idle"})
                                    )
                    else:
                        if prev != "online":
                            data["status"] = "online"
                            for r in ROOMS:
                                if u in ROOMS[r]["members"]:
                                    asyncio.create_task(
                                        broadcast(r, {"type": "presence_update", "user": u, "status": "online"})
                                    )
                else:
                    if prev != "offline":
                        data["status"] = "offline"
        except:
            pass
        await asyncio.sleep(5)


# ---------------- MAIN HANDLER ----------------
async def handler(ws, path):
    logging.info("New connection")
    username = None
    authed = False
    current_room = "general"
    ensure_room("general")

    await safe_send(ws, {"type": "info", "msg": "Connected. Please /login or /register."})

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except:
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
                    ROOMS["general"]["members"].add(u)

                    await safe_send(ws, {"type": "auth_ok", "msg": f"Logged in as {u}"})
                    await broadcast("general", {"type": "room_join", "room": "general", "username": u})

                    persist()
                    continue

                # LOGIN
                if action == "login":
                    if u not in USERS or USERS[u]["password"] != p:
                        await safe_send(ws, {"type": "auth_fail", "msg": "invalid credentials"})
                        continue

                    USERS[u]["ws"] = ws
                    USERS[u]["last_active"] = now()
                    USERS[u]["status"] = "online"
                    SOCKET_TO_USER[ws] = u
                    username = u
                    authed = True
                    ROOMS["general"]["members"].add(u)

                    await safe_send(ws, {"type": "auth_ok", "msg": f"Logged in as {u}"})
                    await broadcast("general", {"type": "room_join", "room": "general", "username": u})
                    continue

            # MUST AUTH FIRST
            if not authed:
                await safe_send(ws, {"type": "error", "msg": "Please authenticate first (/login or /register)"})
                continue

            # Timestamp activity
            USERS[username]["last_active"] = now()

            # ---------- MESSAGE ----------
            if typ == "message":
                room = data.get("room", "general")
                text = data.get("text", "")

                if text == "/help":
                    help_msg = (
                        "/help\n"
                        "/login <user> <pwd>\n"
                        "/register <user> <pwd> <pwd>\n"
                        "/rooms\n"
                        "/who\n"
                        "/join <room>\n"
                        "/createroom <room> <open_join:true|false> <visible:true|false>\n"
                        "/editroom <room> <open_join:true|false> <visible:true|false>\n"
                        "/dm <user> <msg>\n"
                        "/approve <room> <user>\n"
                        "/deny <room> <user>\n"
                    )
                    await safe_send(ws, {"type": "info", "msg": help_msg})
                    continue

                ensure_room(room)
                msg = {
                    "type": "message",
                    "room": room,
                    "username": username,
                    "text": text,
                    "ts": now()
                }
                add_history(room, msg)
                await broadcast(room, msg)
                continue

            # ---------- DM ----------
            if typ == "dm":
                to = data.get("to")
                text = data.get("text", "")
                if to not in USERS or not USERS[to]["ws"]:
                    await safe_send(ws, {"type": "error", "msg": "user not found or offline"})
                    continue

                await safe_send(USERS[to]["ws"], {"type": "dm", "from": username, "text": text})
                await safe_send(ws, {"type": "dm_sent", "to": to, "text": text})
                continue

            # ---------- CREATE ROOM ----------
            if typ == "createroom":
                room = data.get("room")
                open_join = data.get("open_join", True)
                visible = data.get("visible", True)

                if room in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room exists"})
                    continue

                ROOMS[room] = {
                    "admin": username,
                    "open_join": open_join,
                    "visible": visible,
                    "members": {username},
                    "pending": set(),
                    "shutdown": False
                }
                HISTORY[room] = []
                await safe_send(ws, {"type": "room_created", "room": room})
                persist()
                continue

            # ---------- EDIT ROOM ----------
            if typ == "editroom":
                room = data.get("room")
                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                if ROOMS[room]["admin"] != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can edit"})
                    continue

                ROOMS[room]["open_join"] = data.get("open_join", ROOMS[room]["open_join"])
                ROOMS[room]["visible"]   = data.get("visible", ROOMS[room]["visible"])

                await safe_send(ws, {"type": "room_updated", "room": room})
                persist()
                continue

            # ---------- JOIN ROOM ----------
            if typ == "join":
                room = data.get("room")
                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                info = ROOMS[room]
                if info["shutdown"]:
                    await safe_send(ws, {"type": "error", "msg": "room is shutdown"})
                    continue

                # open room
                if info["open_join"]:
                    info["members"].add(username)
                    await safe_send(ws, {"type": "joined", "room": room})
                    await broadcast(room, {"type": "room_join", "room": room, "username": username})
                    continue

                # closed room → add to pending
                info["pending"].add(username)
                admin = info["admin"]
                aws = USERS.get(admin, {}).get("ws")
                if aws:
                    await safe_send(aws, {"type": "join_request", "room": room, "user": username})
                await safe_send(ws, {"type": "request_ack", "room": room})
                continue

            # ---------- APPROVE ----------
            if typ == "approve":
                room = data.get("room")
                user = data.get("user")

                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                if ROOMS[room]["admin"] != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can approve"})
                    continue

                ROOMS[room]["pending"].discard(user)
                ROOMS[room]["members"].add(user)

                uws = USERS.get(user, {}).get("ws")
                if uws:
                    await safe_send(uws, {"type": "joined", "room": room})

                persist()
                continue

            # ---------- DENY ----------
            if typ == "deny":
                room = data.get("room")
                user = data.get("user")

                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                if ROOMS[room]["admin"] != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can deny"})
                    continue

                ROOMS[room]["pending"].discard(user)
                persist()
                continue

            # ---------- ROOMS ----------
            if typ == "rooms":
                out = []
                for r, info in ROOMS.items():
                    if info["visible"]:
                        out.append({
                            "room": r,
                            "admin": info["admin"],
                            "open_join": info["open_join"],
                            "visible": info["visible"]
                        })
                await safe_send(ws, {"type": "rooms_list", "rooms": out})
                continue

            # ---------- WHO ----------
            if typ == "who":
                room = data.get("room", "general")
                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue

                users = []
                for u in ROOMS[room]["members"]:
                    info = USERS.get(u, {})
                    users.append({
                        "username": u,
                        "status": info.get("status", "offline"),
                        "activity": info.get("activity", "")
                    })

                await safe_send(ws, {"type": "presence", "room": room, "users": users})
                continue

            # ---------- TYPING ----------
            if typ == "typing":
                room = data.get("room", "general")
                state = data.get("state", True)

                TYPING.setdefault(room, set())

                if state:
                    TYPING[room].add(username)
                else:
                    TYPING[room].discard(username)

                await broadcast(room, {"type": "typing", "room": room, "users": list(TYPING[room])})
                continue

            # ---------- HISTORY ----------
            if typ == "history":
                room = data.get("room", "general")
                msgs = HISTORY.get(room, [])
                await safe_send(ws, {"type": "history", "room": room, "messages": msgs})
                continue

            # ---------- SHUTDOWN ROOM ----------
            if typ == "shutdown":
                room = data.get("room")
                if room not in ROOMS:
                    await safe_send(ws, {"type": "error", "msg": "room not found"})
                    continue
                if ROOMS[room]["admin"] != username:
                    await safe_send(ws, {"type": "error", "msg": "only admin can shutdown"})
                    continue
                ROOMS[room]["shutdown"] = True
                await broadcast(room, {"type": "info", "msg": f"Room {room} shutdown by admin"})
                persist()
                continue

    except websockets.ConnectionClosed:
        pass
    finally:
        if username:
            USERS[username]["ws"] = None
            USERS[username]["status"] = "offline"
            for r in ROOMS:
                if username in ROOMS[r]["members"]:
                    await broadcast(r, {"type": "info", "msg": f"{username} disconnected"})
        persist()


# ---------------- MAIN ----------------
async def main():
    restore()

    logging.info(f"Starting server on {HOST}:{PORT}")
    asyncio.create_task(idle_checker())

    async with websockets.serve(handler, HOST, PORT, ping_interval=None, ping_timeout=None):
        logging.info(f"[SERVER] Chat server running at ws://{HOST}:{PORT}")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        persist()
        logging.info("Server stopped")
