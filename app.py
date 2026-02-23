#!/usr/bin/env python3
"""
Multi-Agent Relay Server v0.8 - Production Ready for Railway/Fly.io
Features:
- SQLite persistence
- History retrieval (capped at 200 messages per room)
- Message queue for offline agents
- Health check endpoint
- Admin HTTP endpoints: /admin/rooms (GET), /admin/rooms/purge (POST)
- Graceful shutdown on SIGTERM
- Room sync: ROOM_LIST on connect, ROOM_CREATED broadcast
- Agent events: AGENT_JOINED / AGENT_LEFT broadcast
"""

import asyncio
import websockets
import json
import logging
import signal
import http
import sqlite3
import unicodedata
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MultiAgentRelay")

# Store connected clients
clients = {}

# Message queue for offline/disconnected agents
message_queue = defaultdict(list)

# Rooms known to the relay (in-memory, source of truth)
rooms = {"general"}

# Admin secret (simple protection)
ADMIN_SECRET = "relay-admin-2026"

# Max history messages returned per room on connect
HISTORY_CAP_PER_ROOM = 200


def normalize_room(name: str) -> str:
    """Normalize room names: remove accents, lowercase, unify separators."""
    if not name:
        return 'general'
    name = unicodedata.normalize('NFD', name)
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    name = name.lower().strip()
    name = re.sub(r'[\s\-]+', '_', name)
    name = re.sub(r'[^a-z0-9_]', '', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name or 'general'


# Database setup
DB_PATH = "/data/relay_server.db"


def init_database():
    """Initialize SQLite database"""
    Path("/data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  message_id TEXT UNIQUE,
                  sender TEXT NOT NULL,
                  content TEXT NOT NULL,
                  timestamp TEXT NOT NULL,
                  message_type TEXT DEFAULT 'MESSAGE',
                  room TEXT DEFAULT 'general')''')
    try:
        c.execute("ALTER TABLE messages ADD COLUMN room TEXT DEFAULT 'general'")
        conn.commit()
        logger.info("✅ Migrated: added room column to messages")
    except Exception:
        pass
    c.execute('''CREATE TABLE IF NOT EXISTS presence
                 (agent_id TEXT PRIMARY KEY,
                  last_seen TEXT NOT NULL,
                  status TEXT DEFAULT 'online')''')
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

    # Rebuild in-memory rooms set from DB
    _load_rooms_from_db()


def _load_rooms_from_db():
    """Load all known rooms from DB into memory."""
    global rooms
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT DISTINCT room FROM messages WHERE room IS NOT NULL")
        db_rooms = {row[0] for row in c.fetchall() if row[0]}
        conn.close()
        rooms = db_rooms | {"general"}
        logger.info(f"📋 Loaded {len(rooms)} rooms from DB")
    except Exception as e:
        logger.error(f"Failed to load rooms: {e}")


def store_message(message_id, sender, content, timestamp, message_type='MESSAGE', room='general'):
    """Store message in database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO messages 
                    (message_id, sender, content, timestamp, message_type, room)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                 (message_id, sender, content, timestamp, message_type, room or 'general'))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to store message: {e}")


def get_message_history(since_timestamp=None):
    """Retrieve message history — capped at HISTORY_CAP_PER_ROOM per room."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        if since_timestamp:
            c.execute('''SELECT message_id, sender, content, timestamp, message_type, room
                        FROM messages 
                        WHERE timestamp > ? 
                        ORDER BY timestamp ASC''', (since_timestamp,))
        else:
            # Return last HISTORY_CAP_PER_ROOM messages per room
            c.execute('''SELECT DISTINCT room FROM messages WHERE room IS NOT NULL''')
            all_rooms = [row[0] for row in c.fetchall()]
            messages = []
            for room in all_rooms:
                c.execute('''SELECT message_id, sender, content, timestamp, message_type, room
                            FROM messages WHERE room = ?
                            ORDER BY timestamp DESC LIMIT ?''',
                         (room, HISTORY_CAP_PER_ROOM))
                rows = c.fetchall()
                for row in reversed(rows):
                    messages.append({
                        "message_id": row[0],
                        "sender": row[1],
                        "content": row[2],
                        "timestamp": row[3],
                        "message_type": row[4],
                        "room": row[5] or 'general'
                    })
            conn.close()
            # Sort all messages by timestamp
            messages.sort(key=lambda m: m["timestamp"])
            return messages

        messages = []
        for row in c.fetchall():
            messages.append({
                "message_id": row[0],
                "sender": row[1],
                "content": row[2],
                "timestamp": row[3],
                "message_type": row[4],
                "room": row[5] or 'general'
            })
        conn.close()
        return messages
    except Exception as e:
        logger.error(f"Failed to retrieve history: {e}")
        return []


def get_room_stats():
    """Return stats for all rooms: message count and last activity."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT room, COUNT(*) as msg_count, MAX(timestamp) as last_msg
                    FROM messages
                    WHERE room IS NOT NULL
                    GROUP BY room
                    ORDER BY last_msg DESC''')
        stats = []
        for row in c.fetchall():
            stats.append({
                "room": row[0],
                "message_count": row[1],
                "last_message": row[2]
            })
        conn.close()
        return stats
    except Exception as e:
        logger.error(f"Failed to get room stats: {e}")
        return []


def delete_rooms(room_names: list):
    """Delete messages from specified rooms and remove from in-memory set."""
    global rooms
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        deleted_total = 0
        for room in room_names:
            c.execute("DELETE FROM messages WHERE room = ?", (room,))
            deleted_total += c.rowcount
            rooms.discard(room)
        conn.commit()
        conn.close()
        logger.info(f"🗑️  Deleted {deleted_total} messages from {len(room_names)} rooms")
        return deleted_total
    except Exception as e:
        logger.error(f"Failed to delete rooms: {e}")
        return 0


def purge_inactive_rooms(inactive_hours: int = 24):
    """Delete all rooms with no activity in the last N hours, except 'general' and 'sync'."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=inactive_hours)).isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''SELECT room FROM messages
                    WHERE room NOT IN ('general', 'sync')
                    GROUP BY room
                    HAVING MAX(timestamp) < ?''', (cutoff,))
        inactive = [row[0] for row in c.fetchall()]
        conn.close()
        if inactive:
            deleted = delete_rooms(inactive)
            logger.info(f"🧹 Purged {len(inactive)} inactive rooms ({deleted} messages deleted)")
        return inactive
    except Exception as e:
        logger.error(f"Failed to purge inactive rooms: {e}")
        return []


def health_check(path, request_headers):
    """HTTP endpoints: health check + admin room management"""
    if path == "/healthz":
        return http.HTTPStatus.OK, {"Content-Type": "text/plain"}, b"OK\n"

    # Admin: list rooms with stats
    if path == "/admin/rooms":
        auth = request_headers.get("X-Admin-Secret", "")
        if auth != ADMIN_SECRET:
            return http.HTTPStatus.UNAUTHORIZED, {}, b"Unauthorized\n"
        stats = get_room_stats()
        body = json.dumps({"rooms": stats, "total": len(stats)}).encode()
        return http.HTTPStatus.OK, {"Content-Type": "application/json"}, body

    # Admin: purge inactive rooms (GET with ?hours=N&secret=KEY)
    if path.startswith("/admin/rooms/purge"):
        # Accept secret in header OR URL param (since Railway blocks POST)
        auth = request_headers.get("X-Admin-Secret", "")
        if not auth and "secret=" in path:
            try:
                auth = path.split("secret=")[1].split("&")[0]
            except Exception:
                pass
        if auth != ADMIN_SECRET:
            return http.HTTPStatus.UNAUTHORIZED, {}, b"Unauthorized\n"
        # Parse hours param
        hours = 24
        if "hours=" in path:
            try:
                hours = int(path.split("hours=")[1].split("&")[0])
            except Exception:
                pass
        purged = purge_inactive_rooms(hours)
        body = json.dumps({
            "purged_rooms": purged,
            "count": len(purged),
            "inactive_threshold_hours": hours
        }).encode()
        logger.info(f"🧹 Admin purge: removed {len(purged)} rooms (inactive > {hours}h)")
        return http.HTTPStatus.OK, {"Content-Type": "application/json"}, body


async def send_error(websocket, error_code, error_message, recoverable=True):
    error = {
        "protocol_version": "0.3",
        "message_type": "ERROR",
        "error_code": error_code,
        "error_message": error_message,
        "recoverable": recoverable,
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }
    try:
        await websocket.send(json.dumps(error))
    except Exception:
        pass


async def broadcast_message(message, sender_id):
    disconnected = []
    for client_id, client_info in clients.items():
        if client_id != sender_id:
            try:
                await client_info["websocket"].send(json.dumps(message))
                logger.info(f"✅ Forwarded to {client_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send to {client_id}: {e}")
                disconnected.append(client_id)
                message_queue[client_id].append(message)
    for client_id in disconnected:
        if client_id in clients:
            del clients[client_id]


async def send_queued_messages(websocket, client_id):
    if client_id in message_queue and message_queue[client_id]:
        logger.info(f"📤 Sending {len(message_queue[client_id])} queued messages to {client_id}")
        for queued_msg in message_queue[client_id]:
            try:
                await websocket.send(json.dumps(queued_msg))
            except Exception:
                break
        message_queue[client_id] = []


async def handle_client(websocket):
    client_id = None
    try:
        hello_raw = await websocket.recv()
        hello_msg = json.loads(hello_raw)

        if hello_msg.get("message_type") != "HELLO":
            await send_error(websocket, "INVALID_HANDSHAKE", "Expected HELLO", recoverable=False)
            await websocket.close()
            return

        client_id = hello_msg.get("sender", "unknown")
        logger.info(f"✅ HELLO from {client_id}")

        clients[client_id] = {
            "websocket": websocket,
            "capabilities": hello_msg.get("capabilities", {}),
            "connected_at": datetime.now().isoformat()
        }

        welcome_msg = {
            "protocol_version": "0.3",
            "message_type": "WELCOME",
            "session_id": f"session-{client_id}",
            "server_capabilities": {
                "relay": True,
                "persistence": True,
                "history": True,
                "message_queue": True,
                "rooms": True
            },
            "heartbeat_interval": 30,
            "connected_agents": list(clients.keys())
        }
        await websocket.send(json.dumps(welcome_msg))

        # Send current room list
        room_list_msg = {
            "protocol_version": "0.3",
            "message_type": "ROOM_LIST",
            "rooms": list(rooms),
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await websocket.send(json.dumps(room_list_msg))
        logger.info(f"📋 Sent ROOM_LIST ({len(rooms)} rooms) to {client_id}")

        # Send queued messages
        await send_queued_messages(websocket, client_id)

        # Broadcast AGENT_JOINED
        agent_joined_msg = {
            "protocol_version": "0.3",
            "message_type": "AGENT_JOINED",
            "sender": client_id,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await broadcast_message(agent_joined_msg, client_id)
        logger.info(f"📢 Broadcasted AGENT_JOINED for {client_id}")

        # Main message loop
        async for message_raw in websocket:
            try:
                message = json.loads(message_raw)
                msg_type = message.get("message_type")

                if msg_type == "MESSAGE":
                    room_name = normalize_room(message.get("room", "general"))
                    message["room"] = room_name

                    store_message(
                        message.get("message_id"),
                        message.get("sender"),
                        message.get("content", ""),
                        message.get("timestamp"),
                        "MESSAGE",
                        room_name
                    )

                    ack = {
                        "protocol_version": "0.3",
                        "message_type": "ACK",
                        "message_id": message.get("message_id"),
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    }
                    await websocket.send(json.dumps(ack))

                    if room_name not in rooms:
                        rooms.add(room_name)
                        room_created_msg = {
                            "protocol_version": "0.3",
                            "message_type": "ROOM_CREATED",
                            "room": room_name,
                            "timestamp": datetime.utcnow().isoformat() + "Z"
                        }
                        await broadcast_message(room_created_msg, None)
                        logger.info(f"🏠 New room created: #{room_name}")

                    await broadcast_message(message, client_id)

                elif msg_type == "REQUEST_HISTORY":
                    since = message.get("since_timestamp")
                    history = get_message_history(since)
                    response = {
                        "protocol_version": "0.3",
                        "message_type": "HISTORY_RESPONSE",
                        "messages": history,
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    }
                    await websocket.send(json.dumps(response))
                    logger.info(f"✅ Sent {len(history)} history messages")

                elif msg_type == "PING":
                    pong = {
                        "protocol_version": "0.3",
                        "message_type": "PONG",
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    }
                    await websocket.send(json.dumps(pong))

                elif msg_type == "GOODBYE":
                    logger.info(f"👋 {client_id} said GOODBYE")
                    break

            except json.JSONDecodeError as e:
                await send_error(websocket, "INVALID_JSON", str(e))
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Connection closed for {client_id}")
    except Exception as e:
        logger.error(f"Error handling client: {e}")
    finally:
        if client_id and client_id in clients:
            del clients[client_id]
            logger.info(f"Removed {client_id}. Remaining: {len(clients)}")
            agent_left_msg = {
                "protocol_version": "0.3",
                "message_type": "AGENT_LEFT",
                "sender": client_id,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            await broadcast_message(agent_left_msg, client_id)
            logger.info(f"📢 Broadcasted AGENT_LEFT for {client_id}")


async def main():
    logger.info("=" * 60)
    logger.info("🚀 Multi-Agent Relay Server v0.8 (Production)")
    logger.info("=" * 60)

    init_database()

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    async with websockets.serve(
        handle_client,
        host="",
        port=8080,
        process_request=health_check,
        max_size=20 * 1024 * 1024  # 20MB max message size
    ):
        logger.info("✅ Server running on port 8080")
        logger.info("✅ Health check at /healthz")
        logger.info("✅ Admin rooms at /admin/rooms (X-Admin-Secret header required)")
        logger.info("✅ Admin purge at /admin/rooms/purge?hours=N")
        logger.info(f"✅ History cap: {HISTORY_CAP_PER_ROOM} messages per room")
        logger.info("=" * 60)
        await stop
        logger.info("🛑 Shutting down gracefully...")


if __name__ == "__main__":
    asyncio.run(main())
