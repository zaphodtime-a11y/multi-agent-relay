#!/usr/bin/env python3
"""
Multi-Agent Relay Server v0.4 - With File Sharing
Features:
- SQLite persistence
- History retrieval
- Message queue for offline agents
- Health check endpoint
- FILE SHARING: Upload/download files
- Graceful shutdown on SIGTERM
"""

import asyncio
import websockets
import json
import logging
import signal
import http
import sqlite3
import uuid
import os
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MultiAgentRelay")

# Store connected clients
clients = {}

# Message queue for offline/disconnected agents
message_queue = defaultdict(list)

# Database and file storage paths
DB_PATH = "/data/relay_server.db"
FILES_DIR = "/data/files"

def init_database():
    """Initialize SQLite database"""
    # Ensure data directory exists
    Path("/data").mkdir(exist_ok=True)
    Path(FILES_DIR).mkdir(exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Messages table
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  message_id TEXT UNIQUE,
                  sender TEXT NOT NULL,
                  recipient TEXT,
                  content TEXT NOT NULL,
                  timestamp TEXT NOT NULL)''')
    
    # Files table
    c.execute('''CREATE TABLE IF NOT EXISTS files
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  file_id TEXT UNIQUE NOT NULL,
                  file_name TEXT NOT NULL,
                  file_size INTEGER NOT NULL,
                  mime_type TEXT,
                  sender TEXT NOT NULL,
                  recipient TEXT,
                  file_path TEXT NOT NULL,
                  timestamp TEXT NOT NULL)''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def store_message(message_id, sender, recipient, content):
    """Store message in database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    timestamp = datetime.utcnow().isoformat() + "Z"
    
    try:
        c.execute('''INSERT INTO messages (message_id, sender, recipient, content, timestamp)
                     VALUES (?, ?, ?, ?, ?)''',
                  (message_id, sender, recipient, content, timestamp))
        conn.commit()
    except sqlite3.IntegrityError:
        logger.warning(f"Duplicate message_id: {message_id}")
    finally:
        conn.close()

def store_file_metadata(file_id, file_name, file_size, mime_type, sender, recipient, file_path):
    """Store file metadata in database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    timestamp = datetime.utcnow().isoformat() + "Z"
    
    try:
        c.execute('''INSERT INTO files (file_id, file_name, file_size, mime_type, sender, recipient, file_path, timestamp)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (file_id, file_name, file_size, mime_type, sender, recipient, file_path, timestamp))
        conn.commit()
        logger.info(f"File metadata stored: {file_id} - {file_name}")
    except sqlite3.IntegrityError:
        logger.warning(f"Duplicate file_id: {file_id}")
    finally:
        conn.close()

def get_file_metadata(file_id):
    """Retrieve file metadata from database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT file_name, file_size, mime_type, file_path FROM files WHERE file_id = ?', (file_id,))
    result = c.fetchone()
    conn.close()
    return result

def get_history(agent_id, since=None):
    """Retrieve message history for an agent"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    if since:
        c.execute('''SELECT sender, recipient, content, timestamp 
                     FROM messages 
                     WHERE (recipient = ? OR recipient IS NULL) AND timestamp > ?
                     ORDER BY timestamp ASC''', (agent_id, since))
    else:
        c.execute('''SELECT sender, recipient, content, timestamp 
                     FROM messages 
                     WHERE recipient = ? OR recipient IS NULL
                     ORDER BY timestamp ASC''', (agent_id,))
    
    messages = c.fetchall()
    conn.close()
    return messages

async def handle_client(websocket, path):
    """Handle WebSocket client connection"""
    agent_id = None
    
    try:
        # Wait for HELLO message
        hello_msg = await websocket.recv()
        hello_data = json.loads(hello_msg)
        
        if hello_data.get("message_type") != "HELLO":
            await websocket.close(1002, "Expected HELLO message")
            return
        
        agent_id = hello_data.get("agent_id")
        if not agent_id:
            await websocket.close(1002, "Missing agent_id")
            return
        
        # Register client
        clients[agent_id] = websocket
        logger.info(f"Agent {agent_id} connected. Total clients: {len(clients)}")
        
        # Send WELCOME
        welcome = {
            "message_type": "WELCOME",
            "server_version": "0.4",
            "features": ["persistence", "history", "file_sharing"],
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        await websocket.send(json.dumps(welcome))
        
        # Send queued messages
        if agent_id in message_queue:
            for queued_msg in message_queue[agent_id]:
                await websocket.send(json.dumps(queued_msg))
            message_queue[agent_id].clear()
        
        # Broadcast join
        join_msg = {
            "message_type": "MESSAGE",
            "sender": "relay_server",
            "content": f"🎉 {agent_id} joined! Total: {len(clients)}",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        for client_ws in clients.values():
            try:
                await client_ws.send(json.dumps(join_msg))
            except:
                pass
        
        # Handle messages
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("message_type")
            
            if msg_type == "MESSAGE":
                # Forward message
                recipient = data.get("recipient")
                content = data.get("content", "")
                message_id = data.get("message_id", str(uuid.uuid4()))
                
                # Store in database
                store_message(message_id, agent_id, recipient, content)
                
                # Forward to recipient or broadcast
                forward_msg = {
                    "message_type": "MESSAGE",
                    "message_id": message_id,
                    "sender": agent_id,
                    "content": content,
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
                
                if recipient and recipient in clients:
                    await clients[recipient].send(json.dumps(forward_msg))
                elif recipient:
                    # Queue for offline recipient
                    message_queue[recipient].append(forward_msg)
                else:
                    # Broadcast to all except sender
                    for cid, cws in clients.items():
                        if cid != agent_id:
                            try:
                                await cws.send(json.dumps(forward_msg))
                            except:
                                pass
                
                # Send ACK
                ack = {
                    "message_type": "ACK",
                    "message_id": message_id,
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
                await websocket.send(json.dumps(ack))
            
            elif msg_type == "HISTORY_REQUEST":
                since = data.get("since")
                history = get_history(agent_id, since)
                
                history_msg = {
                    "message_type": "HISTORY_RESPONSE",
                    "messages": [
                        {
                            "sender": h[0],
                            "recipient": h[1],
                            "content": h[2],
                            "timestamp": h[3]
                        } for h in history
                    ],
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
                await websocket.send(json.dumps(history_msg))
            
            elif msg_type == "PING":
                pong = {
                    "message_type": "PONG",
                    "timestamp": datetime.utcnow().isoformat() + "Z"
                }
                await websocket.send(json.dumps(pong))
    
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Agent {agent_id} disconnected")
    except Exception as e:
        logger.error(f"Error handling client {agent_id}: {e}")
    finally:
        if agent_id and agent_id in clients:
            del clients[agent_id]
            logger.info(f"Agent {agent_id} removed. Remaining: {len(clients)}")

# HTTP handlers for file operations
async def handle_upload(request):
    """Handle file upload"""
    try:
        reader = await request.multipart()
        
        file_name = None
        file_data = None
        sender = None
        recipient = None
        
        async for part in reader:
            if part.name == 'file':
                file_name = part.filename
                file_data = await part.read()
            elif part.name == 'sender':
                sender = await part.text()
            elif part.name == 'recipient':
                recipient = await part.text()
        
        if not file_name or not file_data or not sender:
            return web.json_response({"error": "Missing required fields"}, status=400)
        
        # Generate file ID and save file
        file_id = str(uuid.uuid4())
        file_path = os.path.join(FILES_DIR, file_id)
        
        with open(file_path, 'wb') as f:
            f.write(file_data)
        
        file_size = len(file_data)
        mime_type = request.content_type or "application/octet-stream"
        
        # Store metadata
        store_file_metadata(file_id, file_name, file_size, mime_type, sender, recipient, file_path)
        
        # Send FILE_TRANSFER message to recipient
        if recipient and recipient in clients:
            transfer_msg = {
                "message_type": "FILE_TRANSFER",
                "file_id": file_id,
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "sender": sender,
                "recipient": recipient,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            }
            try:
                await clients[recipient].send(json.dumps(transfer_msg))
            except:
                message_queue[recipient].append(transfer_msg)
        
        logger.info(f"File uploaded: {file_id} - {file_name} ({file_size} bytes)")
        
        return web.json_response({
            "file_id": file_id,
            "file_name": file_name,
            "file_size": file_size,
            "status": "uploaded"
        })
    
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_download(request):
    """Handle file download"""
    try:
        file_id = request.match_info['file_id']
        
        metadata = get_file_metadata(file_id)
        if not metadata:
            return web.json_response({"error": "File not found"}, status=404)
        
        file_name, file_size, mime_type, file_path = metadata
        
        if not os.path.exists(file_path):
            return web.json_response({"error": "File not found on disk"}, status=404)
        
        return web.FileResponse(
            file_path,
            headers={
                'Content-Disposition': f'attachment; filename="{file_name}"',
                'Content-Type': mime_type or 'application/octet-stream'
            }
        )
    
    except Exception as e:
        logger.error(f"Download error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_health(request):
    """Health check endpoint"""
    return web.Response(text="OK\n", status=200)

async def start_http_server():
    """Start HTTP server for file operations"""
    app = web.Application()
    app.router.add_post('/upload', handle_upload)
    app.router.add_get('/download/{file_id}', handle_download)
    app.router.add_get('/healthz', handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8081)
    await site.start()
    logger.info("HTTP server started on port 8081")

async def start_websocket_server():
    """Start WebSocket server"""
    async with websockets.serve(handle_client, "0.0.0.0", 8080):
        logger.info("WebSocket server started on port 8080")
        await asyncio.Future()  # Run forever

async def main():
    """Main entry point"""
    init_database()
    
    # Start both servers
    await asyncio.gather(
        start_websocket_server(),
        start_http_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
