"""
SoCandyShop Discord Alerts
==========================
Sends alert messages to the Sak Mission Control Discord server
using raw HTTP calls (httpx) + bot token. No discord.py needed.

Alerts:
  • TikTok LIVE start / end
  • Backend health degradation
  • New video drops (when available)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger("socandyshop-discord")

# ── Config ──────────────────────────────────────────────────
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
# #updates channel in Sak Mission Control
DEFAULT_CHANNEL_ID = os.getenv("DISCORD_ALERTS_CHANNEL", "1499361259395485717")
DISCORD_API = "https://discord.com/api/v10"

# Track state transitions to avoid spam
_last_live_state: bool | None = None
_last_alert_ts: datetime | None = None
_MIN_ALERT_INTERVAL_SEC = 300  # 5 min between same-type alerts


# ── Helpers ─────────────────────────────────────────────────
def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _can_alert() -> bool:
    if not BOT_TOKEN:
        logger.warning("DISCORD_BOT_TOKEN not set — skipping Discord alert")
        return False
    return True


def _rate_limit_ok() -> bool:
    global _last_alert_ts
    if _last_alert_ts is None:
        return True
    elapsed = (datetime.utcnow() - _last_alert_ts).total_seconds()
    return elapsed >= _MIN_ALERT_INTERVAL_SEC


async def _send_message(channel_id: str, payload: dict[str, Any]) -> bool:
    """POST a message to a Discord channel."""
    if not _can_alert():
        return False
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=_headers(), json=payload)
            if resp.status_code in (200, 201):
                logger.info(f"Discord alert sent to #{channel_id}")
                return True
            logger.warning(f"Discord alert failed: {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Discord alert exception: {e}")
        return False


# ── Public API ──────────────────────────────────────────────
async def alert_tiktok_live(
    live: bool,
    viewer_count: int,
    title: str,
    avatar_url: str,
    profile_url: str,
    live_url: str,
) -> None:
    """
    Alert Discord when TikTok live status transitions.
    Only fires on state changes (offline→live or live→offline)
    and respects rate limits.
    """
    global _last_live_state, _last_alert_ts

    # Skip if no state change (except first run)
    if _last_live_state is not None and _last_live_state == live:
        return

    _last_live_state = live

    if live:
        # LIVE STARTED
        embed = {
            "title": "🔴 SoCandyShop est en LIVE sur TikTok !",
            "description": f"**{title}**" if title else "Rejoins le live maintenant !",
            "url": live_url,
            "color": 0xFF0050,  # TikTok red
            "timestamp": datetime.utcnow().isoformat(),
            "thumbnail": {"url": avatar_url} if avatar_url else None,
            "fields": [
                {"name": "👀 Viewers", "value": str(viewer_count), "inline": True},
                {"name": "🔗 Profil", "value": f"[@{os.getenv('TIKTOK_SHOP_HANDLE', 'soetsopains')}]({profile_url})", "inline": True},
            ],
        }
        payload = {
            "content": "@everyone 🔴 **LIVE STARTED**",
            "embeds": [embed],
        }
    else:
        # LIVE ENDED
        if not _rate_limit_ok():
            logger.info("Live-end alert rate-limited")
            return
        embed = {
            "title": "⚫ Live terminé",
            "description": "Le live TikTok de SoCandyShop est terminé.",
            "color": 0x333333,
            "timestamp": datetime.utcnow().isoformat(),
        }
        payload = {
            "content": "⚫ **LIVE ENDED**",
            "embeds": [embed],
        }

    ok = await _send_message(DEFAULT_CHANNEL_ID, payload)
    if ok:
        _last_alert_ts = datetime.utcnow()


async def alert_health_degraded(detail: str) -> None:
    """Alert when backend health check fails."""
    if not _rate_limit_ok():
        return
    embed = {
        "title": "⚠️ Backend Health Issue",
        "description": detail,
        "color": 0xFFA500,
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload = {"content": "⚠️ **System Alert**", "embeds": [embed]}
    ok = await _send_message(DEFAULT_CHANNEL_ID, payload)
    if ok:
        _last_alert_ts = datetime.utcnow()


async def alert_new_videos(count: int) -> None:
    """Alert when new videos are scraped."""
    if not _rate_limit_ok() or count == 0:
        return
    payload = {
        "content": f"📹 **{count} new TikTok video(s)** scraped for SoCandyShop feed.",
    }
    ok = await _send_message(DEFAULT_CHANNEL_ID, payload)
    if ok:
        _last_alert_ts = datetime.utcnow()


async def alert_backend_started() -> None:
    """Alert when backend boots."""
    embed = {
        "title": "🚀 SoCandyShop Backend Online",
        "description": "TikTok backend started successfully.",
        "color": 0x00C853,
        "timestamp": datetime.utcnow().isoformat(),
    }
    payload = {"content": "🚀 **Backend Started**", "embeds": [embed]}
    await _send_message(DEFAULT_CHANNEL_ID, payload)
