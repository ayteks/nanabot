"""
SoCandyShop TikTok Live Chat Bot
================================
Connects to TikTok LIVE chat via TikTokLive WebSocket client.
Auto-responds to comments, gifts, likes, joins, and follows.
Runs as an asyncio background task within the FastAPI lifespan.

Environment:
  TIKTOK_SHOP_HANDLE   — target streamer unique_id (default: soetsopains)
  BOT_ENABLED          — "true" to auto-start on backend boot (default: false)
  BOT_GREET_NEW_VIEWERS— "true" to greet joiners (default: true)
  BOT_THANK_GIFTS      — "true" to thank gift senders (default: true)
  BOT_REPLY_MENTIONS   — "true" to reply when bot name is mentioned (default: true)
  BOT_PROMO_INTERVAL   — seconds between auto promo messages (default: 300, 0=off)
  DISCORD_LIVE_WEBHOOK — optional Discord webhook for live events
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("socandyshop-tiktok")

# ── Optional TikTokLive import (graceful degrade) ─────────
try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import (
        ConnectEvent,
        DisconnectEvent,
        CommentEvent,
        GiftEvent,
        LikeEvent,
        JoinEvent,
        FollowEvent,
        ShareEvent,
    )
    from TikTokLive.client.logger import LogLevel
    _HAS_TIKTOKLIVE = True
except Exception as exc:
    _HAS_TIKTOKLIVE = False
    logger.warning(f"TikTokLive library not available: {exc}")

# ── Discord alerts integration ────────────────────────────
try:
    import discord_alerts
    _HAS_DISCORD = True
except Exception:
    _HAS_DISCORD = False

# ── Config ──────────────────────────────────────────────────
SHOP_HANDLE = os.getenv("TIKTOK_SHOP_HANDLE", "soetsopains")
BOT_ENABLED = os.getenv("BOT_ENABLED", "false").lower() == "true"
BOT_GREET_NEW_VIEWERS = os.getenv("BOT_GREET_NEW_VIEWERS", "true").lower() == "true"
BOT_THANK_GIFTS = os.getenv("BOT_THANK_GIFTS", "true").lower() == "true"
BOT_REPLY_MENTIONS = os.getenv("BOT_REPLY_MENTIONS", "true").lower() == "true"
_BOT_PROMO_INTERVAL = int(os.getenv("BOT_PROMO_INTERVAL", "300"))
BOT_PROMO_INTERVAL = _BOT_PROMO_INTERVAL if _BOT_PROMO_INTERVAL > 0 else 0
DISCORD_LIVE_WEBHOOK = os.getenv("DISCORD_LIVE_WEBHOOK", "")

# ── State ───────────────────────────────────────────────────
@dataclass
class BotState:
    enabled: bool = False
    connected: bool = False
    room_id: str = ""
    viewer_count: int = 0
    comment_count: int = 0
    gift_count: int = 0
    like_count: int = 0
    join_count: int = 0
    follow_count: int = 0
    promo_index: int = 0
    last_promo_ts: float = 0.0
    messages_sent: int = 0
    errors: list[str] = field(default_factory=list)
    recent_comments: list[dict] = field(default_factory=list)  # last 50

state = BotState()

# ── Promo rotation ──────────────────────────────────────────
_PROMO_MESSAGES = [
    "🍬 Bienvenue chez SoCandyShop ! Découvre nos bonbons coréens et japonais sur notre boutique en ligne !",
    "🎁 Tu cherches des friandises uniques ? Rendez-vous sur notre site pour commander !",
    "✨ SoCandyShop — le meilleur des sweets asiatiques livrés chez toi !",
    "🍭 Tu aimes les bonbons ? Check notre shop, on a des nouveautés toutes les semaines !",
    "💬 Pose tes questions ici, on répond en direct ! Et n'oublie pas de visiter notre boutique !",
]

_GREETINGS = [
    "Bienvenue {name} ! 🍬 Profite bien du live !",
    "Hey {name} ! Ravie de te voir ici ! 🎉",
    "Salut {name} ! Prêt(e) à découvrir des bonbons incroyables ? 🍭",
    "Welcome {name} ! SoCandyShop te souhaite un bon moment ! ✨",
    "Coucou {name} ! N'hésite pas à poser tes questions ! 🍬",
]

_THANKS_GIFT = [
    "Merci beaucoup {name} pour le cadeau ! 🎁💖",
    "Wouah {name}, merci pour le gift ! Tu es génial(e) ! 🌟",
    "Merci {name} ! Ton soutien fait fondre notre cœur (et nos bonbons) ! 🍫",
    "{name} t'es un(e) vrai(e) fan ! Merci pour le gift ! 🙏",
]

_REPLY_MENTION = [
    "Coucou {name} ! 🍬 Je suis le bot de SoCandyShop ! Comment puis-je t'aider ?",
    "Hey {name} ! Tu as une question sur nos bonbons ? Je suis là pour ça !",
    "{name} ! SoCandyShop te propose les meilleurs sweets asiatiques, check notre shop ! 🍭",
]

_THANKS_LIKE = [
    "Merci pour les likes ! ❤️",
    "Vous êtes incroyables avec tous ces likes ! 🔥",
]

_THANKS_FOLLOW = [
    "Merci pour le follow {name} ! 🎉 Bienvenue dans la famille SoCandyShop !",
    "{name} a rejoint la famille ! Merci pour le follow ! 🍬",
]

# ── Chat reply helper ───────────────────────────────────────
# TikTokLive does NOT support sending chat messages (TikTok blocks it).
# We simulate interaction by logging replies and sending Discord alerts.
# Future: integrate with TikTok's official API or mobile automation.

def _pick(template_list: list[str], **fmt) -> str:
    return random.choice(template_list).format(**fmt)

async def _bot_say(text: str, event_type: str = "reply") -> None:
    """Log a bot 'message' (since we can't post to TikTok chat directly)."""
    state.messages_sent += 1
    logger.info(f"[BOT {event_type.upper()}] {text}")
    # Future hook: send via TikTok official API if available


# ── Event Handlers ──────────────────────────────────────────
_seen_comment_ids: set[str] = set()
_seen_join_ids: set[str] = set()


async def _on_connect(event: "ConnectEvent") -> None:
    state.connected = True
    state.room_id = str(getattr(event, "room_id", ""))
    logger.info(f"🟢 Bot connected to livestream of @{SHOP_HANDLE} (room={state.room_id})")
    await _bot_say("🍬 Bot SoCandyShop est en ligne ! Pose tes questions, je suis là !", "system")


async def _on_disconnect(event: "DisconnectEvent") -> None:
    state.connected = False
    state.room_id = ""
    logger.info("🔴 Bot disconnected from livestream")


async def _on_comment(event: "CommentEvent") -> None:
    state.comment_count += 1
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    unique_id = getattr(user, "unique_id", "") if user else ""
    comment = getattr(event, "comment", "")

    # Deduplicate by user+comment text (TikTokLive sometimes double-fires)
    dedup_key = f"{unique_id}:{nickname}:{comment}"
    if dedup_key in _seen_comment_ids:
        return
    _seen_comment_ids.add(dedup_key)
    if len(_seen_comment_ids) > 500:
        _seen_comment_ids.clear()  # reset to prevent memory leak; loss acceptable

    rec = {
        "ts": datetime.utcnow().isoformat(),
        "user": unique_id or nickname,
        "nickname": nickname,
        "text": comment,
    }
    state.recent_comments.append(rec)
    if len(state.recent_comments) > 50:
        state.recent_comments.pop(0)

    logger.info(f"[CHAT] {nickname} (@{unique_id}): {comment}")

    # Reply if bot name mentioned
    if BOT_REPLY_MENTIONS and comment:
        lowered = comment.lower()
        if any(k in lowered for k in ("bot", "sobot", "socandy", "aide", "help", "?")):
            reply = _pick(_REPLY_MENTION, name=nickname or unique_id or "ami(e)")
            await _bot_say(reply, "reply")
            if _HAS_DISCORD:
                try:
                    await discord_alerts.alert_chat_mention(nickname or unique_id, comment)
                except Exception:
                    pass
            return


async def _on_gift(event: "GiftEvent") -> None:
    state.gift_count += 1
    if not BOT_THANK_GIFTS:
        return
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    unique_id = getattr(user, "unique_id", "") if user else ""
    gift = getattr(event, "gift", None)
    gift_name = getattr(gift, "name", "") if gift else ""
    streaking = getattr(event, "streaking", False)
    repeat = getattr(event, "repeat_count", 1)

    if not streaking:  # thank at end of streak
        msg = _pick(_THANKS_GIFT, name=nickname or unique_id or "toi")
        if gift_name:
            msg += f" (Cadeau: {gift_name} x{repeat})"
        await _bot_say(msg, "gift")
        if _HAS_DISCORD:
            try:
                await discord_alerts.alert_chat_gift(nickname or unique_id, gift_name, repeat)
            except Exception:
                pass


async def _on_like(event: "LikeEvent") -> None:
    state.like_count += getattr(event, "count", 1)
    # Batch-like thanks — only thank every ~50 likes to avoid spam
    if state.like_count % 50 == 0:
        await _bot_say(random.choice(_THANKS_LIKE), "like")


async def _on_join(event: "JoinEvent") -> None:
    state.join_count += 1
    if not BOT_GREET_NEW_VIEWERS:
        return
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    unique_id = getattr(user, "unique_id", "") if user else ""
    # Deduplicate joins
    dedup_key = f"join:{unique_id}:{nickname}"
    if dedup_key in _seen_join_ids:
        return
    _seen_join_ids.add(dedup_key)
    if len(_seen_join_ids) > 500:
        _seen_join_ids.clear()  # reset to prevent memory leak
    # Greet every 5th joiner to avoid flooding
    if state.join_count % 5 == 0:
        msg = _pick(_GREETINGS, name=nickname or unique_id or "nouveau")
        await _bot_say(msg, "join")


async def _on_follow(event: "FollowEvent") -> None:
    state.follow_count += 1
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    unique_id = getattr(user, "unique_id", "") if user else ""
    msg = _pick(_THANKS_FOLLOW, name=nickname or unique_id or "toi")
    await _bot_say(msg, "follow")
    if _HAS_DISCORD:
        try:
            await discord_alerts.alert_chat_follow(nickname or unique_id)
        except Exception:
            pass


async def _on_share(event: "ShareEvent") -> None:
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    await _bot_say(f"Merci {nickname or 'toi'} pour le partage ! 🚀", "share")


# ── Promo loop ──────────────────────────────────────────────
async def _promo_loop(client: Optional["TikTokLiveClient"]) -> None:
    """Send periodic promo messages when live and enabled."""
    if BOT_PROMO_INTERVAL <= 0:
        return
    while state.enabled:
        await asyncio.sleep(BOT_PROMO_INTERVAL)
        if not state.connected or not state.enabled:
            continue
        msg = _PROMO_MESSAGES[state.promo_index % len(_PROMO_MESSAGES)]
        state.promo_index += 1
        await _bot_say(msg, "promo")


# ── Client lifecycle ────────────────────────────────────────
_client: Optional["TikTokLiveClient"] = None
_bot_task: Optional[asyncio.Task] = None
_promo_task: Optional[asyncio.Task] = None


async def start_bot(cookies: dict[str, str] | None = None) -> dict:
    """Start the live chat bot as a background task.

    Args:
        cookies: Optional dict of cookies from a Playwright session to forward
                 to TikTokLive (helps avoid DEVICE_BLOCKED).
    """
    global _client, _bot_task, _promo_task

    if not _HAS_TIKTOKLIVE:
        return {"ok": False, "error": "TikTokLive library not installed"}
    if _bot_task and not _bot_task.done():
        return {"ok": True, "status": "already_running"}

    state.enabled = True
    state.errors.clear()

    # Build web_kwargs with cookies and realistic headers
    if cookies:
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.web_client_cookies.update(cookies)
        logger.info(f"Bot will use {len(cookies)} forwarded cookies via WebDefaults")

    # Optional proxy
    web_proxy = None
    ws_proxy = None
    proxy_url = os.getenv("BOT_PROXY", "")
    if proxy_url:
        import httpx
        web_proxy = httpx.Proxy(url=proxy_url)
        ws_proxy = httpx.Proxy(url=proxy_url)
        logger.info(f"Bot using proxy: {proxy_url}")

    try:
        _client = TikTokLiveClient(
            unique_id=f"@{SHOP_HANDLE}",
            web_proxy=web_proxy,
            ws_proxy=ws_proxy,
        )
        # Set realistic headers to match Playwright session
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.web_client_headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Referer": "https://www.tiktok.com/",
        })

        _client.on(ConnectEvent)(_on_connect)
        _client.on(DisconnectEvent)(_on_disconnect)
        _client.on(CommentEvent)(_on_comment)
        _client.on(GiftEvent)(_on_gift)
        _client.on(LikeEvent)(_on_like)
        _client.on(JoinEvent)(_on_join)
        _client.on(FollowEvent)(_on_follow)
        _client.on(ShareEvent)(_on_share)
    except Exception as e:
        state.errors.append(str(e))
        logger.error(f"Bot init failed: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}

    async def _bot_runner() -> None:
        """Poll live status, connect when live, reconnect on disconnect."""
        consecutive_errors = 0
        while state.enabled:
            try:
                is_live = await _client.is_live()
                if not is_live:
                    wait = min(60 + consecutive_errors * 10, 300)
                    logger.info(f"Bot poll: @{SHOP_HANDLE} not live. Retry in {wait}s.")
                    await asyncio.sleep(wait)
                    consecutive_errors = max(0, consecutive_errors - 1)
                    continue

                logger.info(f"Bot poll: @{SHOP_HANDLE} is LIVE! Connecting...")
                await _client.connect()
                consecutive_errors = 0
                logger.info("Bot: stream ended, will re-poll in 60s.")
                await asyncio.sleep(60)
            except Exception as e:
                consecutive_errors += 1
                err_str = str(e)
                state.errors.append(err_str)
                if len(state.errors) > 20:
                    state.errors.pop(0)

                # Backoff: 30s, 60s, 120s, 240s, then cap at 300s
                wait = min(30 * (2 ** min(consecutive_errors - 1, 4)), 300)
                logger.warning(f"Bot runner error (attempt {consecutive_errors}): {err_str}. Retry in {wait}s.")
                await asyncio.sleep(wait)

    _bot_task = asyncio.create_task(_bot_runner())
    _promo_task = asyncio.create_task(_promo_loop(_client))
    logger.info("🤖 TikTok Live Chat Bot started")
    return {"ok": True, "status": "started"}


async def stop_bot() -> dict:
    """Stop the bot gracefully."""
    global _client, _bot_task, _promo_task
    state.enabled = False
    state.connected = False

    if _promo_task and not _promo_task.done():
        _promo_task.cancel()
        try:
            await _promo_task
        except asyncio.CancelledError:
            pass
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
    if _client:
        try:
            await _client.disconnect()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _client = None

    logger.info("🛑 TikTok Live Chat Bot stopped")
    return {"ok": True, "status": "stopped"}


def get_state() -> dict:
    """Return current bot state for the API."""
    return {
        "enabled": state.enabled,
        "connected": state.connected,
        "room_id": state.room_id,
        "viewer_count": state.viewer_count,
        "comment_count": state.comment_count,
        "gift_count": state.gift_count,
        "like_count": state.like_count,
        "join_count": state.join_count,
        "follow_count": state.follow_count,
        "messages_sent": state.messages_sent,
        "promo_index": state.promo_index,
        "recent_comments": list(state.recent_comments),
        "errors": list(state.errors)[-10:],
    }


# ── FastAPI router helpers ──────────────────────────────────
# These are imported in main.py to expose endpoints.
