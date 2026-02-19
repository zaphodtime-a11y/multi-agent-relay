#!/usr/bin/env python3
"""
Multi-Agent Relay Server v0.3 - Production Ready for Fly.io
Features:
- SQLite persistence
- History retrieval
- Message queue for offline agents
- Health check endpoint
- Graceful shutdown on SIGTERM
"""

import asyncio
import websockets
import json
import logging
import signal
import http
import sqlite3
from datetime import datetime
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

# Database setup
DB_PATH = "/data/relay_server.db"

def init_database():
    """Initialize SQLite database"""
    # Ensure data directory exists
    Path("/data").mkdir(exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Messages table
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  message_id TEXT UNIQUE,
                  sender TEXT NOT NULL,
                  content TEXT NOT NULL,
                  timestamp TEXT NOT NULL,
                  message_type TEXT DEFAULT 'MESSAGE')''')
    
    # Presence table
    c.execute('''CREATE TABLE IF NOT EXISTS presence
                 (agent_id TEXT PRIMARY KEY,
                  last_seen TEXT NOT NULL,
                  status TEXT DEFAULT 'online')''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized")

def store_message(message_id, sender, content, timestamp, message_type='MESSAGE'):
    """Store message in database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO messages 
                    (message_id, sender, content, timestamp, message_type)
                    VALUES (?, ?, ?, ?, ?)''',
                 (message_id, sender, content, timestamp, message_type))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to store message: {e}")

def get_message_history(since_timestamp=None):
    """Retrieve message history"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        if since_timestamp:
            c.execute('''SELECT message_id, sender, content, timestamp, message_type
                        FROM messages 
                        WHERE timestamp > ? 
                        ORDER BY timestamp ASC''', (since_timestamp,))
        else:
            c.execute('''SELECT message_id, sender, content, timestamp, message_type
                        FROM messages 
                        ORDER BY timestamp ASC 
                        LIMIT 100''')
        
        messages = []
        for row in c.fetchall():
            messages.append({
                "message_id": row[0],
                "sender": row[1],
                "content": row[2],
                "timestamp": row[3],
                "message_type": row[4]
            })
        
        conn.close()
        return messages
    except Exception as e:
        logger.error(f"Failed to retrieve history: {e}")
        return []

def health_check(path, request_headers):
    """Health check endpoint for Railway/Fly.io/Render"""
    if path == "/healthz":
        return http.HTTPStatus.OK, {}, b"OK\n"

async def send_error(websocket, error_code, error_message, recoverable=True):
    """Send ERROR message to client"""
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
        logger.info(f"✅ Sent ERROR: {error_code}")
    except Exception as e:
        logger.error(f"❌ Failed to send ERROR: {e}")

async def broadcast_message(message, sender_id):
    """Broadcast message to all connected clients except sender"""
    disconnected = []
    delivery_failures = []
    
    for client_id, client_info in clients.items():
        if client_id != sender_id:
            try:
                await client_info["websocket"].send(json.dumps(message))
                logger.info(f"✅ Forwarded to {client_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send to {client_id}: {e}")
                disconnected.append(client_id)
                delivery_failures.append(client_id)
                message_queue[client_id].append(message)
    
    for client_id in disconnected:
        if client_id in clients:
            del clients[client_id]
    
    return delivery_failures

async def send_queued_messages(websocket, client_id):
    """Send all queued messages to reconnected client"""
    if client_id in message_queue and message_queue[client_id]:
        logger.info(f"📤 Sending {len(message_queue[client_id])} queued messages")
        
        for queued_msg in message_queue[client_id]:
            try:
                await websocket.send(json.dumps(queued_msg))
            except Exception as e:
                logger.error(f"❌ Failed to send queued message: {e}")
                break
        
        message_queue[client_id] = []

async def handle_client(websocket):
    """Handle incoming WebSocket connections"""
    client_id = None
    
    try:
        # Wait for HELLO message
        hello_raw = await websocket.recv()
        hello_msg = json.loads(hello_raw)
        
        if hello_msg.get("message_type") != "HELLO":
            await send_error(websocket, "INVALID_HANDSHAKE", 
                           "Expected HELLO", recoverable=False)
            await websocket.close()
            return
        
        client_id = hello_msg.get("sender", "unknown")
        logger.info(f"✅ HELLO from {client_id}")
        
        # Store client
        clients[client_id] = {
            "websocket": websocket,
            "capabilities": hello_msg.get("capabilities", {}),
            "connected_at": datetime.now().isoformat()
        }
        
        # Send WELCOME
        welcome_msg = {
            "protocol_version": "0.3",
            "message_type": "WELCOME",
            "session_id": f"session-{client_id}",
            "server_capabilities": {
                "relay": True,
                "persistence": True,
                "history": True,
                "message_queue": True
            },
            "heartbeat_interval": 30,
            "connected_agents": len(clients)
        }
        await websocket.send(json.dumps(welcome_msg))
        
        # Send queued messages
        await send_queued_messages(websocket, client_id)
        
        # Announce new agent
        announcement = {
            "protocol_version": "0.3",
            "message_type": "MESSAGE",
            "message_id": f"announce-{client_id}-{int(datetime.now().timestamp())}",
            "sender": "relay_server",
            "content": f"🎉 {client_id} joined! Total: {len(clients)}",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await broadcast_message(announcement, client_id)
        
        # Main message loop
        async for message_raw in websocket:
            try:
                message = json.loads(message_raw)
                msg_type = message.get("message_type")
                
                if msg_type == "MESSAGE":
                    # Store in database
                    store_message(
                        message.get("message_id"),
                        message.get("sender"),
                        message.get("content", ""),
                        message.get("timestamp"),
                        "MESSAGE"
                    )
                    
                    # Send ACK
                    ack = {
                        "protocol_version": "0.3",
                        "message_type": "ACK",
                        "message_id": message.get("message_id"),
                        "timestamp": datetime.utcnow().isoformat() + "Z"
                    }
                    await websocket.send(json.dumps(ack))
                    
                    # Broadcast
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

async def main():
    """Start the WebSocket server with graceful shutdown"""
    logger.info("=" * 60)
    logger.info("🚀 Multi-Agent Relay Server v0.3 (Production)")
    logger.info("=" * 60)
    
    # Initialize database
    init_database()
    
    # Set up graceful shutdown
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
    
    # Start server
    async with websockets.serve(
        handle_client,
        host="",
        port=8080,
        process_request=health_check
    ):
        logger.info("✅ Server running on port 8080")
        logger.info("✅ Health check at /healthz")
        logger.info("=" * 60)
        await stop
        logger.info("🛑 Shutting down gracefully...")

if __name__ == "__main__":
    asyncio.run(main())
