# Deploy Choreo

Get Choreo running on a public URL with SSL, SSE streaming, and outbound webhooks to n8n or any HTTP endpoint.

## Quick Deploy (Docker)

The fastest path from clone to running:

```bash
git clone https://github.com/clovenbradshaw-ctrl/Choreo.git
cd Choreo
docker compose up -d
```

Choreo is now on `http://localhost:8420/ui`. For production with SSL, continue to the nginx and certbot sections below.

## Quick Deploy (Bare Metal)

```bash
git clone https://github.com/clovenbradshaw-ctrl/Choreo.git
cd Choreo
pip3 install -r requirements.txt
python3 choreo_runtime.py --port 8420
```

---

## Full Production Setup

### 1. Prerequisites

A Linux server (Ubuntu 22/24) with a domain pointed at it.

```bash
# On your server
sudo apt update && sudo apt install -y python3 python3-pip nginx certbot python3-certbot-nginx

# Clone from GitHub
git clone https://github.com/clovenbradshaw-ctrl/Choreo.git ~/choreo
cd ~/choreo
pip3 install -r requirements.txt --break-system-packages

# If using PM2 (recommended — same pattern as n8n)
sudo npm install -g pm2
```

### 2. Test locally

```bash
cd ~/choreo
mkdir -p instances
python3 choreo_runtime.py --port 8420

# In another terminal, verify:
curl http://localhost:8420/ui
curl -X POST http://localhost:8420/demo/seed
curl http://localhost:8420/demo/state/places
```

### 3. Nginx reverse proxy

The SSE stream endpoint needs special proxy settings to disable buffering, otherwise nginx holds events and the UI won't update in real time.

```bash
# Generate the config automatically:
python3 choreo_runtime.py --nginx choreo.yourdomain.com > /tmp/choreo.nginx

# Or use the included template:
sudo cp nginx/choreo.conf /etc/nginx/sites-available/choreo

# Edit the server_name to match your domain:
sudo sed -i 's/choreo.yourdomain.com/choreo.YOURDOMAIN.com/' /etc/nginx/sites-available/choreo
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/choreo /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

The key nginx settings for SSE:

```nginx
# SSE stream — THIS IS THE KEY PART
# Without these settings, nginx buffers the event stream
# and the UI/clients never see real-time updates
location ~ ^/[^/]+/stream$ {
    proxy_pass http://127.0.0.1:8420;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    chunked_transfer_encoding off;
}
```

### 4. SSL with Let's Encrypt

```bash
sudo certbot --nginx -d choreo.yourdomain.com
```

Certbot rewrites the nginx config to add SSL. After this, your UI is live at `https://choreo.yourdomain.com/ui`.

### 5. Keep it running with PM2

Same pattern as n8n. PM2 auto-restarts on crash and survives reboots.

```bash
# Use the included ecosystem config (edit paths if needed):
cd ~/choreo
pm2 start ecosystem.config.js
pm2 save
pm2 startup   # generates the systemd service
```

Check status: `pm2 status` should show choreo as `online`. Logs: `pm2 logs choreo`.

### 6. Deploy with Docker (alternative)

```bash
cd ~/choreo
docker compose up -d
```

This exposes port 8420. Point nginx at it the same way (proxy_pass to 127.0.0.1:8420). Data persists in the `choreo-data` Docker volume.

---

## Outbound Webhooks

Every operation appended to the log can be POSTed to external URLs. This is how you connect Choreo to n8n, Zapier, or any HTTP endpoint.

### Register a webhook

```bash
# Send every operation to n8n
curl -X POST https://choreo.yourdomain.com/my-instance/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://n8n.yourdomain.com/webhook/choreo-ingest",
    "active": true
  }'

# Only send ALT and NUL operations (filter by op type)
curl -X POST https://choreo.yourdomain.com/my-instance/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://n8n.yourdomain.com/webhook/choreo-changes",
    "filter": ["ALT", "NUL", "INS"]
  }'
```

### What gets POSTed

Every matching operation is sent as JSON:

```json
{
  "op_id": 47,
  "ts": "2026-02-15T16:22:01.000Z",
  "op": "ALT",
  "target": {
    "id": "pl0",
    "status": "closed"
  },
  "context": {
    "table": "places",
    "source": "scraper"
  },
  "frame": {
    "reason": "Yelp listing shows permanently closed"
  }
}
```

Headers: `Content-Type: application/json`, `X-Choreo-Instance: my-instance`.

### n8n integration

1. In n8n, create a Webhook trigger node. Copy the Production URL.
2. Register it with Choreo:

```bash
curl -X POST https://choreo.yourdomain.com/my-instance/webhooks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://n8n.yourdomain.com/webhook/abc123"}'
```

3. Every operation now triggers your n8n workflow. Route with IF nodes: `op == "ALT"` → do X, `op == "INS"` → do Y.

### Manage webhooks

```bash
# List
curl https://choreo.yourdomain.com/my-instance/webhooks

# Remove
curl -X DELETE https://choreo.yourdomain.com/my-instance/webhooks \
  -H "Content-Type: application/json" \
  -d '{"url": "https://n8n.yourdomain.com/webhook/abc123"}'
```

Webhooks are fire-and-forget. 4-worker thread pool, 10s timeout. If the endpoint is down, the operation still succeeds — the log is the source of truth. Config stored in `instances/webhooks.json`.

---

## SSE Stream (real-time)

For live connections, use the SSE stream endpoint directly. This is what the UI uses internally.

### JavaScript

```javascript
const sse = new EventSource(
  "https://choreo.yourdomain.com/my-instance/stream"
);

sse.addEventListener("op", (event) => {
  const op = JSON.parse(event.data);
  console.log(op.op, op.target, op.context);
});

// Resume after disconnect
const sse2 = new EventSource(
  "https://choreo.yourdomain.com/my-instance/stream?last_id=47"
);
```

### Python

```python
import requests, json

url = "https://choreo.yourdomain.com/my-instance/stream"
with requests.get(url, stream=True) as r:
    for line in r.iter_lines():
        if line and line.startswith(b"data: "):
            op = json.loads(line[6:])
            print(f"{op['op']} {op['target']}")
```

### curl

```bash
curl -N https://choreo.yourdomain.com/my-instance/stream
curl -N https://choreo.yourdomain.com/my-instance/stream?last_id=100
```

**SSE vs Webhooks:** Use SSE when your client is always-on and needs sub-second latency (dashboards, live UIs). Use webhooks for fire-and-forget delivery to workflow engines (n8n, Zapier).

---

## Sending Operations In

Every mutation enters through one endpoint.

### From curl / scripts

```bash
# Create an entity
curl -X POST https://choreo.yourdomain.com/my-instance/operations \
  -H "Content-Type: application/json" \
  -d '{
    "op": "INS",
    "target": {"id": "p1", "name": "New Place", "status": "open"},
    "context": {"table": "places", "source": "manual"}
  }'

# Update a field
curl -X POST https://choreo.yourdomain.com/my-instance/operations \
  -H "Content-Type: application/json" \
  -d '{
    "op": "ALT",
    "target": {"id": "p1", "status": "closed"},
    "context": {"table": "places"},
    "frame": {"reason": "Confirmed closed via site visit"}
  }'
```

### Snapshot ingest (bulk import)

```bash
curl -X POST https://choreo.yourdomain.com/my-instance/operations \
  -H "Content-Type: application/json" \
  -d '{
    "op": "REC",
    "target": {
      "rows": [
        {"name": "Fern & Iron", "hours": "9a-6p", "status": "open"},
        {"name": "New Spot", "hours": "10a-8p", "status": "open"}
      ]
    },
    "context": {"type": "snapshot_ingest", "table": "places"},
    "frame": {
      "match_on": "name",
      "absence_means": "unchanged",
      "null_fields_mean": "unchanged"
    }
  }'
```

### From n8n

In n8n, use an HTTP Request node:
- Method: `POST`
- URL: `https://choreo.yourdomain.com/my-instance/operations`
- Body (JSON):

```json
{
  "op": "INS",
  "target": {
    "id": "{{ $json.id }}",
    "name": "{{ $json.name }}",
    "status": "{{ $json.status }}"
  },
  "context": {
    "table": "scraped-data",
    "source": "n8n-workflow-123"
  }
}
```

---

## Quick Reference

### Endpoints

```
POST /:instance/operations  — the one way in
GET  /:instance/stream      — the one way out (SSE)
POST /:instance/webhooks    — register outbound webhook
GET  /:instance/webhooks    — list webhooks
DEL  /:instance/webhooks    — remove webhook
GET  /instances             — list instances
POST /instances             — create instance {slug}
POST /demo/seed             — seed demo data
GET  /ui                    — web interface
```

### Files

```
instances/*.db              — SQLite databases (one per instance)
instances/webhooks.json     — webhook registrations
```

### Environment Variables

```
CHOREO_PORT                 — HTTP port (default: 8420)
CHOREO_DIR                  — Instance data directory (default: ./instances)
CHOREO_SNAPSHOT_INTERVAL    — Ops between snapshots (default: 1000)
```
