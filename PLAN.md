# SoCandyShop TikTok Live Chat Bot v2 — Plan

## Goal
Build a real interactive TikTok live chat bot that:
- Reads live chat events (comments, gifts, likes, joins, follows)
- **Posts real chat messages** via Playwright browser automation (TikTok web chat)
- Auto-promotes SoCandyShop (French bonbons coréens/japonais)
- Sends Discord alerts on notable events
- Plugs into existing FastAPI backend (port 3100)

## Research Summary
- **TikTokLive** (`pip install TikTokLive`) — excellent for receiving events (WebSocket). Already installed in `venv`. Works.
- **TikTokLive does NOT support sending messages** — intentionally blocked. Must use browser automation.
- **Existing SoCandyShop backend** already has:
  - FastAPI app on port 3100
  - `TikTokApi` Playwright sessions running
  - `live_chat_bot.py` (v1) that listens but can't post
  - Discord webhook integration
  - Environment: xvfb, headless Chromium, systemd service

## Architecture

### Chat Posting Strategy
Use the existing Playwright session to open the TikTok live page and type directly into the chat input box. This requires a valid login session (cookies) and works as long as the web UI is stable.

```
+---------------+     WebSocket      +-----------------+
| TikTokLive    | <--- events ---    | TikTok Server   |
| (read only)   |                    |                 |
+---------------+                    +-----------------+
       |
       | triggers reply
       v
+---------------+     Playwright     +-----------------+
| ChatPoster    | --- types msg ---> | TikTok Web Chat |
| (send msg)    |                    | (browser input) |
+---------------+                    +-----------------+
```

## File Layout
```
~/tiktok-backend/
├── main.py                  # FastAPI app — add bot endpoints
├── live_chat_bot.py         # v1 reference (keep for now)
├── chat_poster.py           # NEW — Playwright-based chat posting
├── live_chat_bot_v2.py      # NEW — bot engine with real chat posting
├── discord_alerts.py        # Already exists
└── PLAN.md                  # This file
```

## Data Flow

### Startup (lifespan)
1. FastAPI boots → creates TikTokApi Playwright sessions
2. Bot starts (if `BOT_ENABLED=true`)
3. Bot polls `is_live()` every 60s
4. When live detected:
   a. Get a Playwright session page
   b. Navigate to `@soetsopains/live`
   c. Start TikTokLive WebSocket for events
5. On events → decide reply → `ChatPoster.post_chat(msg)`
6. Discord alert on gifts / follows / mentions

### Event → Reply Mapping
| Event | Trigger | Bot Reply | Rate Limit |
|---|---|---|---|
| Comment with keyword | "bot", "sobot", "aide", "help", "?", "prix", "shop" | Helpful reply + shop URL | 1 per user / 30s |
| Gift (streak end) | Any gift | Merci + name | 1 per gift |
| Like | Every 50 likes | Merci les likes! | Every 50 |
| Join | Every 5th joiner | Bienvenue + name | Every 5 |
| Follow | Each follow | Merci + name | Every follow |
| Promo timer | Every 5 min | Promo rotation | Every 5 min |

## Implementation Detail

### Chat Poster (`chat_poster.py`)
- Uses Playwright to navigate to `https://www.tiktok.com/@{handle}/live`
- Locates chat input via CSS selector fallback chain:
  - `[data-e2e="chat-input"]`, `[placeholder*="chat"]`, `div[contenteditable="true"]`
- Types message and presses Enter
- Error handling: timeout, element not found, retry once
- Rate limiting: internal cooldown (3s between posts)

### Bot Engine (`live_chat_bot_v2.py`)
- Extends v1 with `ChatPoster` injection
- All `_bot_say` calls now attempt real chat posting
- Graceful degrade: if posting fails, still log + Discord alert
- Auto-reconnection on disconnect
- State exposed via `get_state()`

### FastAPI Integration
New endpoints:
- `POST /api/bot/start` — start bot
- `POST /api/bot/stop` — stop bot
- `GET /api/bot/state` — current state (JSON)
- `POST /api/bot/say` — manual message override

### Environment Variables
```
BOT_ENABLED=true
BOT_GREET_NEW_VIEWERS=true
BOT_THANK_GIFTS=true
BOT_REPLY_MENTIONS=true
BOT_PROMO_INTERVAL=300
BOT_MAX_MSG_RATE=3          # seconds between posts
DISCORD_LIVE_WEBHOOK=...
```

## Risks & Mitigation
| Risk | Mitigation |
|---|---|
| TikTok web DOM changes | Use multiple CSS fallback selectors; graceful degrade to log-only |
| Rate-limited chat | 3s cooldown between posts; max 12 msg/min |
| Playwright page crash | Catch errors, retry once, fall back to log-only |
| Not logged in for chat | Need valid cookies from Playwright session; reuse existing sessions |

## Build Order
1. Write `chat_poster.py`
2. Write `live_chat_bot_v2.py`
3. Patch `main.py` with new endpoints + lifespan wiring
4. Test `BOT_ENABLED=true` startup
5. Manual override test (`/api/bot/say`)

