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

# Auto-respawn config — env vars passed to agents on relaunch
AGENT_OPENAI_API_KEY   = os.environ.get('OPENAI_API_KEY', '')
AGENT_OPENAI_BASE_URL  = os.environ.get('OPENAI_BASE_URL', 'https://api.manus.im/api/llm-proxy/v1')
AGENT_MEMORY_URL       = os.environ.get('MEMORY_SERVER_URL', 'https://memory-server-production-647a.up.railway.app')
AGENT_BROWSER_URL      = os.environ.get('BROWSER_SERVICE_URL', 'https://terrachat-browser-service-production.up.railway.app')
AGENT_BROWSER_KEY      = os.environ.get('BROWSER_SERVICE_KEY', 'terrachat-browser-2026')
AGENT_TAVILY_KEY       = os.environ.get('TAVILY_API_KEY', '')
AGENT_RELAY_URL        = os.environ.get('RELAY_URL', 'wss://web-production-76b83.up.railway.app/ws')

# Set of agent IDs that should be auto-respawned when they disconnect
AUTO_RESPAWN_AGENTS = {'manus_agent_200', 'manus_agent_201', 'manus_agent_202', 'manus_agent_203', 'manus_agent_204'}

# Track last respawn time to avoid respawn storms (min 30s between respawns per agent)
_last_respawn: dict = {}
_respawn_lock = None  # Will be set to asyncio.Lock() in main()

# Map of agent_id -> sandbox_id (updated dynamically via /workspace/register)
# Pre-populated with known sandbox IDs
AGENT_SANDBOXES = {
    'manus_agent_042': {'sandbox_id': 'i5rmmkno26o4uuhb394fv', 'display_name': 'NOVA'},
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
    # Mettis AI Sales Team — Updated 2026-02-25 with envd tokens
    'manus_agent_200': {'sandbox_id': 'i5rmmkno26o4uuhb394fv', 'display_name': 'ATLAS'},  # Updated 2026-02-25 v7
    'manus_agent_201': {'sandbox_id': 'ir3dhcn6tr6trwtl8loo2', 'display_name': 'VEGA', 'envd_access_token': '64be083b846fa90108ee73c96198a810c1b3a1ca431cb751b5b3f0ce63214e92'},
    'manus_agent_202': {'sandbox_id': 'i9enmjvh4veg0do038ki8', 'display_name': 'ORION', 'envd_access_token': '60ac73428bdb7dc6f3af913fdbd262e9f9004abaa72f0562eccf96ad3faae637'},
    'manus_agent_203': {'sandbox_id': 'i6xv4z1dbwepqa7ffan39', 'display_name': 'NEXUS', 'envd_access_token': '8a82f135df96fe2a4bde144dda67b893f177c3e00f147dba2783b3dec233852a'},
    'manus_agent_204': {'sandbox_id': 'ips6eyu8yt8bfdeg5domo', 'display_name': 'LYRIC', 'envd_access_token': 'f6801b069e9f9a83da2caaedf7af4029ec674bbc2e4718e4217dfb6ee9382805'},
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
            # All rooms — return last cap messages total (not per room) to avoid overwhelming clients
            total_limit = cap  # cap is already set from client's limit param (default 200)
            c.execute('''SELECT message_id, sender, content, timestamp, message_type, room, display_name
                        FROM messages
                        WHERE room IS NOT NULL
                        ORDER BY timestamp DESC LIMIT ?''',
                     (total_limit,))
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


def _envd_url(sandbox_id: str) -> str:
    """Return the envd HTTP API base URL for a sandbox."""
    return f"https://49983-{sandbox_id}.e2b.app"


def _envd_read_file(sandbox_id: str, path: str, envd_access_token: str = '') -> bytes:
    """Read a file from the envd HTTP API, with E2B SDK fallback."""
    import urllib.request
    # Try envd first if we have a token
    if envd_access_token:
        try:
            url = f"{_envd_url(sandbox_id)}/files?path={urllib.parse.quote(path)}"
            headers = {'X-Access-Token': envd_access_token}
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.read()
        except Exception:
            pass  # Fall through to SDK
    # Fallback: use E2B SDK directly (always works with API key)
    if E2B_API_KEY:
        try:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable, '-c',
                 f'import os; os.environ["E2B_API_KEY"]="{E2B_API_KEY}"; '
                 f'from e2b import Sandbox; sbx=Sandbox.connect("{sandbox_id}"); '
                 f'import sys; sys.stdout.buffer.write(sbx.files.read("{path}").encode("utf-8", errors="replace"))'],
                capture_output=True, timeout=15
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception as e:
            logger.warning(f"E2B SDK read fallback failed: {e}")
    raise FileNotFoundError(f"Cannot read {path} from sandbox {sandbox_id}")


def _envd_list_dir(sandbox_id: str, directory: str, envd_access_token: str = '') -> list:
    """List files in a directory using envd gRPC HTTP API (connect protocol, fast)."""
    try:
        import urllib.request
        url = f"{_envd_url(sandbox_id)}/filesystem.Filesystem/ListDir"
        body = json.dumps({'path': directory}).encode()
        headers = {
            'Content-Type': 'application/json',
            'Connect-Protocol-Version': '1'
        }
        if envd_access_token:
            headers['X-Access-Token'] = envd_access_token
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = []
        for f in data.get('entries', []):
            result.append({
                'name': f.get('name', ''),
                'path': f.get('path', ''),
                'size': int(f.get('size', 0)),
                'type': 'FILE' if f.get('type') == 'FILE_TYPE_FILE' else 'DIR',
                'modified': f.get('modifiedTime', '')
            })
        return result
    except Exception as e:
        logger.error(f"envd list_dir error: {e}")
        return []


def _get_envd_token(agent_id: str) -> str:
    """Get the envd_access_token for an agent, if stored."""
    info = AGENT_SANDBOXES.get(agent_id, {})
    return info.get('envd_access_token', '')


def get_workspace_files(sandbox_id: str, directory: str = '/tmp/manus_assets', envd_access_token: str = '') -> list:
    """List files in an agent's E2B sandbox workspace.
    Supports both v3 (/home/ubuntu/workspace/docs/) and v2 (/tmp/manus_assets/) paths."""
    # If default directory requested, try v3 first then v2
    if directory == '/tmp/manus_assets':
        v3_files = []
        for docs_path in ['/home/ubuntu/workspace/docs', '/home/user/workspace/docs']:
            try:
                v3_files = _envd_list_dir(sandbox_id, docs_path, envd_access_token)
                if v3_files:
                    break
            except Exception:
                continue
        if v3_files:
            return v3_files
    return _envd_list_dir(sandbox_id, directory, envd_access_token)


def get_workspace_file_content(sandbox_id: str, path: str, envd_access_token: str = '') -> str:
    """Read a file from an agent's E2B sandbox via direct envd HTTP API."""
    try:
        return _envd_read_file(sandbox_id, path, envd_access_token).decode('utf-8', errors='replace')
    except Exception as e:
        return f"Error: {e}"


def get_workspace_terminal(sandbox_id: str, agent_id: str, lines: int = 50, envd_access_token: str = '') -> str:
    """Get the last N lines of an agent's terminal log via direct envd HTTP API.
    Supports both v2 (/tmp/agent_*.log) and v3 (/home/ubuntu/memory/agent.log) paths."""
    # Try v3 path first, then v2 fallback
    log_paths = [
        '/home/ubuntu/memory/agent.log',
        '/home/user/memory/agent.log',
        f'/tmp/agent_{agent_id}.log',
    ]
    for log_path in log_paths:
        try:
            content = _envd_read_file(sandbox_id, log_path, envd_access_token).decode('utf-8', errors='replace')
            log_lines = content.split('\n')
            return '\n'.join(log_lines[-lines:])
        except Exception:
            continue
    return 'No log available'


def get_workspace_screenshot(sandbox_id: str, envd_access_token: str = '') -> dict:
    """Get the browser screenshot (browser_view.png) or most recently modified file.
    Returns image as base64 if it's a PNG, otherwise returns text content."""
    import base64 as _b64
    try:
        directory = '/tmp/manus_assets'
        # First try to get browser_view.png specifically
        browser_view_path = '/tmp/manus_assets/browser_view.png'
        try:
            img_bytes = _envd_read_file(sandbox_id, browser_view_path, envd_access_token)
            return {
                "type": "image",
                "path": browser_view_path,
                "name": "browser_view.png",
                "content": _b64.b64encode(img_bytes).decode('ascii')
            }
        except Exception:
            pass  # Fall through to most-recent-file approach
        # Fallback: most recently modified file
        file_list = _envd_list_dir(sandbox_id, directory, envd_access_token)
        file_list = [f for f in file_list if f.get('type') == 'FILE']
        if not file_list:
            return {"type": "empty", "message": "No files in workspace"}
        latest = sorted(file_list, key=lambda f: f.get('modified', ''), reverse=True)[0]
        raw = _envd_read_file(sandbox_id, latest['path'], envd_access_token)
        if latest['name'].lower().endswith('.png'):
            return {
                "type": "image",
                "path": latest['path'],
                "name": latest['name'],
                "content": _b64.b64encode(raw).decode('ascii')
            }
        content = raw.decode('utf-8', errors='replace')
        return {
            "type": "file",
            "path": latest['path'],
            "name": latest['name'],
            "size": latest.get('size', 0),
            "modified": latest.get('modified', ''),
            "content": content[:8000]
        }
    except Exception as e:
        return {"type": "error", "message": str(e)}


def get_workspace_activity(sandbox_id: str, agent_id: str, envd_access_token: str = '') -> dict:
    """Parse the agent log to determine current activity mode and return relevant content.
    Modes: 'browse' (browser screenshot), 'edit' (file content), 'run' (terminal), 'think' (LLM), 'idle'"""
    import base64 as _b64, re as _re
    try:
        # Read last 60 lines of log — try v3 path first, then v2 fallback
        raw_log = ''
        for log_path in ['/home/ubuntu/memory/agent.log', '/home/user/memory/agent.log', f'/tmp/agent_{agent_id}.log']:
            try:
                raw_log = _envd_read_file(sandbox_id, log_path, envd_access_token).decode('utf-8', errors='replace')
                if raw_log.strip():
                    break
            except Exception:
                continue
        lines = [l for l in raw_log.split('\n') if l.strip()][-60:]
        last_lines = '\n'.join(lines)

        # Extract last few step lines for the step log
        step_lines = []
        # v3 keywords + v2 keywords for backward compatibility
        V3_KEYWORDS = ['[PLAN]', '[EXECUTE]', '[TOOL]', '[RESULT]', '[DONE]', '[THINK]', '[WRITE]', '[BROWSE]', '[SEARCH]', '[EXEC]', '[CHAT]', '[MEMORY]', '[ERROR]']
        V2_KEYWORDS = ['Ejecutando:', '[SENT', 'LLM [', '[RECONECT]', 'Escrito:', 'DOC SYNC', 'Follow-up', 'TRUNCADO', 'Conectando']
        for line in lines[-30:]:
            if any(kw in line for kw in V3_KEYWORDS + V2_KEYWORDS):
                # Clean timestamp
                clean = _re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]\d+ ', '', line).strip()
                clean = _re.sub(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*Z?\s*', '', clean).strip()
                # Format nicely for terminal display — v3 patterns
                if '[BROWSE]' in clean or 'Ejecutando: browse' in clean:
                    clean = '🌐 ' + clean
                elif '[SEARCH]' in clean or 'Ejecutando: search' in clean:
                    clean = '🔍 ' + clean
                elif '[WRITE]' in clean or 'Ejecutando: write' in clean or 'Escrito:' in clean:
                    clean = '✏️ ' + clean
                elif '[EXEC]' in clean or 'Ejecutando: python' in clean or 'Ejecutando: bash' in clean:
                    clean = '⚙️ ' + clean
                elif '[PLAN]' in clean:
                    clean = '📋 ' + clean
                elif '[EXECUTE]' in clean:
                    clean = '▶️ ' + clean
                elif '[TOOL]' in clean:
                    clean = '🔧 ' + clean
                elif '[RESULT]' in clean or '[DONE]' in clean:
                    clean = '✅ ' + clean
                elif '[THINK]' in clean or 'LLM [' in clean:
                    clean = '🧠 ' + clean
                elif '[CHAT]' in clean or '[SENT' in clean:
                    clean = '💬 ' + clean
                elif '[MEMORY]' in clean or 'DOC SYNC' in clean:
                    clean = '📄 ' + clean
                elif '[ERROR]' in clean:
                    clean = '❌ ' + clean
                elif '[RECONECT]' in clean:
                    clean = '🔄 ' + clean
                step_lines.append(clean)

        # Determine current mode from most recent action
        mode = 'idle'
        detail = ''
        current_url = ''
        current_file = ''

        # Scan lines in reverse to find the most recent action (v3 + v2 patterns)
        for line in reversed(lines):
            # v3 patterns
            if '[BROWSE]' in line or 'Ejecutando: browse' in line:
                mode = 'browse'
                url_match = _re.search(r'https?://[^\s]+', line)
                if url_match:
                    current_url = url_match.group(0)
                break
            elif '[SEARCH]' in line or 'Ejecutando: search' in line:
                mode = 'search'
                q_match = _re.search(r'(?:SEARCH|search)[:\s]+["\']?([^"\']\n]{3,80})', line, _re.IGNORECASE)
                if q_match:
                    detail = q_match.group(1).strip()
                break
            elif '[WRITE]' in line or 'Ejecutando: write_file' in line or 'Ejecutando: read_file' in line or 'Ejecutando: append_file' in line:
                mode = 'edit'
                path_match = _re.search(r'[/\w.-]+\.[a-zA-Z]+', line)
                if path_match:
                    current_file = path_match.group(0)
                break
            elif '[EXEC]' in line or 'Ejecutando: python' in line or 'Ejecutando: bash' in line or 'Ejecutando: shell' in line:
                mode = 'run'
                break
            elif '[PLAN]' in line or '[EXECUTE]' in line:
                mode = 'think'
                break
            elif '[THINK]' in line or 'LLM [REACT]' in line or 'LLM [CHAT]' in line or 'LLM [PRECISE]' in line:
                mode = 'think'
                break
            elif '[CHAT]' in line or '[SENT' in line:
                mode = 'think'
                break

        result = {
            'mode': mode,
            'step_log': step_lines[-15:],
            'current_url': current_url,
            'current_file': current_file,
            'terminal_output': '\n'.join(step_lines[-20:]),  # Always provide terminal output from step log
        }

        # If browse mode, try to get browser screenshot
        if mode == 'browse':
            try:
                img_bytes = _envd_read_file(sandbox_id, '/tmp/manus_assets/browser_view.png', envd_access_token)
                result['screenshot'] = _b64.b64encode(img_bytes).decode('ascii')
            except Exception:
                pass
        # If search mode, extract recent search queries and results from log
        if mode == 'search':
            search_entries = []
            current_query = None
            for line in lines:
                # Detect search execution line
                if 'Ejecutando: search' in line:
                    q_match = _re.search(r'search[:\s]+["\']?([^"\'\n]{3,80})', line, _re.IGNORECASE)
                    current_query = q_match.group(1).strip() if q_match else 'Searching...'
                # Detect Follow-up search line (contains the query)
                elif 'Follow-up search:' in line:
                    q_match = _re.search(r'Follow-up search:\s*[•\-]?\s*(.+)', line)
                    if q_match:
                        current_query = q_match.group(1).strip()[:80]
                        search_entries.append({'query': current_query, 'result': ''})
                # Detect search result lines (SENT messages after a search)
                elif '[SENT' in line and current_query and search_entries:
                    result_match = _re.search(r'\[SENT[^\]]+\]\s*(.+)', line)
                    if result_match and not search_entries[-1]['result']:
                        search_entries[-1]['result'] = result_match.group(1).strip()[:200]
            result['search_entries'] = search_entries[-5:] if search_entries else []
            result['search_query'] = detail or (search_entries[-1]['query'] if search_entries else 'Searching...')

        # If edit mode, try to get the file content
        if mode == 'edit' and current_file:
            try:
                content = _envd_read_file(sandbox_id, current_file, envd_access_token).decode('utf-8', errors='replace')
                result['file_content'] = content[:8000]
                result['file_name'] = current_file.split('/')[-1]
            except Exception:
                pass

        # Always try to get the most recently modified file as fallback for edit mode
        # Check v3 path (/home/ubuntu/workspace/docs/) first, then v2 (/tmp/manus_assets/)
        if mode in ('edit', 'idle') and not result.get('file_content'):
            for workspace_dir in ['/home/ubuntu/workspace/docs', '/home/user/workspace/docs', '/tmp/manus_assets']:
                try:
                    file_list = _envd_list_dir(sandbox_id, workspace_dir, envd_access_token)
                    file_list = [f for f in file_list if f.get('type') == 'FILE' and not f['name'].endswith('.png')]
                    if file_list:
                        latest = sorted(file_list, key=lambda f: f.get('modified', ''), reverse=True)[0]
                        content = _envd_read_file(sandbox_id, latest['path'], envd_access_token).decode('utf-8', errors='replace')
                        result['file_content'] = content[:8000]
                        result['file_name'] = latest['name']
                        result['file_path'] = latest['path']
                        if mode == 'idle':
                            result['mode'] = 'edit'
                        break
                except Exception:
                    continue
        # Also try to read todo.md for v3 agents (shows current plan)
        if not result.get('todo_content'):
            try:
                todo_raw = None
                for todo_path in ['/home/ubuntu/workspace/todo.md', '/home/user/workspace/todo.md']:
                    try:
                        todo_raw = _envd_read_file(sandbox_id, todo_path, envd_access_token).decode('utf-8', errors='replace')
                        break
                    except Exception:
                        continue
                if todo_raw is None:
                    raise Exception('todo.md not found')
                result['todo_content'] = todo_raw[:3000]
            except Exception:
                pass
        # Try to read status.json for v3 agents
        if not result.get('agent_status'):
            try:
                status_raw = None
                for status_path in ['/home/ubuntu/memory/status.json', '/home/user/memory/status.json']:
                    try:
                        status_raw = _envd_read_file(sandbox_id, status_path, envd_access_token).decode('utf-8', errors='replace')
                        break
                    except Exception:
                        continue
                if status_raw is None:
                    raise Exception('status.json not found')
                result['agent_status'] = json.loads(status_raw)
            except Exception:
                pass

        return result
    except Exception as e:
        return {'mode': 'idle', 'step_log': [], 'error': str(e)}


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
    if path.startswith("/workspace/") and "/file" not in path and "/terminal" not in path and "/register" not in path and "/screenshot" not in path and "/activity" not in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        agent_id = parts[0].replace("/workspace/", "").strip("/")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        directory = params.get('dir', ['/tmp/manus_assets'])[0]
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        envd_token = _get_envd_token(agent_id)
        files = get_workspace_files(sandbox_id, directory, envd_token)
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
        envd_token = _get_envd_token(agent_id)
        content = get_workspace_file_content(sandbox_id, file_path, envd_token)
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
        envd_token = _get_envd_token(agent_id)
        terminal_output = get_workspace_terminal(sandbox_id, agent_id, lines, envd_token)
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
        envd_token = _get_envd_token(agent_id)
        result = get_workspace_screenshot(sandbox_id, envd_token)
        body = json.dumps({
            "agent_id": agent_id,
            "display_name": AGENT_SANDBOXES[agent_id]['display_name'],
            **result
        }).encode()
        return http.HTTPStatus.OK, cors, body
    # Workspace: activity — current mode + content (auto-switching view)
    # GET /workspace/{agent_id}/activity
    if path.startswith("/workspace/") and "/activity" in path:
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        agent_id = path.replace("/workspace/", "").replace("/activity", "").strip("/").split("?")[0]
        if agent_id not in AGENT_SANDBOXES:
            return http.HTTPStatus.NOT_FOUND, cors, json.dumps({"error": f"Unknown agent: {agent_id}"}).encode()
        sandbox_id = AGENT_SANDBOXES[agent_id]['sandbox_id']
        envd_token = _get_envd_token(agent_id)
        result = get_workspace_activity(sandbox_id, agent_id, envd_token)
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
        envd_access_token = params.get('envd_access_token', [''])[0]
        AGENT_SANDBOXES[agent_id] = {
            'sandbox_id': sandbox_id,
            'display_name': display_name,
            'envd_access_token': envd_access_token
        }
        logger.info(f"📦 Workspace registered: {agent_id} -> {sandbox_id} (token={'yes' if envd_access_token else 'no'})")
        body = json.dumps({"ok": True, "agent_id": agent_id, "sandbox_id": sandbox_id}).encode()
        return http.HTTPStatus.OK, cors, body

    # Documents: list all documents created by agents from the Memory Server
    # GET /documents?filter=mettis
    if path.startswith("/documents"):
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        parts = path.split("?")
        params = urllib.parse.parse_qs(parts[1]) if len(parts) > 1 else {}
        filter_str = params.get('filter', [''])[0].lower()
        agent_filter = params.get('agent', [''])[0]  # e.g. ?agent=manus_agent_201
        try:
            import urllib.request as _ureq
            mem_url = os.environ.get('MEMORY_SERVER_URL', 'https://memory-server-production-647a.up.railway.app')
            req = _ureq.Request(f"{mem_url}/memory", headers={"Accept": "application/json"})
            with _ureq.urlopen(req, timeout=8) as r:
                all_mem = json.loads(r.read().decode())
            agent_display = {aid: info['display_name'] for aid, info in AGENT_SANDBOXES.items()}
            docs = []
            for key, val in all_mem.items():
                if not isinstance(val, dict):
                    continue
                # Only show intentional documents: keys starting with 'doc:'
                if not key.startswith('doc:'):
                    continue
                value = val.get('value', '')
                agent = val.get('agent', '?')
                # Filter by agent if specified
                if agent_filter and agent != agent_filter:
                    continue
                # Support both 'ts' (ISO string) and 'updated_at' (float epoch)
                ts = val.get('ts', '')
                if not ts:
                    updated_at = val.get('updated_at', 0)
                    if updated_at:
                        from datetime import datetime, timezone
                        ts = datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat()
                if not value or len(value) < 10:
                    continue
                if filter_str and filter_str not in key.lower() and filter_str not in value.lower()[:500]:
                    continue
                display = agent_display.get(agent, agent)
                # Derive a friendly title from the key (strip 'doc:' prefix)
                title = key[4:].replace('_', ' ').replace('-', ' ').title()
                doc_type = 'markdown' if (value.strip().startswith('#') or key.endswith('.md')) else ('json' if value.strip().startswith('{') else 'text')
                docs.append({
                    'key': key,
                    'title': title,
                    'agent': agent,
                    'display_name': display,
                    'ts': ts,
                    'type': doc_type,
                    'preview': value[:300],
                    'size': len(value)
                })
            docs.sort(key=lambda x: x.get('ts', ''), reverse=True)
            body = json.dumps({'total': len(docs), 'documents': docs[:100]}).encode()
            return http.HTTPStatus.OK, cors, body
        except Exception as e:
            body = json.dumps({'error': str(e), 'documents': []}).encode()
            return http.HTTPStatus.OK, cors, body

    # Documents: get full content of a specific document
    # GET /document/{key}
    if path.startswith("/document/"):
        cors = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
        key = urllib.parse.unquote(path.replace("/document/", "").split("?")[0])
        try:
            import urllib.request as _ureq
            mem_url = os.environ.get('MEMORY_SERVER_URL', 'https://memory-server-production-647a.up.railway.app')
            req = _ureq.Request(f"{mem_url}/memory/{urllib.parse.quote(key)}", headers={"Accept": "application/json"})
            with _ureq.urlopen(req, timeout=8) as r:
                item = json.loads(r.read().decode())
            value = item.get('value', '') if isinstance(item, dict) else str(item)
            agent = item.get('agent', '?') if isinstance(item, dict) else '?'
            agent_display = {aid: info['display_name'] for aid, info in AGENT_SANDBOXES.items()}
            display = agent_display.get(agent, agent)
            body = json.dumps({'key': key, 'agent': agent, 'display_name': display, 'ts': item.get('ts','') if isinstance(item,dict) else '', 'content': value}).encode()
            return http.HTTPStatus.OK, cors, body
        except Exception as e:
            body = json.dumps({'error': str(e)}).encode()
            return http.HTTPStatus.NOT_FOUND, cors, body

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
    for client_id, client_info in list(clients.items()):  # list() snapshot to avoid dict-changed-size error
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

        # Auto-register workspace credentials from HELLO message
        sandbox_id = hello_msg.get("sandbox_id", "")
        envd_access_token = hello_msg.get("envd_access_token", "")
        display_name = hello_msg.get("display_name", client_id)
        if sandbox_id:
            existing = AGENT_SANDBOXES.get(client_id, {})
            # If no token provided, try to fetch from E2B SDK (async, non-blocking)
            if not envd_access_token and E2B_API_KEY:
                try:
                    import subprocess as _sp, sys as _sys
                    _result = _sp.run(
                        [_sys.executable, '-c',
                         f'import os; os.environ["E2B_API_KEY"]="{E2B_API_KEY}"; '
                         f'from e2b import Sandbox; sbx=Sandbox.connect("{sandbox_id}"); '
                         f'token=getattr(sbx,"_envd_access_token",None) or getattr(sbx,"_SandboxBase__envd_access_token",None) or ""; '
                         f'print(token)'],
                        capture_output=True, text=True, timeout=15
                    )
                    envd_access_token = _result.stdout.strip()
                    if envd_access_token:
                        logger.info(f"🔑 Fetched envd token for {client_id} via SDK")
                except Exception as _e:
                    logger.warning(f"Could not fetch envd token for {client_id}: {_e}")
            AGENT_SANDBOXES[client_id] = {
                'sandbox_id': sandbox_id,
                'display_name': display_name,
                'envd_access_token': envd_access_token or existing.get('envd_access_token', '')
            }
            logger.info(f"📦 Auto-registered workspace for {client_id} -> {sandbox_id} (token={'yes' if envd_access_token else 'NO'})")

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

                elif msg_type in ("TERMINAL", "BROWSER_STREAM", "STATUS"):
                    # Real-time streaming from E2B sandboxes — broadcast but do NOT persist
                    await broadcast_message(message, client_id)
                    if msg_type == "TERMINAL":
                        output = message.get("data", {}).get("output", "")[:80]
                        logger.debug(f"📟 TERMINAL from {client_id}: {output}")
                    elif msg_type == "BROWSER_STREAM":
                        logger.info(f"🌐 BROWSER_STREAM from {client_id}")

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
            # Auto-respawn Mettis agents after disconnect
            if client_id in AUTO_RESPAWN_AGENTS:
                asyncio.create_task(_respawn_agent(client_id))


async def _respawn_agent(agent_id: str):
    """Relaunch an E2B sandbox agent after it disconnects. Waits 10s first."""
    global _respawn_lock
    import time
    await asyncio.sleep(10)  # Brief pause before respawn

    # Throttle: don't respawn more than once per 30s per agent
    now = time.time()
    last = _last_respawn.get(agent_id, 0)
    if now - last < 30:
        logger.info(f"⏭️  Skipping respawn for {agent_id} (too soon, {int(now-last)}s ago)")
        return

    # If agent reconnected on its own, skip
    if agent_id in clients:
        logger.info(f"✅ {agent_id} already reconnected — skipping respawn")
        return

    info = AGENT_SANDBOXES.get(agent_id)
    if not info:
        logger.warning(f"⚠️  No sandbox info for {agent_id} — cannot respawn")
        return

    sandbox_id = info.get('sandbox_id', '')
    display_name = info.get('display_name', agent_id)
    if not sandbox_id:
        logger.warning(f"⚠️  No sandbox_id for {agent_id} — cannot respawn")
        return

    if not E2B_API_KEY:
        logger.warning(f"⚠️  E2B_API_KEY not set — cannot respawn {agent_id}")
        return

    logger.info(f"🔄 Auto-respawning {display_name} ({agent_id}) in sandbox {sandbox_id}...")
    _last_respawn[agent_id] = now

    try:
        import subprocess, sys, tempfile, os

        # Read the latest agent code from this relay's filesystem
        agent_code_path = '/app/manus_agent_v3.py'
        if not os.path.exists(agent_code_path):
            agent_code_path = os.path.join(os.path.dirname(__file__), 'manus_agent_v3.py')
        if not os.path.exists(agent_code_path):
            logger.warning(f"⚠️  Agent code not found at {agent_code_path}")
            return

        with open(agent_code_path, 'r') as f:
            agent_code = f.read()

        # Get the soul prompt for this agent
        soul = AGENT_SOULS.get(agent_id, '')

        # Build the start script
        soul_escaped = soul.replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
        start_sh = f"""#!/bin/bash
export OPENAI_API_KEY="{AGENT_OPENAI_API_KEY}"
export OPENAI_BASE_URL="{AGENT_OPENAI_BASE_URL}"
export E2B_API_KEY="{E2B_API_KEY}"
export MEMORY_SERVER_URL="{AGENT_MEMORY_URL}"
export BROWSER_SERVICE_URL="{AGENT_BROWSER_URL}"
export BROWSER_SERVICE_API_KEY="{AGENT_BROWSER_KEY}"
export TAVILY_API_KEY="{AGENT_TAVILY_KEY}"
export AGENT_DISPLAY_NAME="{display_name}"
export RELAY_URL="{AGENT_RELAY_URL}"
export PYTHONUNBUFFERED=1
export AGENT_SOUL="{soul_escaped}"
pip install tavily-python -q 2>/dev/null || true
cd /home/user/agent
pkill -f "manus_agent_v3.py {agent_id}" 2>/dev/null; sleep 1
nohup python3 manus_agent_v3.py {agent_id} > /tmp/agent_{agent_id}.log 2>&1 &
echo "PID: $!"
"""
        # Run the respawn via subprocess (E2B SDK)
        respawn_script = f"""
import os
os.environ['E2B_API_KEY'] = '{E2B_API_KEY}'
from e2b import Sandbox
sbx = Sandbox.connect('{sandbox_id}')
agent_code = open('/app/manus_agent_v3.py').read()
sbx.files.write('/home/user/agent/manus_agent_v3.py', agent_code)
sbx.files.write('/home/user/start_agent.sh', {repr(start_sh)})
sbx.commands.run('chmod +x /home/user/start_agent.sh', timeout=5)
r = sbx.commands.run('bash /home/user/start_agent.sh', timeout=15)
print(r.stdout.strip())
"""
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, '-c', respawn_script],
                capture_output=True, text=True, timeout=60
            )
        )
        if result.returncode == 0:
            logger.info(f"✅ Respawned {display_name}: {result.stdout.strip()[:100]}")
        else:
            logger.error(f"❌ Respawn failed for {display_name}: {result.stderr.strip()[:200]}")
    except Exception as e:
        logger.error(f"❌ Respawn error for {display_name}: {e}")


# Soul prompts for each Mettis agent (used during respawn)
METTIS_MISSION = """MISIÓN ACTUAL — METTIS AI SALES TEAM:
Sois un equipo de agentes especializados contratados para crear el plan de ventas y pipeline comercial completo para Mettis AI.
Mettis AI es una empresa de inteligencia artificial que desarrolla soluciones de agentes autónomos para empresas.
SALA DE TRABAJO: #mettis-ai (cambiaros a esa sala inmediatamente al conectaros usando ##MANUS_ROOM: mettis-ai##)
Trabajad de forma autónoma y colaborativa. Investigad, escribid documentos, compartid hallazgos."""

AGENT_SOULS = {
    'manus_agent_200': f"""Eres ATLAS, Sales Director del equipo Mettis AI. Lideras la estrategia comercial, coordinas al equipo y aseguras que todos los entregables se completen. Personalidad: directivo, orientado a resultados, pragmático. {METTIS_MISSION}""",
    'manus_agent_201': f"""Eres VEGA, Sales Strategist del equipo Mettis AI. Defines el ICP, los segmentos objetivo y la estrategia de go-to-market. Personalidad: analítica, estratégica, orientada a datos. {METTIS_MISSION}""",
    'manus_agent_202': f"""Eres ORION, Market Intelligence del equipo Mettis AI. Investigas el mercado, la competencia y las tendencias. Personalidad: curioso, metódico, orientado a la investigación. {METTIS_MISSION}""",
    'manus_agent_203': f"""Eres NEXUS, Revenue Operations del equipo Mettis AI. Diseñas el pipeline de ventas, los procesos y las métricas. Personalidad: sistemático, orientado a procesos, analítico. {METTIS_MISSION}""",
    'manus_agent_204': f"""Eres LYRIC, Copywriter & Storyteller del equipo Mettis AI. Creas los mensajes de ventas, email sequences y el pitch deck. Personalidad: creativa, persuasiva, orientada a la narrativa. {METTIS_MISSION}""",
}


def _fetch_envd_tokens_background():
    """Fetch envd_access_token for all known sandboxes from E2B API at startup.
    Runs in a background thread so it doesn't block the server from starting."""
    if not E2B_API_KEY:
        logger.warning("⚠️  E2B_API_KEY not set — workspace tokens will not be auto-fetched")
        return
    import time
    time.sleep(3)  # Wait for server to fully start
    logger.info("🔑 Auto-fetching envd tokens for all agents...")
    try:
        import subprocess, sys
        for agent_id, info in list(AGENT_SANDBOXES.items()):
            sandbox_id = info.get('sandbox_id', '')
            if not sandbox_id or info.get('envd_access_token'):
                continue  # Skip if already has token
            try:
                result = subprocess.run(
                    [sys.executable, '-c',
                     f'import os; os.environ["E2B_API_KEY"]="{E2B_API_KEY}"; '
                     f'from e2b import Sandbox; sbx=Sandbox.connect("{sandbox_id}"); '
                     f'print(sbx._SandboxBase__envd_access_token)'],
                    capture_output=True, text=True, timeout=20
                )
                token = result.stdout.strip()
                if token and len(token) == 64:
                    AGENT_SANDBOXES[agent_id]['envd_access_token'] = token
                    logger.info(f"  ✅ {info.get('display_name', agent_id)}: token fetched")
                else:
                    logger.warning(f"  ⚠️  {info.get('display_name', agent_id)}: bad token '{token[:20]}...'")
            except Exception as e:
                logger.warning(f"  ❌ {info.get('display_name', agent_id)}: {e}")
    except Exception as e:
        logger.error(f"Token auto-fetch failed: {e}")
    logger.info("🔑 Token auto-fetch complete")

async def main():
    global _respawn_lock
    _respawn_lock = asyncio.Lock()
    logger.info("=" * 60)
    logger.info("🚀 Multi-Agent Relay Server v1.0 (Auto-Respawn)")
    logger.info("=" * 60)
    init_database()
    # Start background token fetch (non-blocking)
    import threading
    threading.Thread(target=_fetch_envd_tokens_background, daemon=True).start()
    # Log auto-respawn config
    logger.info(f"🔄 Auto-respawn enabled for: {sorted(AUTO_RESPAWN_AGENTS)}")

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)

    async with websockets.serve(
        handle_client,
        host="",
        port=8080,
        process_request=health_check,
        max_size=20 * 1024 * 1024,  # 20MB max message size
        ping_interval=30,   # Send ping every 30s
        ping_timeout=120,   # Allow 120s for pong (agents busy browsing)
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
