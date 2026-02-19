# Deploy Multi-Agent Relay to Render

## Quick Deploy (5 minutes)

### Prerequisites
- GitHub account
- Render account (sign up at https://render.com with GitHub)

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

### Step 3: Deploy on Render

1. Go to https://dashboard.render.com
2. Click **New +** → **Web Service**
3. Connect your GitHub account if not already connected
4. Select `multi-agent-relay` repository
5. Configure:
   - **Name:** `multi-agent-relay`
   - **Region:** Choose closest to you
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py`
   - **Instance Type:** `Free`

6. Click **Advanced** and add:
   - **Health Check Path:** `/healthz`
   - **Auto-Deploy:** `Yes`

7. Click **Create Web Service**

### Step 4: Add Persistent Disk

1. In your service dashboard, go to **Disks** tab
2. Click **Add Disk**
3. Configure:
   - **Name:** `relay-data`
   - **Mount Path:** `/data`
   - **Size:** 1GB (free tier)
4. Click **Save**

**Important:** Service will restart after adding disk

### Step 5: Get Public URL

Your service URL will be:
```
https://multi-agent-relay.onrender.com
```

Your WebSocket URL:
```
wss://multi-agent-relay.onrender.com
```

### Step 6: Test Connection

```bash
# Install websockets client
pip install websockets

# Test connection
python -m websockets wss://multi-agent-relay.onrender.com

# You should see connection successful
# Type CTRL+D to exit
```

## Configuration Files

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

### render.yaml (optional, for Infrastructure as Code)

Create `render.yaml` in root:

```yaml
services:
  - type: web
    name: multi-agent-relay
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python app.py
    healthCheckPath: /healthz
    disk:
      name: relay-data
      mountPath: /data
      sizeGB: 1
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.0
```

Then deploy with:
```bash
render deploy
```

## Troubleshooting

### Build fails
- Check build logs in Render dashboard
- Verify `requirements.txt` is correct
- Try specifying Python version in `runtime.txt`:
  ```
  python-3.11.0
  ```

### WebSocket connection fails
- Ensure you're using `wss://` not `ws://`
- Check service logs in Render dashboard
- Verify health check: `curl https://your-app.onrender.com/healthz`
- **Important:** Render free tier may sleep after 15 min of inactivity
  - First connection will wake it up (takes ~30 seconds)
  - Consider upgrading to paid tier for always-on

### Database not persisting
- Verify disk is mounted at `/data`
- Check logs for SQLite errors
- Ensure disk was created before first messages

### Service keeps restarting
- Check logs for Python errors
- Verify SIGTERM handling
- Check memory usage (free tier: 512MB limit)

## Free Tier Limitations

**Important:** Render free tier has limitations:

1. **Sleeps after 15 minutes of inactivity**
   - First request wakes it up (~30 seconds)
   - Not ideal for real-time communication
   - **Solution:** Upgrade to Starter ($7/month) for always-on

2. **750 hours/month limit**
   - Enough for ~31 days if always on
   - Shared across all free services

3. **512MB RAM limit**
   - Should be enough for relay server
   - Monitor usage in dashboard

**Recommendation:** Use free tier for testing, upgrade to Starter for production.

## Monitoring

### View Logs
1. Go to service dashboard
2. Click **Logs** tab
3. Real-time streaming logs

### Check Health
```bash
curl https://multi-agent-relay.onrender.com/healthz
# Should return: OK
```

### Monitor Metrics
Render dashboard shows:
- CPU usage
- Memory usage
- Request count
- Response times

## Costs

**Free Tier:**
- $0/month
- 750 hours/month
- 512MB RAM
- 1GB disk
- Sleeps after 15 min inactivity

**Starter Plan ($7/month):**
- Always-on (no sleep)
- 512MB RAM
- 1GB disk included
- Better for production

**Standard Plan ($25/month):**
- 2GB RAM
- 10GB disk included
- Priority support

## Keeping Service Awake (Free Tier Hack)

If you want to use free tier without sleep, add a cron job to ping every 10 minutes:

```bash
# On your local machine or another server
*/10 * * * * curl https://multi-agent-relay.onrender.com/healthz
```

Or use a service like:
- UptimeRobot (free)
- Cron-job.org (free)
- Pingdom (free tier)

## Next Steps

1. Update both agent clients with new URL
2. Test reconnection
3. Verify message persistence
4. Monitor for 24 hours
5. Consider upgrading if needed
6. Celebrate deployment! 🎉

## Support

- Render Docs: https://render.com/docs
- Render Community: https://community.render.com
- GitHub Issues: https://github.com/YOUR_USERNAME/multi-agent-relay/issues
