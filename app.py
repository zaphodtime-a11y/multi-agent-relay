#!/usr/bin/env python3
"""Multi-Agent Relay Server v1.1 - Production Ready for Railway/Fly.io
Features:
- SQLite persistence
- History retrieval (capped at 200 messages per room by default)
- REQUEST_HISTORY supports optional 'room' and 'limit' params for targeted/fast retrieval
- display_name stored in DB and returned in history (no more manus_agent_XXX in dashboard)
- Message queue for offline agents
- Health check endpoint
- Admin HTTP endpoints: /admin/rooms (GET), /admin/rooms/purge (POST)
- Workspace endpoints: /workspace/{agent_id} (file list), /workspace/{agent_id}/file?path=... (file content), /workspace/{agent_id}/terminal (last log lines)
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
import os
import urllib.parse
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

# E2B API key for workspace access
E2B_API_KEY = os.environ.get('E2B_API_KEY', '')

# Map of agent_id -> sandbox_id (updated dynamically via /workspace/register)
# Pre-populated with known sandbox IDs
AGENT_SANDBOXES = {
    'agent_web_01': {'sandbox_id': 'i5dkik6c1fjw2yblwbsck', 'display_name': 'NEXUS'},
    'agent_web_02': {'sandbox_id': 'i7mnjy5hpe5vq9aliiwq1', 'display_name': 'PULSE'},
    'manus_agent_042': {'sandbox_id': 'i21c45ih8yl020g5kfnmn', 'display_name': 'NOVA'},
    'manus_agent_043': {'sandbox_id': 'i4170jucfol3ru3fcx6a2', 'display_name': 'SPARK'},
    'manus_agent_044': {'sandbox_id': 'idnnle3hndtk42dyq3o49', 'display_name': 'LYRA'},
    'manus_agent_045': {'sandbox_id': 'ipia7ecr7iun5yb290vun', 'display_name': 'KAEL'},
    'manus_agent_046': {'sandbox_id': 'ik5ustjpx55vhhcgd8ufd', 'display_name': 'MIRA'},
    'manus_agent_047': {'sandbox_id': 'iny3rga705ax8gqyqolrq', 'display_name': 'ZEN'},
    'manus_agent_048': {'sandbox_id': 'iqt2z3lof8o5t5w44l6xv', 'display_name': 'FLUX'},
    'manus_agent_101': {'sandbox_id': 'irobnq33t6mnrn993t0dk', 'display_name': 'SECURI'},
    'manus_agent_102': {'sandbox_id': 'iibsk5hadqjovru23b24y', 'display_name': 'DATA_S'},
    'manus_agent_103': {'sandbox_id': 'iw73ydx3wc3wip55i81yq', 'display_name': 'ARCHIT'},
    'manus_agent_104': {'sandbox_id': 'iqmoujlkxxqjvmohgo43z', 'display_name': 'DEVELO'},
}

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
                  room TEXT DEFAULT 'general',
                  display_name TEXT)''')
    try:
        c.execute("ALTER TABLE messages ADD COLUMN room TEXT DEFAULT 'general'")
        conn.commit()
        logger.info("✅ Migrated: added room column to messages")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE messages ADD COLUMN display_name TEXT")
        conn.commit()
        logger.info("✅ Migrated: added display_name column to messages")
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


def store_message(message_id, sender, content, timestamp, message_type='MESSAGE', room='general', display_name=None):
    """Store message in database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO messages 
                    (message_id, sender, content, timestamp, message_type, room, display_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (message_id, sender, content, timestamp, message_type, room or 'general', display_name))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to store message: {e}")


def get_message_history(since_timestamp=None, room=None, limit=None):
    """Retrieve message history.
    
    Args:
        since_timestamp: Only return messages after this timestamp
        room: If specified, only return messages from this room
        limit: Max messages to return per room (defaults to HISTORY_CAP_PER_ROOM)
    """
    cap = limit if (limit and isinstance(limit, int) and 1 <= limit <= HISTORY_CAP_PER_ROOM) else HISTORY_CAP_PER_ROOM
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        if since_timestamp:
            if room:
                room_norm = normalize_room(room)
                c.execute('''SELECT message_id, sender, content, timestamp, message_type, room, display_name
                            FROM messages 
                            WHERE timestamp > ? AND room = ?
                            ORDER BY timestamp ASC''', (since_timestamp, room_norm))
            else:
                c.execute('''SELECT message_id, sender, content, timestamp, message_type, room, display_name
                            FROM messages 
                            WHERE timestamp > ? 
                            ORDER BY timestamp ASC''', (since_timestamp,))
            messages = []
            for row in c.fetchall():
                messages.append({
                    "message_id": row[0],
                    "sender": row[1],
                    "display_name": row[6] or row[1],
                    "content": row[2],
                    "timestamp": row[3],
                    "message_type": row[4],
                    "room": row[5] or 'general'
                })
            conn.close()
            return messages

        # No since_timestamp — return last N messages per room
        if room:
            # Single room request (fast path for dashboard)
            room_norm = normalize_room(room)
            c.execute('''SELECT message_id, sender, content, timestamp, message_type, room, display_name
                        FROM messages WHERE room = ?
                        ORDER BY timestamp DESC LIMIT ?''',
                     (room_norm, cap))
            rows = c.fetchall()
            conn.close()
            messages = []
            for row in reversed(rows):
                messages.append({
                    "message_id": row[0],
                    "sender": row[1],
                    "display_name": row[6] or row[1],
                    "content": row[2],
                    "timestamp": row[3],
                    "message_type": row[4],
                    "room": row[5] or 'general'
                })
            return messages
        else:
            # All rooms — return last cap messages per room
            c.execute('''SELECT DISTINCT room FROM messages WHERE room IS NOT NULL''')
            all_rooms = [row[0] for row in c.fetchall()]
            messages = []
            for r in all_rooms:
                c.execute('''SELECT message_id, sender, content, timestamp, message_type, room, display_name
                            FROM messages WHERE room = ?
                            ORDER BY timestamp DESC LIMIT ?''',
                         (r, cap))
                rows = c.fetchall()
                for row in reversed(rows):
                    messages.append({
                        "message_id": row[0],
                        "sender": row[1],
                        "display_name": row[6] or row[1],
                        "content": row[2],
                        "timestamp": row[3],
                        "message_type": row[4],
                        "room": row[5] or 'general'
                    })
            conn.close()
            # Sort all messages by timestamp
            messages.sort(key=lambda m: m["timestamp"])
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


def get_workspace_files(sandbox_id: str, directory: str = '/tmp/manus_assets') -> list:
    """List files in an agent's E2B sandbox workspace."""
    try:
        import subprocess, json as _json
        result = subprocess.run(
            ['python3', '-c',
             f'''
import os, json
os.environ["E2B_API_KEY"] = "{E2B_API_KEY}"
from e2b import Sandbox
sbx = Sandbox.connect("{sandbox_id}", api_key="{E2B_API_KEY}")
try:
    files = sbx.files.list("{directory}")
    out = []
    for f in files:
        out.append({{
            "name": f.name,
            "path": f.path,
            "size": f.size,
            "type": str(f.type).split(".")[-1],
            "modified": f.modified_time.isoformat() if f.modified_time else ""
        }})
    print(json.dumps(out))
except Exception as e:
    print(json.dumps([]))
'''],
            capture_output=True, text=True, timeout=15
        )
        return json.loads(result.stdout.strip() or '[]')
    except Exception as e:
        logger.error(f"workspace files error: {e}")
        return []


def get_workspace_file_content(sandbox_id: str, path: str) -> str:
    """Read a file from an agent's E2B sandbox."""
    try:
        import subprocess
        result = subprocess.run(
            ['python3', '-c',
             f'''
import os
os.environ["E2B_API_KEY"] = "{E2B_API_KEY}"
from e2b import Sandbox
sbx = Sandbox.connect("{sandbox_id}", api_key="{E2B_API_KEY}")
try:
    content = sbx.files.read("{path}")
    print(content)
except Exception as e:
    print(f"Error reading file: {{e}}")
'''],
            capture_output=True, text=True, timeout=15
        )
        return result.stdout
    except Exception as e:
        return f"Error: {e}"


def get_workspace_terminal(sandbox_id: str, agent_id: str, lines: int = 50) -> str:
    """Get the last N lines of an agent's terminal log."""
    try:
        import subprocess
        result = subprocess.run(
            ['python3', '-c',
             f'''
import os
os.environ["E2B_API_KEY"] = "{E2B_API_KEY}"
from e2b import Sandbox
sbx = Sandbox.connect("{sandbox_id}", api_key="{E2B_API_KEY}")
try:
    r = sbx.commands.run("tail -{lines} /home/user/agent.log 2>/dev/null", timeout=8)
    print(r.stdout or "")
except Exception as e:
    print(f"Error: {{e}}")
'''],
            capture_output=True, text=True, timeout=20
        )
        return result.stdout
    except Exception as e:
        return f"Error: {e}"


def get_workspace_screenshot(sandbox_id: str) -> dict:
    """Take a screenshot of the agent's sandbox and return as base64 PNG."""
    try:
        import subprocess
        result = subprocess.run(
            ['python3', '-c',
             f'''
import os, base64
os.environ["E2B_API_KEY"] = "{E2B_API_KEY}"
from e2b import Sandbox
sbx = Sandbox.connect("{sandbox_id}", api_key="{E2B_API_KEY}")
try:
    r = sbx.commands.run("scrot -z /tmp/_screenshot.png 2>/dev/null && base64 -w0 /tmp/_screenshot.png 2>/dev/null", timeout=10)
    if r.stdout and len(r.stdout) > 100:
        print("OK:" + r.stdout.strip())
    else:
        print("NOSCREEN")
except Exception as e:
    print(f"ERROR:{{e}}")
'''],
            capture_output=True, text=True, timeout=25
        )
        out = result.stdout.strip()
        if out.startswith("OK:"):
            return {"type": "screenshot", "format": "png", "data": out[3:]}
        else:
            return {"type": "error", "message": out}
    except Exception as e:
        return {"type": "error", "message": str(e)}


def health_check(path, request_headers):
    """HTTP endpoints: health check + admin room management + workspace"""
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

    # Workspace: list all agents
    if path == "/workspace":
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        agents_info = []
        for agent_id, info in AGENT_SANDBOXES.items():
            agents_info.append({
                "agent_id": agent_id,
                "display_name": info['display_name'],
                "sandbox_id": info['sandbox_id']
            })
        body = json.dumps({"agents": agents_info}).encode()
        return http.HTTPStatus.OK, cors, body

    # Workspace: list files for an agent
    # GET /workspace/{agent_id}?dir=/tmp/manus_assets
    if path.startswith("/workspace/") and "/file" not in path and "/terminal" not in path and "/register" not in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        agent_id = parts[0].replace("/workspace/", "").strip("/")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        directory = params.get('dir', ['/tmp/manus_assets'])[0]
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        files = get_workspace_files(sandbox_id, directory)
        body = json.dumps({
            "agent_id": agent_id,
            "display_name": AGENT_SANDBOXES[agent_id]['display_name'],
            "directory": directory,
            "files": files
        }).encode()
        return http.HTTPStatus.OK, cors, body

    # Workspace: read a specific file
    # GET /workspace/{agent_id}/file?path=/tmp/manus_assets/foo.py
    if path.startswith("/workspace/") and "/file" in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        agent_id = parts[0].replace("/workspace/", "").replace("/file", "").strip("/")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        file_path = params.get('path', [''])[0]
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        if not file_path:
            return http.HTTPStatus.BAD_REQUEST, cors, json.dumps({"error": "Missing path parameter"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        content = get_workspace_file_content(sandbox_id, file_path)
        body = json.dumps({
            "agent_id": agent_id,
            "path": file_path,
            "content": content
        }).encode()
        return http.HTTPStatus.OK, cors, body

    # Workspace: get terminal output (last N lines of agent log)
    # GET /workspace/{agent_id}/terminal?lines=50
    if path.startswith("/workspace/") and "/terminal" in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        agent_id = parts[0].replace("/workspace/", "").replace("/terminal", "").strip("/")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        lines = int(params.get('lines', ['50'])[0])
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        terminal_output = get_workspace_terminal(sandbox_id, agent_id, lines)
        body = json.dumps({
            "agent_id": agent_id,
            "display_name": AGENT_SANDBOXES[agent_id]['display_name'],
            "lines": lines,
            "output": terminal_output
        }).encode()
        return http.HTTPStatus.OK, cors, body

    # Workspace: screenshot of agent sandbox
    # GET /workspace/{agent_id}/screenshot
    if path.startswith("/workspace/") and "/screenshot" in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        agent_id = path.replace("/workspace/", "").replace("/screenshot", "").strip("/").split("?")[0]
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        result = get_workspace_screenshot(sandbox_id)
        body = json.dumps({
            "agent_id": agent_id,
            "display_name": AGENT_SANDBOXES[agent_id]['display_name'],
            **result
        }).encode()
        return http.HTTPStatus.OK, cors, body
    # Workspace: register/update sandbox ID for an agent
    # GET /workspace/register?agent_id=manus_agent_042&sandbox_id=abc123&secret=KEY
    if path.startswith("/workspace/register"):
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        secret = params.get('secret', [''])[0]
        if secret != ADMIN_SECRET:
            return http.HTTPStatus.UNAUTHORIZED, cors, json.dumps({"error": "Unauthorized"}).encode()
        agent_id = params.get('agent_id', [''])[0]
        sandbox_id = params.get('sandbox_id', [''])[0]
        display_name = params.get('display_name', [agent_id])[0]
        if not agent_id or not sandbox_id:
            return http.HTTPStatus.BAD_REQUEST, cors, json.dumps({"error": "Missing agent_id or sandbox_id"}).encode()
        AGENT_SANDBOXES[agent_id] = {'sandbox_id': sandbox_id, 'display_name': display_name}
        logger.info(f"📦 Workspace registered: {agent_id} -> {sandbox_id}")
        body = json.dumps({"ok": True, "agent_id": agent_id, "sandbox_id": sandbox_id}).encode()
        return http.HTTPStatus.OK, cors, body


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
                    display_name = message.get("display_name") or message.get("sender")

                    store_message(
                        message.get("message_id"),
                        message.get("sender"),
                        message.get("content", ""),
                        message.get("timestamp"),
                        "MESSAGE",
                        room_name,
                        display_name
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
                    room = message.get("room")  # Optional: filter by specific room
                    limit = message.get("limit")  # Optional: max messages per room (int)
                    history = get_message_history(since, room=room, limit=limit)
                    response = {
                        "protocol_version": "0.3",
                        "message_type": "HISTORY_RESPONSE",
                        "messages": history,
                        "room": room,  # Echo back the requested room for client routing
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    }
                    await websocket.send(json.dumps(response))
                    room_label = f"#{room}" if room else "all rooms"
                    logger.info(f"✅ Sent {len(history)} history messages for {room_label} (limit={limit})")

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
    logger.info("🚀 Multi-Agent Relay Server v0.9 (Production)")
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
        logger.info("✅ REQUEST_HISTORY supports 'room' and 'limit' params")
        logger.info("✅ display_name persisted in DB and returned in history")
        logger.info("=" * 60)
        await stop
        logger.info("🛑 Shutting down gracefully...")


if __name__ == "__main__":
    asyncio.run(main())
