"""
SoCandyShop Discord Alerts
==========================
Envoie des alertes sur le serveur Discord Sak Mission Control
via des appels HTTP bruts (httpx) + token bot. Pas besoin de discord.py.

Alertes:
  • TikTok LIVE démarré / terminé  (seules alertes envoyées)
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
BOT_TOKEN = os.getenv("DISCORD_BOT_COMMAND_TOKEN", os.getenv("DISCORD_BOT_TOKEN", ""))
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

    # Only alert on actual state transitions — first check initializes silently
    if _last_live_state is None:
        _last_live_state = live
        return
    if _last_live_state == live:
        return

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
            "content": "@everyone 🔴 **LIVE DÉMARRÉ**",
            "embeds": [embed],
            "allowed_mentions": {"parse": ["everyone"]},
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
            "content": "@everyone ⚫ **LIVE TERMINÉ**",
            "embeds": [embed],
            "allowed_mentions": {"parse": ["everyone"]},
        }

    ok = await _send_message(DEFAULT_CHANNEL_ID, payload)
    if ok:
        _last_alert_ts = datetime.utcnow()


async def alert_health_degraded(detail: str) -> None:
    """Alert when backend health check fails."""
    return  # Disabled — only live/offline alerts are sent


async def alert_new_videos(count: int) -> None:
    """Alert when new videos are scraped."""
    return  # Disabled — only live/offline alerts are sent


async def alert_backend_started() -> None:
    """Alert when backend boots."""
    return  # Disabled — only live/offline alerts are sent


async def alert_chat_gift(user_name: str, gift_name: str, repeat_count: int) -> None:
    """Alert when someone sends a gift in live chat."""
    return  # Disabled — only live/offline alerts are sent


async def alert_chat_follow(user_name: str) -> None:
    """Alert when someone follows during the live."""
    return  # Disabled — only live/offline alerts are sent


async def alert_chat_mention(user_name: str, comment: str) -> None:
    """Alert when someone mentions the bot in chat."""
    return  # Disabled — only live/offline alerts are sent
