"""
SoCandyShop TikTok Live Chat Bot v2
====================================
Real interactive bot: listens via TikTokLive WebSocket, posts real chat
messages via Playwright (chat_poster.py).

Environment:
  TIKTOK_SHOP_HANDLE   — target streamer unique_id (default: soetsopains)
  BOT_ENABLED          — "true" to auto-start on backend boot (default: false)
  BOT_GREET_NEW_VIEWERS— "true" to greet joiners (default: true)
  BOT_THANK_GIFTS      — "true" to thank gift senders (default: true)
  BOT_REPLY_MENTIONS   — "true" to reply when bot name is mentioned (default: true)
  BOT_PROMO_INTERVAL   — seconds between auto promo messages (default: 300, 0=off)
  BOT_MAX_MSG_RATE     — min seconds between chat posts (default: 3)
  DISCORD_LIVE_WEBHOOK — optional Discord webhook for live events
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("nanabot")

# ── Optional TikTokLive import ────────────────────────────
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
    _HAS_TIKTOKLIVE = True
except Exception as exc:
    _HAS_TIKTOKLIVE = False
    logger.warning(f"TikTokLive library not available: {exc}")

# ── ChatPoster ──────────────────────────────────────────────
try:
    from chat_poster import ChatPoster
    _HAS_POSTER = True
except Exception as exc:
    _HAS_POSTER = False
    logger.warning(f"ChatPoster not available: {exc}")

# ── TikTokChatSender (HTTP-based, no DOM) ──────────────────
try:
    from tiktok_chat_sender import TikTokChatSender
    _HAS_HTTP_SENDER = True
except Exception as exc:
    _HAS_HTTP_SENDER = False
    logger.warning(f"TikTokChatSender not available: {exc}")

# ── LLM Engine ──────────────────────────────────────────────
try:
    from llm_engine import reply_to_comment, is_relevant_comment
    _HAS_LLM = True
except Exception as exc:
    _HAS_LLM = False
    logger.warning(f"LLM engine not available: {exc}")
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
_BOT_MAX_MSG_RATE = int(os.getenv("BOT_MAX_MSG_RATE", "3"))
BOT_MAX_MSG_RATE = max(1, _BOT_MAX_MSG_RATE)
DISCORD_LIVE_WEBHOOK = os.getenv("DISCORD_LIVE_WEBHOOK", "")

# ── Short link — TikTok shop or Shopify store ───────────────
SHOP_SHORT_URL = os.getenv("SHOP_SHORT_URL", "https://socandyshop.com")

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
    share_count: int = 0
    promo_index: int = 0
    last_promo_ts: float = 0.0
    messages_sent: int = 0
    errors: list[str] = field(default_factory=list)
    recent_comments: list[dict] = field(default_factory=list)
    poster_stats: dict = field(default_factory=dict)

state = BotState()

# ── Promo rotation ──────────────────────────────────────────
_PROMO_MESSAGES = [
    "🍬 Hello tout le monde ! C'est Nana de la team socandyshop — si vous aimez les friandises, on est fait(e)s pour se connaître 😄",
    "🎁 On a des bonbons de mal chez nous — surtout les rubans acides, j'ai adoré la première fois 🌈",
    "✨ Moi j'adore les nouveautés, c'est notre dada chez socandyshop ! Vous avez des préférés ?",
    "🍭 Le Crâne BubbleGum Acidulé c'est ma pépite perso — celui qui aime les choses fortes sera servi 🤘",
    "💬 Trop chou le live ! N'hésitez pas à poser des questions sur les bonbons, je connais bien la gamme 😁",
    "🤘 Si vous kiffez les trucs acides, les rubans rainbow c'est le test ultime — perso j'ai les larmes aux yeux à chaque fois",
    "✨ Le petit dernier qui cartonne c'est le Anneaux Arlequin, les goûts mélangés c'est une dinguerie",
]

_GREETINGS = [
    # Disabled — no join greetings per request
]

_THANKS_GIFT = [
    "Merci beaucoup {name} pour le cadeau ! 🎁💖",
    "Wouah {name} merci pour le gift ! Tu es génial(e) ! 🌟",
    "Merci {name} ! Ton soutien fait fondre notre cœur (et nos bonbons) ! 🍫",
    "{name} t’es un(e) vrai(e) fan ! Merci pour le gift ! 🙏",
]

_REPLY_MENTION = [
    "Coucou {name} ! 🍬 Moi c'est Nana, je bosse avec l'équipe socandyshop — t'as une question sur les bonbons ?",
    "Hey {name} ! Ca va ? Je suis de la team socandyshop, t'as envie de découvrir un truc sympa ?",
    "{name} ! Le Anneaux Arlequin c'est mon coup de cœur perso — les goûts de réglisse mélangés, c'est fou 🍭",
    "Salut {name} 😊 Moi c'est Nana de socandyshop — je connais tout le stock sur le bout des doigts !",
    "Hey {name} ! Je suis trop bavarde, mais quand il s'agit de bonbons je peux pas m'arrêter 😂",
]

_REPLY_KEYWORD = {
    "prix": "Oula les prix varient selon les paquets, mais ya des trucs cool pour tous les budgets !",
    "commande": "T'as déjà commandé qq chose de drôle récemment ? Les nouveautés ça arrive vite chez nous 😋",
    "livraison": "On envoie en France métro et en Belgique — c'est rapide en général !",
    "nouveau": "Omg les nouveautés là j'adore — surtout les Bisous Mûre et Melon, c'est une dinguerie 🍬",
    "coréen": "Nan nous on est plus spécialisés dans les bonbons français et européens — les Anneaux Arlequin c'est mon kiff 😋",
    "japonais": "Nos bonbons sont plutôt européens et français — les Dauphin XL par ex sont trop fun 😄",
    "bonbon": "Ahh t'as un préféré toi ? Moi c'est les Crânes BubbleGum Acides, mon vice 🤘",
    "magasin": "Nos produits sont sur socandyshop.fr — tu trouveras tout ce qu'on a en stock !",
    "shop": "Nos produits sont sur socandyshop.fr — tu trouveras tout ce qu'on a en stock !",
}

_THANKS_LIKE = [
    "Merci pour les likes ! ❤️",
    "Vous êtes incroyables avec tous ces likes ! 🔥",
    "Tous ces likes, vous êtes les meilleurs ! 💕",
]

_THANKS_FOLLOW = [
    "Merci pour le follow {name} ! 🎉 Bienvenue dans la famille SoCandyShop !",
    "{name} a rejoint la famille ! Merci pour le follow ! 🍬",
    "Wouah merci {name} ! On est de plus en plus nombreux ! 🙌",
]

# ── Helpers ─────────────────────────────────────────────────

def _pick(template_list: list[str], **fmt) -> str:
    return random.choice(template_list).format(**fmt)

def _format(text: str, **kw) -> str:
    kw.setdefault("url", SHOP_SHORT_URL)
    return text.format(**kw)

def _get_user(event) -> tuple[str, str]:
    user = getattr(event, "user", None)
    nickname = getattr(user, "nickname", "") if user else ""
    unique_id = getattr(user, "unique_id", "") if user else ""
    return nickname, unique_id

# ── Global rate limiter ─────────────────────────────────────
_last_bot_post_ts: float = 0.0
_http_sender: Optional[TikTokChatSender] = None

async def _get_http_sender() -> Optional[TikTokChatSender]:
    """Lazily init the HTTP-based chat sender."""
    global _http_sender
    if not _HAS_HTTP_SENDER:
        return None
    if _http_sender is None:
        _http_sender = TikTokChatSender()
        loaded = await _http_sender.load_cookies()
        if not loaded:
            logger.warning("[ChatSender] cookies not loaded — HTTP sender disabled")
            _http_sender = None
            return None
        logger.info("[ChatSender] HTTP sender initialized ✅")
    return _http_sender

async def _bot_say(text: str, event_type: str = "reply") -> None:
    """Log + attempt real chat post. Prefer HTTP sender, fall back to DOM poster."""
    global _last_bot_post_ts

    now = time.time()
    elapsed = now - _last_bot_post_ts
    if elapsed < BOT_MAX_MSG_RATE:
        logger.info(f"[BOT RATE-LIMITED] {event_type.upper()} blocked — {BOT_MAX_MSG_RATE - elapsed:.1f}s left")
        return
    _last_bot_post_ts = now
    state.messages_sent += 1
    logger.info(f"[BOT {event_type.upper()}] {text}")

    # ── Strategy 1: HTTP sender (no browser, fastest) ──
    sender = await _get_http_sender()
    if sender and state.room_id:
        try:
            result = await sender.send_chat(text, room_id=int(state.room_id))
            if result.get("success"):
                logger.info(f"[ChatSender] ✅ sent via HTTP")
                state.poster_stats = {"method": "http", "sender_stats": sender.session_info}
                return
            else:
                logger.warning(f"[ChatSender] HTTP failed: {result.get('message')}")
        except Exception as e:
            logger.warning(f"[ChatSender] HTTP exception: {e}")

    # ── Strategy 2: DOM-based ChatPoster (fallback) ──
    if poster is not None:
        try:
            result = await poster.post(text)
            if not result.get("ok"):
                logger.warning(f"[ChatPoster] failed to post: {result.get('error')}")
            state.poster_stats = poster.get_stats() if poster else {}
        except Exception as e:
            logger.warning(f"[ChatPoster] exception: {e}")


# ── Dedup cache ─────────────────────────────────────────────
_seen_comment_ids: set[str] = set()
_seen_join_ids: set[str] = set()
_user_last_reply: dict[str, float] = {}   # user -> last reply timestamp

# ── Event Handlers ──────────────────────────────────────────

async def _on_connect(event: "ConnectEvent") -> None:
    state.connected = True
    state.room_id = str(getattr(event, "room_id", ""))
    logger.info(f"🟢 Bot connected to @{SHOP_HANDLE} (room={state.room_id})")
    await _bot_say("🍬 Heyyy ! C'est Nana de la team socandyshop ! Contente de vous voir là 😁", "system")


async def _on_disconnect(event: "DisconnectEvent") -> None:
    state.connected = False
    state.room_id = ""
    logger.info("🔴 Bot disconnected from livestream")


async def _on_comment(event: "CommentEvent") -> None:
    state.comment_count += 1
    nickname, unique_id = _get_user(event)
    comment: str = getattr(event, "comment", "") or ""

    dedup_key = f"{unique_id}:{nickname}:{comment}"
    if dedup_key in _seen_comment_ids:
        return
    _seen_comment_ids.add(dedup_key)
    if len(_seen_comment_ids) > 500:
        _seen_comment_ids.clear()

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

    # ── Keyword-based replies (not just mentions) ─────────────
    lowered = comment.lower()
    user_key = unique_id or nickname

    now = time.time()
    if BOT_REPLY_MENTIONS and user_key:
        # Rate limit: one reply per user per 30s
        if now - _user_last_reply.get(user_key, 0) < 30:
            return

        triggered = False
        reply_text = ""

        # Mention triggers
        mention_triggers = ("bot", "sobot", "socandy", "aide", "help", "?")
        if any(k in lowered for k in mention_triggers):
            reply_text = _pick(_REPLY_MENTION, name=nickname or unique_id or "ami(e)")
            triggered = True

        # Keyword triggers
        if not triggered:
            for kw, tmpl in _REPLY_KEYWORD.items():
                if kw in lowered:
                    reply_text = _format(tmpl, name=nickname or "toi")
                    triggered = True
                    break

        if triggered:
            _user_last_reply[user_key] = now
            # LLM override for richer, contextual replies
            if _HAS_LLM:
                try:
                    llm_reply = await reply_to_comment(
                        nickname or unique_id or "ami",
                        comment,
                        context="TikTok LIVE SoCandyShop",
                    )
                    if llm_reply:
                        reply_text = llm_reply
                except Exception as e:
                    logger.debug(f"[LLM] fallback to template: {e}")
            await _bot_say(reply_text, "reply")
            if _HAS_DISCORD:
                try:
                    await discord_alerts.alert_chat_mention(nickname or unique_id, comment)
                except Exception:
                    pass


async def _on_gift(event: "GiftEvent") -> None:
    state.gift_count += 1
    if not BOT_THANK_GIFTS:
        return
    nickname, unique_id = _get_user(event)
    gift = getattr(event, "gift", None)
    gift_name = getattr(gift, "name", "") if gift else ""
    streaking = getattr(event, "streaking", False)
    repeat = getattr(event, "repeat_count", 1)

    if not streaking:
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
    cnt = getattr(event, "count", 1)
    state.like_count += cnt
    if state.like_count % 50 == 0:
        await _bot_say(random.choice(_THANKS_LIKE), "like")


async def _on_join(event: "JoinEvent") -> None:
    state.join_count += 1
    # Join greetings disabled per request — no chat spam
    return


async def _on_follow(event: "FollowEvent") -> None:
    state.follow_count += 1
    nickname, unique_id = _get_user(event)
    msg = _pick(_THANKS_FOLLOW, name=nickname or unique_id or "toi")
    await _bot_say(msg, "follow")
    if _HAS_DISCORD:
        try:
            await discord_alerts.alert_chat_follow(nickname or unique_id)
        except Exception:
            pass


async def _on_share(event: "ShareEvent") -> None:
    state.share_count += 1
    nickname, _ = _get_user(event)
    await _bot_say(f"Merci {nickname or 'toi'} pour le partage ! 🚀", "share")


# ── Promo loop ──────────────────────────────────────────────
async def _promo_loop() -> None:
    if BOT_PROMO_INTERVAL <= 0:
        return
    while state.enabled:
        await asyncio.sleep(BOT_PROMO_INTERVAL)
        if not state.connected or not state.enabled:
            continue
        msg = _PROMO_MESSAGES[state.promo_index % len(_PROMO_MESSAGES)]
        state.promo_index += 1
        await _bot_say(_format(msg), "promo")


# ── Client lifecycle ────────────────────────────────────────
_client: Optional["TikTokLiveClient"] = None
_bot_task: Optional[asyncio.Task] = None
_promo_task: Optional[asyncio.Task] = None
poster: Optional[ChatPoster] = None


async def start_bot() -> dict:
    """Start the live chat bot."""
    global _client, _bot_task, _promo_task, poster

    if not _HAS_TIKTOKLIVE:
        return {"ok": False, "error": "TikTokLive library not installed"}
    if _bot_task and not _bot_task.done():
        return {"ok": True, "status": "already_running"}

    state.enabled = True
    state.errors.clear()

    # Init ChatPoster — only if HTTP sender is unavailable
    # HTTP sender (tiktok_chat_sender) is preferred and doesn't need a browser
    if not _HAS_HTTP_SENDER:
        if _HAS_POSTER:
            poster = ChatPoster()
            try:
                # ChatPoster creates its own standalone browser with full auth cookies
                await poster.bind()
                logger.info("[ChatPoster] standalone browser ready (fallback mode)")
            except Exception as e:
                logger.warning(f"[ChatPoster] standalone init failed: {e}")
        else:
            logger.warning("[ChatPoster] NOT available — bot will log only")
    else:
        logger.info("[ChatSender] HTTP sender active — skipping ChatPoster browser (saves resources)")

    web_proxy = None
    ws_proxy = None
    proxy_url = os.getenv("BOT_PROXY", "")
    if proxy_url:
        try:
            import httpx
            web_proxy = httpx.Proxy(url=proxy_url)
            ws_proxy = httpx.Proxy(url=proxy_url)
            logger.info(f"Bot using proxy: {proxy_url}")
        except Exception:
            pass

    try:
        _client = TikTokLiveClient(
            unique_id=f"@{SHOP_HANDLE}",
            web_proxy=web_proxy,
            ws_proxy=ws_proxy,
        )
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
                wait = min(30 * (2 ** min(consecutive_errors - 1, 4)), 300)
                logger.warning(f"Bot runner error ({consecutive_errors}): {err_str}. Retry in {wait}s.")
                await asyncio.sleep(wait)

    _bot_task = asyncio.create_task(_bot_runner())
    _promo_task = asyncio.create_task(_promo_loop())
    logger.info("🤖 TikTok Live Chat Bot v2 started")
    return {"ok": True, "status": "started"}


async def stop_bot() -> dict:
    """Stop the bot gracefully."""
    global _client, _bot_task, _promo_task, poster, _http_sender
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
        except Exception:
            pass
        _client = None

    # Clean up HTTP sender
    if _http_sender:
        try:
            await _http_sender.close()
        except Exception:
            pass
        _http_sender = None

    poster = None
    logger.info("🛑 TikTok Live Chat Bot v2 stopped")
    return {"ok": True, "status": "stopped"}


def get_state() -> dict:
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
        "share_count": state.share_count,
        "messages_sent": state.messages_sent,
        "promo_index": state.promo_index,
        "recent_comments": list(state.recent_comments),
        "errors": list(state.errors)[-10:],
        "poster_stats": state.poster_stats,
    }


# ── Rebind poster to new page ─────────────────────────────
async def rebind_poster(page) -> dict:
    """Rebind ChatPoster to a new Playwright page (e.g. after live page reload)."""
    global poster
    if not _HAS_POSTER:
        return {"ok": False, "error": "ChatPoster not available"}
    if poster is None:
        poster = ChatPoster()
    await poster.bind(page)
    return {"ok": True, "stats": poster.get_stats()}
