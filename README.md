# Terminal-Based Real-Time Chat Application
A real-time, multi-user chat system built using Python, AsyncIO, and WebSockets.  
Supports authentication, chat rooms, private messaging, join approvals, presence updates, typing indicators, and persistent storage.

---

## Features

### User Authentication
- Register a new user
- Login with username and password
- Credentials stored locally in `users.json`

### Real-Time Messaging
- Broadcast messages in rooms
- Private direct messages (DM)
- Room-specific message history (up to 200 messages)

### Rooms System
- Create rooms with:
  - open or closed join settings
  - visible or hidden room listing
- Join open rooms instantly
- Closed rooms require admin approval
- Room editing by admin (change visibility/open join)
- Room shutdown by admin

### Presence Tracking
- Tracks status: **online**, **idle**, **offline**
- Idle after 5 minutes of inactivity
- Presence updates are broadcast to room members

### Typing Indicators
- Shows users currently typing in a room

### Persistent Storage
The server maintains:
- `users.json` — user credentials  
- `rooms.json` — room metadata  
- `history.json` — messages per room  

All data is restored when the server restarts.

### Cross-Platform Support
- Works on **Windows**, **macOS**, and **Linux**
- Supports LAN and remote access using ngrok

---

## Project Structure

```
project/
│
├── server.py          # Main WebSocket server
├── client.py          # Windows/Linux client
├── client_macos.py    # macOS-stable client
│
├── users.json         # User credentials
├── rooms.json         # Room configurations
├── history.json       # Room chat history
│
├── server.log         # Logging output
└── README.md
```

---

## Installation

### 1. Install Python 3.10 or higher  
Download from: https://www.python.org

### 2. Install required packages
```
pip install websockets
```

### 3. (Optional) Use a virtual environment
```
python -m venv venv
source venv/bin/activate      # macOS/Linux
venv\Scripts\activate         # Windows
```

---

## Running the Server

Inside the project folder:

```
python server.py
```

Expected output:

```
[SERVER] Chat server running at ws://0.0.0.0:8765
```

### Remote Access (Optional)
Use ngrok if connecting across networks:

```
ngrok http 8765
```

or for TCP:

```
ngrok tcp 8765
```

The generated URL will be used by the client.

---

## Running the Client

Format:

```
python client.py ws://<host>:<port>
```

Example (local):

```
python client.py ws://localhost:8765
```

Example (ngrok TCP tunnel):

```
python client.py ws://0.tcp.in.ngrok.io:12345
```

---

## Supported Commands

### Authentication
```
/login <username> <password>
/register <username> <password> <password>
```

### Room Management
```
/rooms
/join <room>
/createroom <room> <open> <visible>
/editroom <room> <open> <visible>
/shutdown <room>
```

### Join Approval (Closed Rooms)
```
/approve <room> <user>
/deny <room> <user>
```

### Messaging
```
/dm <user> <message>
```

### User/Room Info
```
/who
/history
```

### Help
```
/help
```

---

## Architecture Overview

### WebSocket Server
The server uses `asyncio` + `websockets` to manage multiple clients concurrently.  
Each connected client runs inside the `handler()` coroutine.

### Data Persistence
All user, room, and history information is stored in JSON files:

- `users.json`
- `rooms.json`
- `history.json`

The server loads these on startup and updates them after every change.

### Room Structure
Each room contains:
- admin (creator)
- open/closed join flag
- visible/hidden flag
- members list
- pending join requests
- shutdown state

### User Structure
Each user stores:
- password
- websocket connection
- status (online/idle/offline)
- last active timestamp
- activity string

### Idle Checker
A background task updates user presence every 5 seconds.
