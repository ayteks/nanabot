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
sys.path.insert(0, os.path.expanduser("~/tiktok-api"))
from TikTokApi import TikTokApi

# ── Logging ───────────────────────────────────────────────
log_path = os.path.expanduser("~/tiktok-backend/backend.log")
logging.basicConfig(
    level=logging.DEBUG,
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
        import re

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


async def _check_live_via_fetch(endpoint: str, label: str) -> tuple[bool, int, str]:
    """
    Strategy 2: Use run_fetch_script to make a direct fetch to a TikTok API endpoint
    through the browser session (with proper cookies/headers).
    """
    if not api:
        return (False, 0, "")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": LIVE_URL,
        "Origin": "https://www.tiktok.com",
    }

    try:
        result = await api.run_fetch_script(endpoint, headers=headers)
        logger.info(f"Fetch [{label}] raw result type: {type(result).__name__}")

        # run_fetch_script returns text from .text(), so parse JSON
        if isinstance(result, str):
            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                logger.info(f"Fetch [{label}] returned non-JSON: {result[:500]}")
                return (False, 0, "")
        elif isinstance(result, dict):
            data = result
        else:
            logger.info(f"Fetch [{label}] unexpected type: {type(result).__name__}")
            return (False, 0, "")

        logger.info(f"Fetch [{label}] response keys: {list(data.keys())[:20]}")
        logger.info(f"Fetch [{label}] response (first 500): {json.dumps(data)[:500]}")

        # Try to extract live info from common response structures
        is_live, viewers, title = _parse_live_response(data)
        if is_live:
            return (is_live, viewers, title)

        return (False, 0, "")

    except Exception as e:
        logger.warning(f"Fetch [{label}] failed: {e}", exc_info=True)
        return (False, 0, "")


def _parse_live_response(data: dict) -> tuple[bool, int, str]:
    """Try to extract live info from any response dict structure."""
    is_live = False
    viewers = 0
    title = ""

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
        user = api.user(SHOP_HANDLE)
        user_data = await user.info()
        user_info = user_data.get("userInfo", {})
        user_obj = user_info.get("user", {})
        avatar_url = (
            user_obj.get("avatarLarger")
            or user_obj.get("avatarMedium")
            or user_obj.get("avatarThumb")
            or ""
        )
    except Exception as e:
        logger.debug(f"Could not fetch avatar/userinfo (continuing): {e}")
        # Use the last known avatar
        avatar_url = cached_live_status.get("avatar_url", "")

    # Try multiple live check strategies (each independent)
    is_live = False
    viewer_count = 0
    title = ""

    # Strategy A: Direct live endpoints via browser fetch
    live_endpoints = [
        "/api/live/detail/",
        "/api/live/room/",
        "/api/live/",
    ]

    for endpoint_suffix in live_endpoints:
        if not is_live:
            endpoint = f"https://www.tiktok.com{endpoint_suffix}?aid=1988&uniqueId={SHOP_HANDLE}"
            try:
                is_live, viewer_count, title = await _check_live_via_fetch(endpoint, endpoint_suffix.strip("/"))
            except Exception as e:
                logger.debug(f"Fetch strategy '{endpoint_suffix}' failed: {e}")

    # Strategy B: Fetch live page HTML via httpx (browserless)
    if not is_live:
        logger.info("API fetch didn't detect live, trying browserless httpx...")
        try:
            is_live, viewer_count, title = await _check_live_via_httpx()
        except Exception as e:
            logger.debug(f"HTTPX strategy failed: {e}")

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
    """Get TikTok user profile information."""
    _verify_api()
    try:
        user = api.user(username=username)
        data = await user.info()
        user_info = data.get("userInfo", {})
        u = user_info.get("user", {})
        s = user_info.get("stats", {})

        return UserInfo(
            username=u.get("uniqueId", username),
            user_id=u.get("id", ""),
            nickname=u.get("nickname", ""),
            bio=u.get("signature", ""),
            avatar=u.get("avatarLarger", u.get("avatarMedium", "")),
            following=s.get("followingCount", 0),
            followers=s.get("followerCount", 0),
            likes=s.get("heartCount", 0),
            videos=s.get("videoCount", 0),
            verified=u.get("verified", False),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user/{username}/videos", response_model=VideoListResponse)
async def get_user_videos(username: str, count: int = 12, cursor: int = 0):
    """Get a user's recent videos."""
    _verify_api()
    try:
        user = api.user(username=username)
        videos = []
        has_more = False
        new_cursor = cursor

        i = 0
        async for video in user.videos(count=count, cursor=cursor):
            if i >= count:
                break
            videos.append(_video_to_info(video))
            new_cursor += 1
            i += 1

        return VideoListResponse(
            videos=videos,
            has_more=len(videos) >= count,
            cursor=new_cursor,
        )
    except Exception as e:
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


@app.get("/api/video/{video_id}", response_model=VideoInfo)
async def get_video_info(video_id: str):
    """Get detailed info about a specific video."""
    _verify_api()
    try:
        return VideoInfo(id="not_implemented_directly", url="")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Debug Endpoint ────────────────────────────────────────
@app.get("/api/debug/live")
async def debug_live():
    """Debug endpoint: show raw live page data."""
    if not api:
        return {"error": "No API instance"}

    results = {}

    # Try page navigation
    try:
        _, session = await api._get_valid_session_index()
    except Exception as e:
        results["session_error"] = str(e)
        return results

    # Navigate to live page
    try:
        await session.page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        title = await session.page.title()
        results["page_title"] = title

        # Get all script tags content
        scripts = await session.page.evaluate("""
            () => {
                const results = {};
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    const id = s.id || 'no-id';
                    // Only capture non-empty scripts
                    if (text.length > 50) {
                        results[id] = {
                            type: s.type || 'unknown',
                            length: text.length,
                            preview: text.substring(0, 300)
                        };
                    }
                }
                return results;
            }
        """)
        results["scripts"] = scripts

        # Body text preview
        body_text = await session.page.evaluate("() => document.body.innerText.substring(0, 1000)")
        results["body_text"] = body_text

        # Check for specific live indicators
        live_indicators = await session.page.evaluate("""
            () => {
                const html = document.documentElement.innerHTML;
                const checks = {
                    hasLIVE: html.includes('LIVE'),
                    has_live: html.includes('"live"'),
                    hasIsLive: html.includes('isLive'),
                    hasRoomId: /room_id|roomId|RoomId/.test(html),
                    hasViewerCount: /viewer_count|viewerCount|viewer_count/.test(html),
                    hasInLiveRoom: html.includes('InLiveRoom'),
                    hasWatchNum: /watch_num|watchingCount/.test(html),
                };
                // Also check all meta tags
                const metas = {};
                document.querySelectorAll('meta').forEach(m => {
                    if (m.getAttribute('property') || m.getAttribute('name')) {
                        metas[m.getAttribute('property') || m.getAttribute('name')] = m.getAttribute('content') || '';
                    }
                });
                return { checks, metas };
            }
        """)
        results["live_indicators"] = live_indicators

    except Exception as e:
        results["nav_error"] = str(e)

    return results


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3100"))
    uvicorn.run("main:app", host=host, port=port, reload=False, log_level="info")
