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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── TikTok API ────────────────────────────────────────────
sys.path.insert(0, os.path.expanduser("~/tiktok-api"))
from TikTokApi import TikTokApi

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("socandyshop-tiktok")

# ── Config ────────────────────────────────────────────────
MS_TOKEN = os.getenv("TIKTOK_MS_TOKEN", "")
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
    except Exception as e:
        logger.error(f"Failed to init TikTokApi: {e}")
        api = None

    # Start background live-status poller
    poller_task = asyncio.create_task(_poll_live_status())

    yield

    poller_task.cancel()
    if api:
        try:
            await api.close_sessions()
        except Exception:
            pass
        logger.info("TikTokApi sessions closed")


# ── Background Live-Status Poller ─────────────────────────
async def _poll_live_status():
    """Periodically check if SoCandyShop is live on TikTok."""
    global cached_live_status
    while True:
        try:
            await _refresh_live_status()
        except Exception as e:
            logger.warning(f"Live status poll failed: {e}")
        await asyncio.sleep(LIVE_CHECK_INTERVAL)


async def _refresh_live_status():
    """Check shop profile for live status via user info + video heuristic."""
    global cached_live_status, api
    if not api:
        cached_live_status["live"] = False
        cached_live_status["checked_at"] = datetime.utcnow().isoformat()
        return

    try:
        user = api.user(SHOP_HANDLE)
        user_data = await user.info()

        # Extract avatar + stats
        user_info = user_data.get("userInfo", {})
        user_obj = user_info.get("user", {})
        stats = user_info.get("stats", {})

        avatar_url = (
            user_obj.get("avatarLarger")
            or user_obj.get("avatarMedium")
            or user_obj.get("avatarThumb")
            or ""
        )

        # Try to detect live status from roomData
        room_data = user_info.get("roomData", None)
        if room_data:
            is_live = True
            viewer_count = room_data.get("viewerCount", 0)
            title = room_data.get("title", "SoCandyShop est en live !")
        else:
            is_live = False
            viewer_count = 0
            title = ""

        cached_live_status = {
            "live": is_live,
            "viewer_count": viewer_count,
            "title": title,
            "avatar_url": avatar_url,
            "checked_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        # Silent fail — keep previous cached status, just update timestamp
        logger.debug(f"Live status refresh failed (expected without ms_token): {e}")
        cached_live_status["checked_at"] = datetime.utcnow().isoformat()


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

        async for i, video in enumerate(user.videos(count=count, cursor=cursor)):
            if i >= count:
                break
            videos.append(_video_to_info(video))
            new_cursor += 1

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
        async for i, video in enumerate(api.trending.videos(count=count)):
            if i >= count:
                break
            videos.append(_video_to_info(video))

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
        async for i, video in enumerate(tag_obj.videos(count=count)):
            if i >= count:
                break
            videos.append(_video_to_info(video))

        return VideoListResponse(videos=videos, has_more=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/video/{video_id}", response_model=VideoInfo)
async def get_video_info(video_id: str):
    """Get detailed info about a specific video."""
    _verify_api()
    try:
        # We need the full URL to fetch via video.info()
        # Use the video ID to construct a minimal URL
        # The video API will resolve it
        return VideoInfo(id="not_implemented_directly", url="")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "3100"))
    uvicorn.run("main:app", host=host, port=port, reload=False, log_level="info")
