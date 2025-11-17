import asyncio
import websockets
import json
import sys
import threading
import queue

# ================= WINDOWS FIX =================
sys.stdout.reconfigure(line_buffering=True)

input_queue = queue.Queue()
active_room = "general"
logged_in = False
ws_global = None
my_username = None

def safe_print(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

def input_thread():
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                continue
            input_queue.put(line.strip())
        except:
            pass

async def send_json(obj):
    if ws_global:
        try:
            await ws_global.send(json.dumps(obj))
        except:
            pass

async def async_input_loop():
    global logged_in, active_room

    while True:
        line = await asyncio.to_thread(input_queue.get)
        if not line:
            continue

        # ---------- AUTH ----------
        if not logged_in:
            if line.startswith("/login "):
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    safe_print("Usage: /login <user> <pwd>")
                    continue
                await send_json({"type":"auth","action":"login","username":parts[1],"password":parts[2]})
                continue

            if line.startswith("/register "):
                parts = line.split(" ", 3)
                if len(parts) < 4:
                    safe_print("Usage: /register <user> <pwd> <pwd>")
                    continue
                if parts[2] != parts[3]:
                    safe_print("Passwords do not match.")
                    continue
                await send_json({"type":"auth","action":"register","username":parts[1],"password":parts[2]})
                continue

            safe_print("You must /login or /register first.")
            continue

        # ---------- COMMANDS ----------
        if line == "/quit":
            await ws_global.close()
            break

        if line == "/help":
            await send_json({"type":"message","room":active_room,"text":"/help"})
            continue

        if line == "/rooms":
            await send_json({"type":"rooms"})
            continue

        if line == "/who":
            await send_json({"type":"who","room":active_room})
            continue

        if line.startswith("/join "):
            room = line.split(" ",1)[1]
            active_room = room
            await send_json({"type":"join","room":room})
            continue

        if line.startswith("/createroom "):
            parts = line.split(" ")
            if len(parts) != 4:
                safe_print("Usage: /createroom <room> <open:true|false> <visible:true|false>")
                continue
            await send_json({"type":"createroom","room":parts[1],"open_join":parts[2]=="true","visible":parts[3]=="true"})
            continue

        if line.startswith("/editroom "):
            parts = line.split(" ")
            if len(parts) != 4:
                safe_print("Usage: /editroom <room> <open:true|false> <visible:true|false>")
                continue
            await send_json({"type":"editroom","room":parts[1],"open_join":parts[2]=="true","visible":parts[3]=="true"})
            continue

        if line.startswith("/dm "):
            parts = line.split(" ",2)
            if len(parts)<3:
                safe_print("Usage: /dm <user> <msg>")
                continue
            await send_json({"type":"dm","to":parts[1],"text":parts[2]})
            continue

        # ---------- MESSAGE ----------
        await send_json({"type":"message","room":active_room,"text":line})

async def receiver(ws):
    global logged_in, my_username

    safe_print("[Connected] Use /login or /register")

    try:
        async for raw in ws:
            data = json.loads(raw)
            typ = data.get("type")

            if typ == "auth_ok":
                logged_in = True
                my_username = data["msg"].split()[-1]
                safe_print("[AUTH OK] " + data["msg"])
                continue

            if typ == "error":
                safe_print("[ERROR] " + data["msg"])
                continue

            if typ == "info":
                safe_print("[INFO] " + data["msg"])
                continue

            if typ == "message":
                safe_print(f"[{data['room']}] {data['username']}: {data['text']}")
                continue

            if typ == "room_join":
                safe_print(f"[INFO] {data['username']} joined {data['room']}")
                continue

            if typ == "dm":
                safe_print(f"[DM from {data['from']}] {data['text']}")
                continue

            if typ == "dm_sent":
                safe_print(f"[DM to {data['to']}] {data['text']}")
                continue

            if typ == "rooms_list":
                safe_print("----- Rooms -----")
                for r in data["rooms"]:
                    safe_print(f"{r['room']} | admin={r['admin']} | open={r['open_join']} | visible={r['visible']}")
                continue

            if typ == "presence":
                safe_print(f"----- Users in {data['room']} -----")
                for u in data["users"]:
                    safe_print(f"{u['username']} : {u['status']}")
                continue

            if typ == "join_request":
                safe_print(f"[JOIN REQUEST] {data['user']} wants to join {data['room']}")
                safe_print(f"Use /approve {data['room']} {data['user']}")
                continue

            if typ == "history":
                safe_print(f"----- History {data['room']} -----")
                for m in data["messages"]:
                    safe_print(f"[{m['room']}] {m['username']}: {m['text']}")
                continue

    except websockets.ConnectionClosed:
        safe_print("[Disconnected]")

async def main():
    global ws_global

    if len(sys.argv) < 2:
        print("Usage: python client_windows.py ws://<server>/ws")
        return

    url = sys.argv[1]
    safe_print(f"[CLIENT] Connecting to {url}")

    threading.Thread(target=input_thread, daemon=True).start()

    try:
        async with websockets.connect(url) as ws:
            ws_global = ws
            await asyncio.gather(receiver(ws), async_input_loop())
    except Exception as e:
        safe_print(f"[ERROR] {e}")

if __name__ == "__main__":
    asyncio.run(main())
