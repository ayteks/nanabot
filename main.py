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
    """Startup: create TikTok sessions. Shutdown: clean up."""
    global api
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

        # Dump session headers for debugging
        for i, s in enumerate(api.sessions):
            logger.info(f"Session {i}: headers keys = {list((s.headers or {}).keys())}")
            logger.info(f"Session {i}: params keys = {list((s.params or {}).keys())}")

    except Exception as e:
        logger.error(f"Failed to init TikTokApi: {e}", exc_info=True)
        api = None

    # Start background live-status poller
    poller_task = asyncio.create_task(_background_live_poller())

    yield

    poller_task.cancel()
    if api:
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


async def _check_live_via_httpx() -> tuple[bool, int, str]:
    """
    Strategy 1: Browserless live check — fetch HTML via httpx with Playwright
    session cookies. No browser page load means:
    - No audio playing through WSLg/FreeRDP
    - We don't count as a real viewer
    - Much faster (no 5s wait, no Playwright page lifecycle)
    """
    if not api or not api.sessions:
        return (False, 0, "")

    try:
        _, session = await api._get_valid_session_index()
    except Exception:
        logger.warning("No valid session for httpx live check")
        return (False, 0, "")

    try:
        # Extract cookies from the Playwright session
        pw_cookies = await session.page.context.cookies()
        cookie_jar = {c["name"]: c["value"] for c in pw_cookies}

        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
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
        enough_viewers = viewer_count >= 5
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
    """Raise 503 if TikTokApi is not initialized."""
    if not api:
        raise HTTPException(
            status_code=503,
            detail="TikTokApi not initialized. Sessions may not have been created.",
        )


# ── Endpoints ─────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    _verify_api()
    stats = api.get_resource_stats() if api else {}
    return {
        "status": "ok",
        "sessions": stats.get("total_sessions", 0),
        "valid_sessions": stats.get("valid_sessions", 0),
        "live_status": cached_live_status["live"],
    }


@app.post("/api/refresh")
async def force_refresh():
    """Force a live-status refresh immediately."""
    if api:
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
