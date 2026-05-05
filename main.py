"""
SoCandyShop TikTok Backend
==========================
FastAPI service that wraps TikTok-Api (Playwright-based scraper)
for the SoCandyShop Shopify boutique.

Endpoints:
  GET /api/tiktok-live          — Live status & viewer info (backward compat)
  GET /api/user/{username}      — User profile info
  GET /api/user/{username}/videos — User's recent videos
  GET /api/trending             — Trending/FYP videos
  GET /api/hashtag/{tag}        — Hashtag videos
  GET /api/video/{id}           — Video details & comments
  GET /health                   — Backend health check
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional

import httpx

from dotenv import load_dotenv

# Load .env file before anything else
load_dotenv(os.path.expanduser("~/tiktok-backend/.env"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── TikTok API ────────────────────────────────────────────
from TikTokApi import TikTokApi

# ── Live Chat Bot ───────────────────────────────────────────
import live_chat_bot as chatbot
import live_chat_bot_v2 as chatbot_v2

# ── Discord Command Bot ────────────────────────────────────
import discord_commands

# ── Discord Alerts ──────────────────────────────────────────
import discord_alerts

# ── Logging ───────────────────────────────────────────────
log_path = os.path.expanduser("~/tiktok-backend/backend.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("socandyshop-tiktok")

# ── Config ────────────────────────────────────────────────
MS_TOKEN = os.getenv("MS_TOKEN", "")
NUM_SESSIONS = int(os.getenv("TIKTOK_SESSIONS", "2"))
HEADLESS = os.getenv("TIKTOK_HEADLESS", "true").lower() == "true"
LIVE_CHECK_INTERVAL = int(os.getenv("LIVE_CHECK_INTERVAL", "60"))
# SoCandyShop TikTok handle — change this if needed
SHOP_HANDLE = os.getenv("TIKTOK_SHOP_HANDLE", "soetsopains")
PROFILE_URL = f"https://www.tiktok.com/@{SHOP_HANDLE}"
LIVE_URL = f"https://www.tiktok.com/@{SHOP_HANDLE}/live"

# ── Global API Instance ───────────────────────────────────
api: Optional[TikTokApi] = None
cached_live_status = {
    "live": False,
    "viewer_count": 0,
    "title": "",
    "avatar_url": "",
    "checked_at": None,
}


# ── Lifespan ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create TikTok sessions (if enabled). Shutdown: clean up."""
    global api

    if NUM_SESSIONS > 0:
        try:
            api = TikTokApi()

            # Build ms_tokens from env
            ms_tokens = []
            if MS_TOKEN:
                ms_tokens = [MS_TOKEN]
            extra_token = os.getenv("MS_TOKEN_WWW")
            if extra_token and extra_token != MS_TOKEN:
                ms_tokens.append(extra_token)

            # Chromium with stealth args — xvfb provides the display
            display_set = bool(os.getenv("DISPLAY"))
            extra_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if not display_set:
                extra_args.append("--headless=new")

            await api.create_sessions(
                num_sessions=NUM_SESSIONS,
                headless=False,  # xvfb handles the display
                ms_tokens=ms_tokens if ms_tokens else None,
                browser="chromium",
                sleep_after=5,
                override_browser_args=extra_args,
                suppress_resource_load_types=["image", "stylesheet", "font", "media"],
                allow_partial_sessions=True,
                min_sessions=1,
            )
            session_count = len(api.sessions)
            logger.info(f"TikTokApi ready — {session_count}/{NUM_SESSIONS} session(s) created")

            # ── Inject TikTok auth cookies into all sessions ──────────
            cookie_path = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
            if os.path.isfile(cookie_path):
                try:
                    import json
                    with open(cookie_path, "r") as f:
                        raw_cookies = json.load(f)
                    normalized = []
                    for c in raw_cookies:
                        nc = {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain", ".tiktok.com"),
                            "path": c.get("path", "/"),
                        }
                        if "expires" in c and c["expires"] not in (None, -1):
                            nc["expires"] = int(c["expires"])
                        if "sameSite" in c:
                            ss = c["sameSite"]
                            if ss in ("Strict", "Lax", "None"):
                                nc["sameSite"] = ss
                            elif str(ss).lower() in ("no_restriction", "unspecified"):
                                nc["sameSite"] = "None"
                        if "httpOnly" in c:
                            nc["httpOnly"] = bool(c["httpOnly"])
                        if "secure" in c:
                            nc["secure"] = bool(c["secure"])
                        normalized.append(nc)
                    for i, s in enumerate(api.sessions):
                        if s.page and not s.page.is_closed():
                            await s.page.context.add_cookies(normalized)
                            logger.info(f"Session {i}: injected {len(normalized)} auth cookies")
                except Exception as e:
                    logger.warning(f"Cookie injection failed: {e}")

            # Dump session headers for debugging
            for i, s in enumerate(api.sessions):
                logger.info(f"Session {i}: headers keys = {list((s.headers or {}).keys())}")
                logger.info(f"Session {i}: params keys = {list((s.params or {}).keys())}")

        except Exception as e:
            logger.error(f"Failed to init TikTokApi: {e}", exc_info=True)
            api = None
    else:
        logger.info("TikTokApi sessions disabled (TIKTOK_SESSIONS=0) — running browserless mode")
        api = None

    # Start background live-status poller
    poller_task = asyncio.create_task(_background_live_poller())

    # Start live chat bot v2 (if enabled)
    try:
        if os.getenv("BOT_ENABLED", "false").lower() == "true":
            await chatbot_v2.start_bot()
    except Exception as e:
        logger.warning(f"Live chat bot v2 start failed: {e}")

    # Start Discord command bot (if token available)
    try:
        await discord_commands.start_bot_command(
            get_live_status=_get_cached_live_status,
            get_bot_state=_get_cached_bot_state,
            bot_control=_bot_control,
            get_comments=_get_recent_comments,
            get_logs=_get_log_tail,
            restart=_restart_service,
            set_handle=_set_target_handle,
        )
    except Exception as e:
        logger.debug(f"Discord command bot start failed: {e}")

    # Discord startup alert
    try:
        await discord_alerts.alert_backend_started()
    except Exception as e:
        logger.debug(f"Discord startup alert failed: {e}")

    yield

    # Stop Discord command bot
    try:
        await discord_commands.stop_bot_command()
    except Exception:
        pass

    poller_task.cancel()
    # Stop chat bot v2 gracefully
    try:
        await chatbot_v2.stop_bot()
    except Exception:
        pass
    if api and NUM_SESSIONS > 0:
        try:
            await api.close_sessions()
        except Exception:
            pass
        logger.info("TikTokApi sessions closed")


# ── Live Detection — Multiple Strategies ─────────────────

async def _background_live_poller():
    """Periodically check if SoCandyShop is live on TikTok."""
    while True:
        try:
            await _refresh_live_status()
        except Exception as e:
            logger.warning(f"Live status poll failed: {e}")
        await asyncio.sleep(LIVE_CHECK_INTERVAL)


def _load_cookies_from_file() -> dict:
    """Load TikTok cookies from tiktok_cookies.json — no Playwright needed."""
    cookie_path = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
    if not os.path.isfile(cookie_path):
        return {}
    try:
        with open(cookie_path, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
        elif isinstance(raw, dict):
            return raw
    except Exception:
        return {}
    return {}


async def _check_live_via_httpx() -> tuple[bool, int, str]:
    """
    Browserless live check — uses cookies from file (no Playwright needed).
    """
    cookie_jar = _load_cookies_from_file()

    if not cookie_jar:
        logger.warning("No cookie file found — cannot check live status")
        return (False, 0, "")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        }

        async with httpx.AsyncClient(
            cookies=cookie_jar,
            headers=headers,
            follow_redirects=True,
            timeout=30,
        ) as client:
            response = await client.get(LIVE_URL)
            html = response.text

        logger.info(f"HTTPS fetch OK — {len(html)} bytes, status={response.status_code}")

        # Extract from HTML without a browser
        # 1. Page title
        title_match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        page_title = title_match.group(1) if title_match else ""
        live_title = page_title or f"{SHOP_HANDLE} est en live !"

        # 2. LD+JSON VideoObject for viewer count
        viewer_count = 0
        ld_json_matches = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )

        for match in ld_json_matches:
            try:
                obj = json.loads(match)
                if isinstance(obj, dict) and obj.get("@type") == "VideoObject":
                    interactions = obj.get("interactionStatistic", [])
                    for i in interactions:
                        if isinstance(i, dict) and i.get("userInteractionCount"):
                            viewer_count = i["userInteractionCount"]
                            break
                    if obj.get("name"):
                        live_title = obj["name"]
                    break
            except json.JSONDecodeError:
                continue

        if viewer_count:
            logger.info(f"VideoObject (via httpx) has {viewer_count} viewers!")

        # 3. Determine live status — require at least 5 viewers to avoid
        # false positives (stale CDN data, our own checks, etc.)
        title_has_live = "live" in page_title.lower()
        not_offline = (
            "not live" not in page_title.lower()
            and "offline" not in page_title.lower()
        )
        enough_viewers = viewer_count >= 20
        is_live = title_has_live and not_offline and enough_viewers

        if is_live:
            logger.info(f"✅ LIVE DETECTED! viewers={viewer_count} (browserless)")
        else:
            logger.info("HTTPX check: No live indicators found")

        return (is_live, viewer_count, live_title)

    except Exception as e:
        logger.warning(f"HTTPX live check failed: {e}", exc_info=True)
        return (False, 0, "")

    # Pattern 1: LiveRoomInfo with status==2
    if "LiveRoomInfo" in data:
        ri = data["LiveRoomInfo"]
        status = ri.get("status")
        if status == 2 or str(status) == "2":
            is_live = True
            viewers = ri.get("viewer_count", 0) or ri.get("totalUser", 0)
            title = ri.get("title", "")
        logger.info(f"LiveRoomInfo pattern: status={status}, viewers={viewers}, title={title}")

    # Pattern 2: data.status==2 or data.live_status==2
    if not is_live and "data" in data:
        d = data["data"]
        if isinstance(d, dict):
            status = d.get("status") or d.get("live_status")
            if status == 2:
                is_live = True
                viewers = d.get("viewer_count", 0) or d.get("totalUser", 0) or d.get("watch_num", 0)
                title = d.get("title", "")
                logger.info(f"Data.status pattern: status={status}, viewers={viewers}")

    # Pattern 3: room_id exists + status
    if not is_live and "room_id" in data:
        status = data.get("status")
        if status == 2:
            is_live = True
            viewers = data.get("viewer_count", 0)
            title = data.get("title", "")

    # Pattern 4: InLiveRoom = true/false
    if not is_live and data.get("InLiveRoom"):
        is_live = True
        viewers = data.get("viewerCount", 0)

    # Pattern 5: liveRoomInfo (camelCase)
    if not is_live and "liveRoomInfo" in data:
        ri = data["liveRoomInfo"]
        if ri.get("status") == 2:
            is_live = True
            viewers = ri.get("viewerCount", 0)

    return (is_live, viewers, title)


async def _refresh_live_status():
    """Check TikTok live status through multiple strategies, independently."""
    global cached_live_status, api
    if not api:
        cached_live_status["live"] = False
        cached_live_status["checked_at"] = datetime.utcnow().isoformat()
        return

    # Get avatar (best-effort, don't let failure block live detection)
    avatar_url = ""
    try:
        avatar_url = await _scrape_avatar(SHOP_HANDLE)
    except Exception as e:
        logger.debug(f"Could not fetch avatar (continuing): {e}")
        # Use the last known avatar
        avatar_url = cached_live_status.get("avatar_url", "")

    # Browserless httpx check (the only strategy that works)
    try:
        is_live, viewer_count, title = await _check_live_via_httpx()
    except Exception as e:
        logger.debug(f"httpx live check failed: {e}")
        is_live, viewer_count, title = False, 0, ""

    cached_live_status = {
        "live": is_live,
        "viewer_count": viewer_count,
        "title": title,
        "avatar_url": avatar_url,
        "checked_at": datetime.utcnow().isoformat(),
    }
    logger.info(f"Live status updated: live={is_live}, viewers={viewer_count}")
    if is_live:
        logger.info(f"🎉 LIVE DETECTED! Title: {title}")

    # ── Discord Alert on state change ─────────────────────────
    # DISABLED: live start/end alerts turned off per request.
    # If re-enabled, uncomment below:
    # try:
    #     await discord_alerts.alert_tiktok_live(
    #         live=is_live,
    #         viewer_count=viewer_count,
    #         title=title,
    #         avatar_url=avatar_url,
    #         profile_url=PROFILE_URL,
    #         live_url=LIVE_URL,
    #     )
    # except Exception as e:
    #     logger.debug(f"Discord live alert failed: {e}")


# ── FastAPI App ───────────────────────────────────────────
app = FastAPI(
    title="SoCandyShop TikTok Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Response Models ───────────────────────────────────────
class LiveStatusResponse(BaseModel):
    live: bool
    viewer_count: int = 0
    title: str = ""
    avatar_url: str = ""
    profile_url: str = ""
    live_url: str = ""
    checked_at: str = ""


class VideoInfo(BaseModel):
    id: str
    url: str
    description: str = ""
    create_time: str = ""
    likes: int = 0
    comments: int = 0
    shares: int = 0
    views: int = 0
    play_url: str = ""
    cover_url: str = ""
    author_name: str = ""
    author_avatar: str = ""
    music_title: str = ""
    duration: int = 0


class UserInfo(BaseModel):
    username: str
    user_id: str
    nickname: str = ""
    bio: str = ""
    avatar: str = ""
    following: int = 0
    followers: int = 0
    likes: int = 0
    videos: int = 0
    verified: bool = False


class VideoListResponse(BaseModel):
    videos: list[VideoInfo]
    has_more: bool = False
    cursor: int = 0


# ── Helpers ───────────────────────────────────────────────
def _video_to_info(v) -> VideoInfo:
    """Convert a TikTokApi video object to our response model."""
    try:
        d = v.as_dict if hasattr(v, "as_dict") else {}
        stats = d.get("stats", {}) or d.get("statsV2", {})
        author = d.get("author", {})
        music = d.get("music", {})
        video = d.get("video", {})

        return VideoInfo(
            id=d.get("id", ""),
            url=f"https://www.tiktok.com/@{author.get('uniqueId', '')}/video/{d.get('id', '')}",
            description=d.get("desc", ""),
            create_time=(
                datetime.fromtimestamp(int(d["createTime"])).isoformat()
                if d.get("createTime")
                else ""
            ),
            likes=stats.get("diggCount", stats.get("likeCount", 0)),
            comments=stats.get("commentCount", stats.get("commentCount", 0)),
            shares=stats.get("shareCount", stats.get("shareCount", 0)),
            views=stats.get("playCount", stats.get("viewCount", 0)),
            play_url=video.get("playAddr", ""),
            cover_url=video.get("cover", video.get("dynamicCover", "")),
            author_name=author.get("uniqueId", ""),
            author_avatar=author.get("avatarLarger", author.get("avatarMedium", "")),
            music_title=music.get("title", ""),
            duration=video.get("duration", 0),
        )
    except Exception as e:
        logger.warning(f"Failed to convert video: {e}")
        return VideoInfo(id="unknown", url="")


def _parse_number(text: str) -> int:
    """Parse TikTok numbers like 7.6K, 1.2M, '1,234'."""
    if not text:
        return 0
    t = text.strip().replace(",", "")
    if t.endswith("K"):
        return int(float(t[:-1]) * 1000)
    elif t.endswith("M"):
        return int(float(t[:-1]) * 1000000)
    try:
        return int(float(t))
    except ValueError:
        return 0


async def _scrape_user_info(username: str) -> dict:
    """
    Scrape user profile info from tiktok.com/@{username} via Playwright DOM,
    waiting for the profile to fully load (networkidle).
    """
    if not api or not api.sessions:
        raise HTTPException(status_code=503, detail="TikTokApi not ready")

    _, session = await api._get_valid_session_index()
    tmp_page = await session.page.context.new_page()
    try:
        url = f"https://www.tiktok.com/@{username}"
        await tmp_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        html = await tmp_page.content()

        # Try SSR JSON first
        ssr_match = re.search(
            r'<script[^\u003e]*>window\._SSR_HYDRATED_DATA\s*=\s*(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if ssr_match:
            try:
                raw = ssr_match.group(1)
                data = json.loads(raw)
                profile = data.get("ProfilePage", {}).get("userInfo", {})
                user = profile.get("user", {})
                stats = profile.get("stats", {})
                return {
                    "username": user.get("uniqueId", username),
                    "user_id": user.get("id", ""),
                    "nickname": user.get("nickname", ""),
                    "bio": user.get("signature", ""),
                    "avatar": user.get("avatarLarger", user.get("avatarMedium", "")),
                    "following": stats.get("followingCount", 0),
                    "followers": stats.get("followerCount", 0),
                    "likes": stats.get("heartCount", 0),
                    "videos": stats.get("videoCount", 0),
                    "verified": user.get("verified", False),
                }
            except Exception:
                pass

        # Try to extract full user info from __DEFAULT_SCOPE__ JSON (once)
        scope_data: dict = {}
        try:
            scope_match = re.search(
                r'<script[^\u003e]*\u003e\s*(\{[^\u003c]*"__DEFAULT_SCOPE__"[^\u003c]*\})\s*\u003c/script\u003e',
                html, re.DOTALL
            )
            if scope_match:
                scope_data = json.loads(scope_match.group(1))
        except Exception:
            scope_data = {}

        def _re(pattern, default=""):
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            return m.group(1).strip() if m else default

        # --- avatar: try og:image meta, then __DEFAULT_SCOPE__ JSON ---
        avatar = _re(r'<meta[^\u003e]+property="og:image"[^\u003e]+content="([^"]+)"')
        if not avatar and scope_data:
            try:
                user = scope_data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {}).get("user", {})
                avatar = user.get("avatarLarger", user.get("avatarMedium", ""))
            except Exception:
                pass

        # --- nickname, user_id, bio, verified from __DEFAULT_SCOPE__ if available ---
        nickname = ""
        user_id = ""
        bio = ""
        verified = False
        if scope_data:
            try:
                user = scope_data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {}).get("user", {})
                nickname = user.get("nickname", "")
                user_id = user.get("id", "")
                bio = user.get("signature", "")
                verified = user.get("verified", False)
            except Exception:
                pass

        if not nickname:
            nickname = await tmp_page.title()
            nickname = nickname.split("(@")[0].strip() if "(@" in nickname else nickname

        if not bio:
            bio_el = await tmp_page.query_selector('[data-e2e="user-bio"]')
            if bio_el:
                bio = await bio_el.inner_text() or ""
            if not bio:
                bio = "No bio yet."

        body_text = await tmp_page.evaluate("() => document.body.innerText")
        follower_m = re.search(r"([0-9.,]+[KM]?)\s*[Ff]ollowers", body_text)
        following_m = re.search(r"([0-9.,]+[KM]?)\s*[Ff]ollowing", body_text)
        likes_m = re.search(r"([0-9.,]+[KM]?)\s*[Ll]ikes", body_text)

        # Try stats from __DEFAULT_SCOPE__ too
        followers = 0
        following = 0
        likes = 0
        if scope_data:
            try:
                stats = scope_data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {}).get("stats", {})
                followers = stats.get("followerCount", 0)
                following = stats.get("followingCount", 0)
                likes = stats.get("heartCount", 0)
            except Exception:
                pass

        if not followers and follower_m:
            followers = _parse_number(follower_m.group(1))
        if not following and following_m:
            following = _parse_number(following_m.group(1))
        if not likes and likes_m:
            likes = _parse_number(likes_m.group(1))

        return {
            "username": username,
            "user_id": user_id,
            "nickname": nickname or username,
            "bio": bio,
            "avatar": avatar,
            "following": following,
            "followers": followers,
            "likes": likes,
            "videos": 0,
            "verified": verified,
        }
    finally:
        await tmp_page.close()


async def _get_user_videos(username: str, count: int = 12) -> list[VideoInfo]:
    """
    Get a user's videos.
    
    NOTE: TikTok aggressively blocks personalized endpoints (item_list, user/detail)
    for headless/datacenter sessions. The trending endpoint works because it's less
    protected. Without residential proxies or real device emulation, user-specific
    video lists return empty. This is a known limitation.
    
    We attempt DOM scraping as fallback, but TikTok's SPA only renders the video
    grid after a successful API call — which fails on our session.
    
    Returns empty list with graceful degradation.
    """
    if not api or not api.sessions:
        raise HTTPException(status_code=503, detail="TikTokApi not ready")

    # TikTok blocks personalized endpoints for headless sessions.
    # Known limitation — returning empty list gracefully.
    logger.debug(f"Video fetch for @{username}: blocked by TikTok bot detection")
    return []


async def _scrape_user_videos(username: str, count: int = 12) -> list[VideoInfo]:
    """
    Scrape a user's videos from their profile page by scrolling and extracting
    DOM data (title, views, video ID, cover). Uses a temporary page.
    """
    if not api or not api.sessions:
        raise HTTPException(status_code=503, detail="TikTokApi not ready")

    _, session = await api._get_valid_session_index()
    tmp_page = await session.page.context.new_page()
    try:
        url = f"https://www.tiktok.com/@{username}"
        await tmp_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Get avatar from meta tag (once)
        html = await tmp_page.content()
        avatar_match = re.search(
            r'<meta[^\u003e]+property="og:image"[^\u003e]+content="([^"]+)"', html
        )
        avatar = avatar_match.group(1) if avatar_match else ""

        videos: list[VideoInfo] = []
        seen_ids: set[str] = set()

        # Scroll loop – TikTok lazily loads videos as we scroll
        for scroll in range(10):
            # Scrape current DOM state
            cards = await tmp_page.query_selector_all('a[href*="/video/"]')
            for card in cards:
                href = await card.get_attribute("href") or ""
                vid_match = re.search(r"/video/(\d+)", href)
                if not vid_match:
                    continue
                vid_id = vid_match.group(1)
                if vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)

                # Search nearby ancestors for metadata
                desc = ""
                views = ""
                img = ""
                parent = card
                for _ in range(5):
                    parent = await parent.query_selector("xpath=..")
                    if not parent:
                        break
                    if not img:
                        img_el = await parent.query_selector("img")
                        if img_el:
                            img = await img_el.get_attribute("src") or ""
                    if not views:
                        view_el = await parent.query_selector('[data-e2e="video-views"]')
                        if view_el is None:
                            view_el = await parent.query_selector('span[class*="video-count"]')
                        if view_el is None:
                            inner = await parent.inner_text()
                            v_m = re.search(r"([0-9.,]+[KM]?)\s*(?:views?|vues)", inner, re.I)
                            if v_m:
                                views = v_m.group(1)
                        else:
                            views = await view_el.inner_text() or ""
                    if not desc:
                        desc_el = await parent.query_selector('[data-e2e="video-desc"]')
                        if desc_el:
                            desc = await desc_el.inner_text() or ""
                        else:
                            inner = await parent.inner_text()
                            lines = [l.strip() for l in inner.split("\n") if l.strip()]
                            for line in lines:
                                if "views" not in line.lower() and vid_id not in line:
                                    desc = line[:120]
                                    break

                videos.append(
                    VideoInfo(
                        id=vid_id,
                        url=f"https://www.tiktok.com/@{username}/video/{vid_id}",
                        description=desc,
                        views=_parse_number(views),
                        cover_url=img,
                        author_name=username,
                        author_avatar=avatar,
                    )
                )
                if len(videos) >= count:
                    return videos

            # Scroll down
            await tmp_page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        return videos
    finally:
        await tmp_page.close()


async def _scrape_avatar(username: str) -> str:
    """Fast avatar fetch via Playwright page load (uses temporary page)."""
    try:
        _, session = await api._get_valid_session_index()
        tmp_page = await session.page.context.new_page()
        try:
            await tmp_page.goto(
                f"https://www.tiktok.com/@{username}",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(1)
            html = await tmp_page.content()
            # 1) og:image meta
            m = re.search(r'<meta[^\u003e]+property="og:image"[^\u003e]+content="([^"]+)"', html)
            if m:
                return m.group(1)
            # 2) __DEFAULT_SCOPE__ JSON
            scope_m = re.search(
                r'<script[^\u003e]*\u003e\s*(\{[^\u003c]*"__DEFAULT_SCOPE__"[^\u003c]*\})\s*\u003c/script\u003e',
                html, re.DOTALL
            )
            if scope_m:
                scope_data = json.loads(scope_m.group(1))
                user = scope_data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("userInfo", {}).get("user", {})
                return user.get("avatarLarger", user.get("avatarMedium", ""))
            return ""
        finally:
            await tmp_page.close()
    except Exception as e:
        logger.debug(f"Avatar scrape failed: {e}")
        return ""


def _verify_api():
    """Raise 503 if TikTokApi sessions are disabled or not initialized."""
    if NUM_SESSIONS == 0:
        raise HTTPException(
            status_code=503,
            detail="Playwright sessions disabled (TIKTOK_SESSIONS=0). This endpoint requires browser sessions.",
        )
    if not api:
        raise HTTPException(
            status_code=503,
            detail="TikTokApi not initialized. Sessions may not have been created.",
        )


# ── Endpoints ─────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    if NUM_SESSIONS > 0 and api:
        stats = api.get_resource_stats()
        sessions = stats.get("total_sessions", 0)
        valid = stats.get("valid_sessions", 0)
    else:
        sessions = 0
        valid = 0
    return {
        "status": "ok",
        "sessions": sessions,
        "valid_sessions": valid,
        "live_status": cached_live_status["live"],
        "browserless": NUM_SESSIONS == 0,
    }


@app.post("/api/refresh")
async def force_refresh():
    """Force a live-status refresh immediately."""
    await _refresh_live_status()
    return cached_live_status


@app.get("/api/tiktok-live", response_model=LiveStatusResponse)
async def get_tiktok_live():
    """
    Backward-compatible endpoint for the Shopify TikTok Live banner.
    Returns live status with viewer count, title, and avatar.
    """
    status = LiveStatusResponse(
        live=cached_live_status["live"],
        viewer_count=cached_live_status["viewer_count"],
        title=cached_live_status["title"],
        avatar_url=cached_live_status["avatar_url"],
        profile_url=PROFILE_URL,
        live_url=LIVE_URL,
        checked_at=cached_live_status.get("checked_at", ""),
    )
    return status


@app.get("/api/user/{username}", response_model=UserInfo)
async def get_user_info(username: str):
    """Get TikTok user profile information (via Playwright page scraping)."""
    _verify_api()
    try:
        data = await _scrape_user_info(username)
        return UserInfo(**data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User info scrape failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Helpers for Discord Command Bot ─────────────────────────

async def _get_cached_live_status():
    """Return a copy of the current live status dict."""
    return dict(cached_live_status)


async def _get_cached_bot_state():
    """Return a copy of the current chat bot v2 state."""
    return chatbot_v2.get_state()


async def _bot_control(action: str):
    """Start or stop the live chat bot v2."""
    if action == "start":
        return await chatbot_v2.start_bot()
    elif action == "stop":
        return await chatbot_v2.stop_bot()
    return {"ok": False, "error": f"Unknown action: {action}"}


async def _get_recent_comments(n: int = 10):
    """Return the N most recent chat comments."""
    st = chatbot_v2.get_state()
    comments = st.get("recent_comments", [])
    return comments[-n:] if n > 0 else comments


async def _get_log_tail(n: int = 20):
    """Return the last N lines of the backend log."""
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception as e:
        return [f"Error reading log: {e}"]


def _restart_service():
    """Trigger a background restart of the systemd service."""
    import subprocess
    subprocess.Popen(
        ["systemctl", "--user", "restart", "socandyshop-tiktok.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "status": "Restart initiated — will reconnect in ~15s"}


def _set_target_handle(username: str):
    """Update the TikTok target handle in .env and the running process."""
    import re
    env_path = os.path.expanduser("~/tiktok-backend/.env")
    try:
        with open(env_path, "r") as f:
            content = f.read()
        content = re.sub(
            r"^TIKTOK_SHOP_HANDLE=.*$",
            f"TIKTOK_SHOP_HANDLE={username}",
            content,
            flags=re.MULTILINE,
        )
        with open(env_path, "w") as f:
            f.write(content)
        # Update in-memory globals (requires restart for full effect)
        global SHOP_HANDLE, PROFILE_URL, LIVE_URL
        SHOP_HANDLE = username
        PROFILE_URL = f"https://www.tiktok.com/@{username}"
        LIVE_URL = f"https://www.tiktok.com/@{username}/live"
        return {"ok": True, "status": f"Handle set to @{username} — restart for full effect"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Chat Bot Control Endpoints ────────────────────────────

@app.post("/api/chatbot/start")
async def chatbot_start():
    """Start the TikTok live chat bot v2 (real chat posting)."""
    result = await chatbot_v2.start_bot()
    return result


@app.post("/api/chatbot/stop")
async def chatbot_stop():
    """Stop the TikTok live chat bot v2."""
    result = await chatbot_v2.stop_bot()
    return result


@app.get("/api/chatbot/state")
async def chatbot_state():
    """Get current chat bot v2 state and recent chat activity."""
    return chatbot_v2.get_state()


@app.post("/api/chatbot/say")
async def chatbot_say(payload: dict):
    """Manually send a chat message via the bot. Body: {"text": "..."}. Uses HTTP sender first, falls back to DOM poster."""
    text = payload.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text' field")

    room_id = payload.get("room_id")
    st = chatbot_v2.get_state()
    if not room_id and st.get("room_id"):
        room_id = int(st["room_id"])
    if not room_id:
        raise HTTPException(status_code=400, detail="No room_id (bot not connected?)")

    # Strategy 1: HTTP sender
    try:
        from tiktok_chat_sender import TikTokChatSender
        sender = TikTokChatSender()
        loaded = await sender.load_cookies()
        if loaded:
            result = await sender.send_chat(text, room_id=room_id)
            await sender.close()
            return {"ok": result.get("success", False), "method": "http", "detail": result.get("message", "")}
        await sender.close()
    except Exception as e:
        logger.debug(f"HTTP sender failed: {e}")

    # Strategy 2: DOM poster
    try:
        from live_chat_bot_v2 import poster
        if poster:
            result = await poster.post(text)
            return {"ok": result.get("ok"), "method": "dom", "error": result.get("error", None)}
    except Exception as e:
        logger.debug(f"DOM poster failed: {e}")

    raise HTTPException(status_code=503, detail="No chat sender available (neither HTTP nor DOM)")


@app.get("/api/chatbot/sender-status")
async def chatbot_sender_status():
    """Check TikTokChatSender health and session info."""
    try:
        from tiktok_chat_sender import TikTokChatSender
        sender = TikTokChatSender()
        loaded = await sender.load_cookies()
        if not loaded:
            await sender.close()
            return {"available": False, "reason": "Cookies not loaded"}
        health = await sender.check_session()
        info = sender.session_info
        await sender.close()
        return {"available": True, "health": health, "info": info}
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.post("/api/cookies/import")
async def import_cookies(payload: dict):
    """
    Import TikTok session cookies (EditThisCookie format).
    Body: {"cookies": [{"name": "sessionid", "value": "...", "domain": ".tiktok.com", ...}]}
    Saves to tiktok_cookies.json for use by chat sender and browser sessions.
    """
    cookies = payload.get("cookies", [])
    if not cookies:
        raise HTTPException(status_code=400, detail="Missing 'cookies' array")

    cookie_path = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
    try:
        # Validate and normalize
        normalized = []
        for c in cookies:
            if not c.get("name") or not c.get("value"):
                continue
            normalized.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "Lax"),
                "expires": c.get("expires", -1),
            })

        with open(cookie_path, "w") as f:
            json.dump(normalized, f, indent=2)

        return {"ok": True, "saved": len(normalized), "browserless": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cookies/status")
async def cookies_status():
    """Check if cookies file exists and report which auth cookies are present."""
    cookie_path = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
    if not os.path.isfile(cookie_path):
        return {"exists": False, "auth_cookies": {}}

    try:
        with open(cookie_path, "r") as f:
            raw = json.load(f)
        cookies = {c["name"]: c["value"][:8] + "..." for c in raw if isinstance(c, dict) and "name" in c and "value" in c}
        critical = ["sessionid", "tt-target-idc", "odin_tt", "sid_tt", "uid_tt", "msToken", "sid_guard"]
        present = {k: (k in cookies) for k in critical}
        return {"exists": True, "total_cookies": len(cookies), "auth_cookies": present, "session_id_prefix": cookies.get("sessionid")}
    except Exception as e:
        return {"exists": True, "error": str(e)}


# ── Original User / Trending / Hashtag Endpoints ───────────


@app.get("/api/user/{username}/videos", response_model=VideoListResponse)
async def get_user_videos(username: str, count: int = 12, cursor: int = 0):
    """Get a user's recent videos (with graceful fallback for bot detection)."""
    _verify_api()
    try:
        videos = await _get_user_videos(username, count=count)
        return VideoListResponse(
            videos=videos,
            has_more=len(videos) >= count,
            cursor=cursor + len(videos),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"User videos scrape failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/trending", response_model=VideoListResponse)
async def get_trending(count: int = 12):
    """Get trending / For You Page videos."""
    _verify_api()
    try:
        videos = []
        i = 0
        async for video in api.trending.videos(count=count):
            if i >= count:
                break
            videos.append(_video_to_info(video))
            i += 1

        return VideoListResponse(videos=videos, has_more=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hashtag/{tag}", response_model=VideoListResponse)
async def get_hashtag_videos(tag: str, count: int = 12):
    """Get videos for a specific hashtag."""
    _verify_api()
    try:
        tag_obj = api.hashtag(name=tag)
        videos = []
        i = 0
        async for video in tag_obj.videos(count=count):
            if i >= count:
                break
            videos.append(_video_to_info(video))
            i += 1

        return VideoListResponse(videos=videos, has_more=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/video/{video_id}")
async def get_video_info(video_id: str):
    """Get detailed info about a specific video."""
    raise HTTPException(status_code=501, detail="Single video fetch not implemented")


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3100"))
    uvicorn.run("main:app", host=host, port=port, reload=False, log_level="info")
