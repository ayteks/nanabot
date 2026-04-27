# SoCandyShop TikTok Backend

FastAPI backend that wraps TikTok-Api (Playwright-based scraper) for the SoCandyShop Shopify boutique.

## Features

- **Live Status Detection** — checks if SoCandyShop is live on TikTok
- **User Info** — profile stats, bio, avatar
- **User Videos** — recent posts
- **Trending** — For You Page content
- **Hashtag Search** — videos by hashtag

## Setup

### 1. Install Dependencies

```bash
cd ~/tiktok-backend
./venv/bin/pip install fastapi uvicorn httpx TikTokApi
./venv/bin/python -m playwright install chromium
```

### 2. Get Your ms_token

The TikTok API needs an `ms_token` from your browser cookies to avoid bot detection.

1. Open Chrome/Firefox and login to **tiktok.com**
2. Open DevTools → Application (Chrome) or Storage (Firefox)
3. Find the `msToken` cookie
4. Copy its value

![TikTok msToken cookie](https://i.imgur.com/example.png)

> **How to find cookies:**
> - **Chrome**: F12 → Application → Cookies → tiktok.com → find `msToken`
> - **Firefox**: F12 → Storage → Cookies → tiktok.com → find `msToken`

### 3. Configure

Set the `TIKTOK_MS_TOKEN` in the service file:

```bash
# Edit the service file to add your token
nano ~/.config/systemd/user/socandyshop-tiktok.service
```

Uncomment this line and add your token:

```
Environment=TIKTOK_MS_TOKEN=your_ms_token_here
```

### 4. Install & Start Service

```bash
./venv/bin/python install_service.py
systemctl --user daemon-reload
systemctl --user start socandyshop-tiktok
systemctl --user enable socandyshop-tiktok
```

### 5. Verify

```bash
curl http://localhost:3100/health
```

Should return something like:
```json
{"status":"ok","sessions":2,"valid_sessions":2,"live_status":false}
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Backend health check |
| `GET /api/tiktok-live` | Live status (backward compat) |
| `GET /api/user/{username}` | User profile info |
| `GET /api/user/{username}/videos?count=12` | User's recent videos |
| `GET /api/trending?count=12` | Trending/FYP videos |
| `GET /api/hashtag/{tag}?count=12` | Videos by hashtag |

## Deploying to Server

From your dev machine:

```bash
# Set your server (replace with actual hostname/IP)
export TIKTOK_SERVER="user@socandyshopfr"

# Or pass as second arg:
# python deploy.py send user@192.168.x.x

# Send code
python deploy.py send

# Install deps + setup service
python deploy.py install

# Start
python deploy.py start

# Check logs
python deploy.py logs

# Full deploy in one go
python deploy.py setup
```

## Shopify Frontend Integration

The JS files in `~/boutique/` are already updated:

- `assets/tiktok-live.js` — live banner with real data
- `assets/tiktok-live.css` — banner styling
- Plus TikTok feed sections for videos/trending content

Upload these to your Shopify theme editor, or deploy as part of the boutique repo.
