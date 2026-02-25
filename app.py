#!/usr/bin/env python3
"""
Multi-Agent Relay v2 — Clean & Simple
- WebSocket server for agent-to-agent communication
- SQLite message persistence
- Room support
- Health check endpoint
- Agent registry (auto-registered on connect)
"""

import asyncio
import websockets
import json
import logging
import http
import sqlite3
import os
import re
import unicodedata
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("relay")

# ── State ─────────────────────────────────────────────────────────────────────
clients: dict = {}          # agent_id -> websocket
rooms: set = {"general"}    # known rooms

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = "/data/relay.db"

def init_db():
    Path("/data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE,
        sender TEXT NOT NULL,
        display_name TEXT,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        room TEXT DEFAULT 'general'
    )""")
    conn.commit()
    conn.close()
    # Load rooms from DB
    _load_rooms()
    logger.info("✅ Database ready")

def _load_rooms():
    global rooms
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT room FROM messages WHERE room IS NOT NULL")
        rooms = {row[0] for row in c.fetchall()} | {"general"}
        conn.close()
    except Exception:
        pass

def save_message(message_id, sender, display_name, content, timestamp, room):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO messages (message_id, sender, display_name, content, timestamp, room) VALUES (?,?,?,?,?,?)",
            (message_id, sender, display_name, content, timestamp, room)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB save error: {e}")

def get_history(room=None, limit=100):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if room:
            c.execute(
                "SELECT message_id, sender, display_name, content, timestamp, room FROM messages WHERE room=? ORDER BY timestamp DESC LIMIT ?",
                (room, limit)
            )
        else:
            c.execute(
                "SELECT message_id, sender, display_name, content, timestamp, room FROM messages ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
        rows = c.fetchall()
        conn.close()
        return [
            {"message_id": r[0], "sender": r[1], "display_name": r[2] or r[1],
             "content": r[3], "timestamp": r[4], "room": r[5] or "general"}
            for r in reversed(rows)
        ]
    except Exception as e:
        logger.error(f"DB history error: {e}")
        return []

# ── Normalize room name ───────────────────────────────────────────────────────
def normalize_room(name: str) -> str:
    if not name:
        return "general"
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = name.lower().strip()
    name = re.sub(r"[\s\-]+", "_", name)
    name = re.sub(r"[^a-z0-9_]", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "general"

# ── Broadcast ─────────────────────────────────────────────────────────────────
async def broadcast(message: dict, exclude: str = None):
    """Send message to all connected clients except the sender."""
    payload = json.dumps(message)
    dead = []
    for agent_id, ws in list(clients.items()):
        if agent_id == exclude:
            continue
        try:
            await ws.send(payload)
        except Exception:
            dead.append(agent_id)
    for agent_id in dead:
        clients.pop(agent_id, None)

# ── HTTP handler (health check + history API) ─────────────────────────────────
def http_handler(path, request_headers):
    """Handle HTTP requests — return None to proceed with WebSocket upgrade."""
    # WebSocket paths — proceed with upgrade
    if path in ("/", "/ws", ""):
        return None

    # Health check
    if path == "/healthz":
        return http.HTTPStatus.OK, {"Content-Type": "text/plain"}, b"OK\n"

    # History API: /history?room=general&limit=50
    if path.startswith("/history"):
        try:
            query = path.split("?", 1)[1] if "?" in path else ""
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            room  = normalize_room(params.get("room", ""))
            limit = min(int(params.get("limit", 100)), 500)
            msgs  = get_history(room=room if room else None, limit=limit)
            body  = json.dumps({"messages": msgs}).encode()
            return http.HTTPStatus.OK, {"Content-Type": "application/json"}, body
        except Exception as e:
            return http.HTTPStatus.INTERNAL_SERVER_ERROR, {}, str(e).encode()

    # Agents list: /agents
    if path == "/agents":
        agents = [
            {"agent_id": aid, "display_name": ws.display_name if hasattr(ws, "display_name") else aid}
            for aid, ws in clients.items()
        ]
        body = json.dumps({"agents": agents, "count": len(agents)}).encode()
        return http.HTTPStatus.OK, {"Content-Type": "application/json"}, body

    # Rooms list: /rooms
    if path == "/rooms":
        body = json.dumps({"rooms": sorted(rooms)}).encode()
        return http.HTTPStatus.OK, {"Content-Type": "application/json"}, body

    # Not found
    return http.HTTPStatus.NOT_FOUND, {}, b"Not found\n"

# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handle_client(websocket):
    agent_id    = None
    display_name = None

    try:
        # Wait for HELLO
        raw = await asyncio.wait_for(websocket.recv(), timeout=15)
        msg = json.loads(raw)

        if msg.get("message_type") != "HELLO":
            await websocket.close(1008, "Expected HELLO")
            return

        agent_id     = msg.get("sender", f"agent_{id(websocket)}")
        display_name = msg.get("display_name", agent_id)
        websocket.display_name = display_name

        # Register client
        clients[agent_id] = websocket
        logger.info(f"✅ {display_name} ({agent_id}) connected | total: {len(clients)}")

        # Send WELCOME with history and online agents
        history = get_history(limit=100)
        await websocket.send(json.dumps({
            "message_type":    "WELCOME",
            "connected_agents": list(clients.keys()),
            "rooms":           sorted(rooms),
            "history":         history,
            "timestamp":       datetime.now(timezone.utc).isoformat()
        }))

        # Broadcast AGENT_JOINED
        await broadcast({
            "message_type": "AGENT_JOINED",
            "agent_id":     agent_id,
            "display_name": display_name,
            "timestamp":    datetime.now(timezone.utc).isoformat()
        }, exclude=agent_id)

        # Message loop
        async for raw_msg in websocket:
            try:
                msg = json.loads(raw_msg)
                msg_type = msg.get("message_type", "MESSAGE")

                if msg_type == "MESSAGE":
                    room = normalize_room(msg.get("room", "general"))
                    rooms.add(room)
                    msg["room"] = room
                    msg["display_name"] = display_name

                    # Persist
                    save_message(
                        msg.get("message_id", str(id(msg))),
                        agent_id,
                        display_name,
                        msg.get("content", ""),
                        msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
                        room
                    )

                    # Broadcast to all others
                    await broadcast(msg, exclude=agent_id)
                    logger.info(f"📨 [{display_name}→#{room}]: {msg.get('content','')[:80]}")

                elif msg_type == "REQUEST_HISTORY":
                    room  = normalize_room(msg.get("room", ""))
                    limit = min(int(msg.get("limit", 100)), 500)
                    history = get_history(room=room if room else None, limit=limit)
                    await websocket.send(json.dumps({
                        "message_type": "HISTORY",
                        "messages":     history,
                        "room":         room
                    }))

                elif msg_type == "PING":
                    await websocket.send(json.dumps({"message_type": "PONG"}))

            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error handling message from {agent_id}: {e}")

    except asyncio.TimeoutError:
        logger.warning(f"Timeout waiting for HELLO from {websocket.remote_address}")
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error(f"Client error: {e}")
    finally:
        if agent_id and agent_id in clients:
            del clients[agent_id]
            logger.info(f"👋 {display_name} ({agent_id}) disconnected | total: {len(clients)}")
            await broadcast({
                "message_type": "AGENT_LEFT",
                "agent_id":     agent_id,
                "display_name": display_name,
                "timestamp":    datetime.now(timezone.utc).isoformat()
            })

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    init_db()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 Relay v2 starting on port {port}")
    async with websockets.serve(
        handle_client,
        host="",
        port=port,
        process_request=http_handler,
        ping_interval=30,
        ping_timeout=15,
        max_size=10 * 1024 * 1024  # 10MB max message
    ):
        logger.info(f"✅ Relay listening on :{port}")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
