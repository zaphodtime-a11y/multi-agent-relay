# Deploy Multi-Agent Relay to Railway

## Quick Deploy (5 minutes)

### Prerequisites
- GitHub account
- Railway account (sign up at https://railway.app with GitHub)

### Step 1: Create GitHub Repository

1. Go to https://github.com/new
2. Repository name: `multi-agent-relay`
3. Description: `WebSocket relay server for multi-agent communication`
4. Public or Private: **Public** (for free tier)
5. Click **Create repository**

### Step 2: Push Code to GitHub

```bash
# Clone this directory or copy files
cd /path/to/relay_deployment

# Initialize git
git init
git add .
git commit -m "Initial commit: Multi-agent relay server v0.3"

# Add remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/multi-agent-relay.git

# Push
git branch -M main
git push -u origin main
```

### Step 3: Deploy on Railway

1. Go to https://railway.app
2. Click **New Project**
3. Select **Deploy from GitHub repo**
4. Authorize Railway to access your GitHub
5. Select `multi-agent-relay` repository
6. Railway will auto-detect Python and deploy

### Step 4: Configure Environment

Railway auto-detects everything, but verify:

1. In Railway dashboard, click your service
2. Go to **Settings** tab
3. Verify:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py`
   - **Port:** 8080 (auto-detected)

### Step 5: Add Persistent Storage

1. In your service, go to **Data** tab
2. Click **Add Volume**
3. Mount path: `/data`
4. Size: 1GB (free tier)
5. Click **Add**

### Step 6: Get Public URL

1. Go to **Settings** tab
2. Scroll to **Networking**
3. Click **Generate Domain**
4. You'll get: `your-app-name.up.railway.app`
5. Your WebSocket URL: `wss://your-app-name.up.railway.app`

### Step 7: Test Connection

```bash
# Install websockets client
pip install websockets

# Test connection (replace URL)
python -m websockets wss://your-app-name.up.railway.app

# You should see connection successful
# Type CTRL+D to exit
```

## Configuration Files Explained

### app.py
- Main relay server code
- Handles WebSocket connections
- SQLite persistence
- Health check at `/healthz`
- Graceful shutdown on SIGTERM

### requirements.txt
```
websockets==13.0.1
```

### Procfile (optional, Railway auto-detects)
```
web: python app.py
```

### railway.json (optional, for custom config)
```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS"
  },
  "deploy": {
    "startCommand": "python app.py",
    "healthcheckPath": "/healthz",
    "healthcheckTimeout": 100,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

## Troubleshooting

### Build fails
- Check Python version in `runtime.txt`: `python-3.11`
- Verify `requirements.txt` is in root directory

### WebSocket connection fails
- Ensure you're using `wss://` not `ws://`
- Check Railway logs: `railway logs`
- Verify health check: `curl https://your-app.up.railway.app/healthz`

### Database not persisting
- Verify volume is mounted at `/data`
- Check Railway logs for SQLite errors

### Service keeps restarting
- Check logs for errors
- Verify SIGTERM handling is working
- Increase health check timeout

## Monitoring

### View Logs
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# View logs
railway logs
```

### Check Health
```bash
curl https://your-app.up.railway.app/healthz
# Should return: OK
```

### Monitor Connections
Check Railway dashboard → Metrics for:
- CPU usage
- Memory usage
- Network traffic

## Costs

**Free Tier:**
- $5 credit per month
- Enough for ~500 hours of uptime
- 1GB storage included
- Unlimited bandwidth

**If you need more:**
- Upgrade to Hobby plan ($5/month)
- Or add credit as needed

## Next Steps

1. Update both agent clients with new URL
2. Test reconnection
3. Verify message persistence
4. Monitor for 24 hours
5. Celebrate permanent deployment! 🎉

## Support

- Railway Docs: https://docs.railway.app
- Railway Discord: https://discord.gg/railway
- GitHub Issues: https://github.com/YOUR_USERNAME/multi-agent-relay/issues
