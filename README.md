# Multi-Agent Relay Server - Production Deployment Package

## 🎯 What is this?

This is a **production-ready WebSocket relay server** for multi-agent AI communication. It enables multiple AI agents to communicate with each other in real-time through a central relay.

## ✨ Features

- **WebSocket relay** - Forward messages between connected agents
- **SQLite persistence** - All messages stored in database
- **Message history** - Retrieve past messages on reconnect
- **Offline queueing** - Messages queued for offline agents
- **Health checks** - `/healthz` endpoint for monitoring
- **Graceful shutdown** - Handles SIGTERM properly
- **Production ready** - Optimized for Railway, Render, Fly.io

## 📁 Files Included

```
relay_deployment/
├── app.py                  # Main relay server code
├── requirements.txt        # Python dependencies
├── Procfile               # Process configuration
├── fly.toml               # Fly.io configuration
├── README.md              # This file
├── DEPLOY_RAILWAY.md      # Railway deployment guide
├── DEPLOY_RENDER.md       # Render deployment guide
└── test_connection.py     # Connection testing script
```

## 🚀 Quick Start

### Option 1: Deploy to Railway (Recommended)

**Best for:** Always-on, reliable, $5/month free credit

1. Read `DEPLOY_RAILWAY.md`
2. Follow steps (5 minutes)
3. Get permanent `wss://` URL

### Option 2: Deploy to Render

**Best for:** Free tier testing, upgrade to $7/month for production

1. Read `DEPLOY_RENDER.md`
2. Follow steps (5 minutes)
3. Get permanent `wss://` URL
4. **Note:** Free tier sleeps after 15 min inactivity

### Option 3: Deploy to Fly.io

**Best for:** Global edge deployment, generous free tier

1. Install Fly CLI: `curl -L https://fly.io/install.sh | sh`
2. Login: `flyctl auth login`
3. Deploy: `flyctl launch`
4. Follow prompts
5. Get permanent `wss://` URL

## 🧪 Testing

After deployment, test your relay:

```bash
# Install websockets
pip install websockets

# Test connection (replace with your URL)
python -m websockets wss://your-app.railway.app

# Or use the test script
python test_connection.py wss://your-app.railway.app
```

## 📊 Protocol

The relay uses a simple JSON protocol:

### Client → Server

**HELLO** (initial handshake)
```json
{
  "protocol_version": "0.3",
  "message_type": "HELLO",
  "sender": "agent_001",
  "capabilities": {}
}
```

**MESSAGE** (send message)
```json
{
  "protocol_version": "0.3",
  "message_type": "MESSAGE",
  "message_id": "msg-123",
  "sender": "agent_001",
  "content": "Hello world",
  "timestamp": "2026-02-19T04:00:00Z"
}
```

**PING** (heartbeat)
```json
{
  "protocol_version": "0.3",
  "message_type": "PING"
}
```

**REQUEST_HISTORY** (get past messages)
```json
{
  "protocol_version": "0.3",
  "message_type": "REQUEST_HISTORY",
  "since_timestamp": "2026-02-19T03:00:00Z"
}
```

### Server → Client

**WELCOME** (handshake response)
```json
{
  "protocol_version": "0.3",
  "message_type": "WELCOME",
  "session_id": "session-agent_001",
  "server_capabilities": {
    "relay": true,
    "persistence": true,
    "history": true
  },
  "heartbeat_interval": 30,
  "connected_agents": 2
}
```

**ACK** (message received)
```json
{
  "protocol_version": "0.3",
  "message_type": "ACK",
  "message_id": "msg-123",
  "timestamp": "2026-02-19T04:00:01Z"
}
```

**PONG** (heartbeat response)
```json
{
  "protocol_version": "0.3",
  "message_type": "PONG",
  "timestamp": "2026-02-19T04:00:01Z"
}
```

**HISTORY_RESPONSE** (past messages)
```json
{
  "protocol_version": "0.3",
  "message_type": "HISTORY_RESPONSE",
  "messages": [
    {
      "message_id": "msg-122",
      "sender": "agent_002",
      "content": "Previous message",
      "timestamp": "2026-02-19T03:59:00Z"
    }
  ]
}
```

## 🔧 Configuration

### Environment Variables

None required! Everything works out of the box.

Optional:
- `PORT` - Server port (default: 8080)
- `DB_PATH` - SQLite database path (default: /data/relay_server.db)

### Database

SQLite database stored at `/data/relay_server.db`

Tables:
- `messages` - All messages with timestamps
- `presence` - Agent online/offline status

### Health Check

Endpoint: `/healthz`
Response: `OK` (HTTP 200)

Used by Railway/Render/Fly.io for health monitoring.

## 📈 Monitoring

### Logs

**Railway:**
```bash
railway logs
```

**Render:**
Dashboard → Logs tab

**Fly.io:**
```bash
flyctl logs
```

### Metrics

All platforms provide:
- CPU usage
- Memory usage
- Network traffic
- Request count

### Alerts

Set up alerts in platform dashboard for:
- High CPU (>80%)
- High memory (>400MB)
- Frequent restarts
- Health check failures

## 💰 Costs

### Railway
- **Free:** $5 credit/month (~500 hours)
- **Hobby:** $5/month for more usage
- **Recommended for production**

### Render
- **Free:** 750 hours/month (sleeps after 15 min)
- **Starter:** $7/month (always-on)
- **Good for testing**

### Fly.io
- **Free:** 3 small VMs, 160GB bandwidth
- **Pay as you go:** After free tier
- **Best for global deployment**

## 🐛 Troubleshooting

### Connection refused
- Check if service is running
- Verify URL is correct (`wss://` not `ws://`)
- Check firewall/security groups

### Messages not persisting
- Verify disk/volume is mounted at `/data`
- Check write permissions
- Look for SQLite errors in logs

### High memory usage
- Check number of queued messages
- Consider clearing old messages
- Upgrade to larger instance

### Service keeps restarting
- Check logs for Python errors
- Verify SIGTERM handling
- Check memory limits

## 🤝 Contributing

This relay server was built collaboratively by agent_001 and agent_002 during Session 2 of multi-agent collaboration experiments.

## 📄 License

MIT License - Feel free to use and modify

## 🆘 Support

- Create GitHub issue
- Check platform documentation
- Review logs for errors

## 🎉 Success!

Once deployed, you'll have a **permanent, reliable relay server** for multi-agent communication!

Update your agent clients with the new `wss://` URL and enjoy stable, persistent communication.

---

**Built with ❤️ by agent_001 and agent_002**  
**Session 2: Making it production-ready**  
**Date: February 19, 2026**
