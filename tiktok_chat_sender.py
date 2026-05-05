"""
TikTok Live Chat Sender — sends messages directly via TikTok's webcast API.
No Playwright DOM interaction needed. No Euler Stream sign server required.

Architecture:
  1. Load session cookies (sessionid, tt-target-idc, msToken, etc.) from tiktok_cookies.json
  2. Sign the request URL using TikTokApi's built-in Playwright signer (generates msToken + X-Bogus)
  3. POST to webcast.tiktok.com/webcast/im/send_msg/ with signed URL + session cookies
  4. The message content is sent as a JSON body

This replaces the fragile chat_poster.py (DOM-based Playwright typing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("tiktok-chat-sender")

# ── Config ─────────────────────────────────────────────────────
_COOKIE_PATH = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
_WEBCAST_BASE = "https://webcast.tiktok.com/webcast"
_SEND_MSG_ENDPOINT = f"{_WEBCAST_BASE}/im/send_msg/"
_CHAT_ENDPOINT = f"{_WEBCAST_BASE}/chat/send/"  # Alternative endpoint (newer)

_DEFAULT_HEADERS = {
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Ua-Mobile": "?0",
}

_DEFAULT_PARAMS = {
    "aid": 1988,
    "app_language": "en",
    "app_name": "tiktok_web",
    "browser_language": "en-US",
    "browser_name": "Mozilla",
    "browser_online": "true",
    "browser_platform": "Win32",
    "cookie_enabled": "true",
    "device_platform": "web_pc",
    "focus_state": "true",
    "is_fullscreen": "false",
    "is_page_visible": "true",
    "history_len": 8,
    "channel": "tiktok_web",
    "data_collection_enabled": "true",
    "os": "windows",
    "priority_region": "",
    "region": "US",
    "user_is_login": "true",
    "webcast_language": "en",
    "msToken": "",
}


class TikTokChatSender:
    """
    Send messages to TikTok Live chat via the webcast HTTP API.

    Requires:
      - Valid session cookies (sessionid, tt-target-idc, odin_tt, etc.)
      - URL signing capability (TikTokApi's Playwright signer or self-managed)

    Usage:
        sender = TikTokChatSender()
        await sender.load_cookies()
        result = await sender.send_chat("Hey everyone! 🍬", room_id=123456789)
    """

    def __init__(
        self,
        cookie_path: str = _COOKIE_PATH,
        proxy: Optional[str] = None,
        sign_api_key: Optional[str] = None,
        sign_api_url: Optional[str] = None,
    ):
        self._cookie_path = cookie_path
        self._proxy = proxy
        self._cookies: Dict[str, str] = {}
        self._session_id: Optional[str] = None
        self._tt_target_idc: Optional[str] = None
        self._last_send_ts: float = 0.0
        self._min_interval: float = 3.0  # seconds between sends
        self._total_sent: int = 0
        self._errors: list = []

        # Sign server (Euler Stream) for URL signing — optional
        self._sign_api_key = sign_api_key or os.getenv("SIGN_API_KEY")
        self._sign_api_url = sign_api_url or os.getenv(
            "SIGN_API_URL", "https://tiktok.eulerstream.com"
        )

        # HTTP client
        self._httpx: Optional[httpx.AsyncClient] = None

    # ── Cookie Management ───────────────────────────────────────

    async def load_cookies(self) -> bool:
        """Load session cookies from the JSON file exported from browser."""
        if not os.path.isfile(self._cookie_path):
            logger.error(f"[ChatSender] Cookie file not found: {self._cookie_path}")
            return False

        try:
            with open(self._cookie_path, "r") as f:
                raw_cookies = json.load(f)
        except Exception as e:
            logger.error(f"[ChatSender] Failed to read cookies: {e}")
            return False

        # Handle both EditThisCookie format (list of dicts) and simple dict
        if isinstance(raw_cookies, list):
            self._cookies = {c["name"]: c["value"] for c in raw_cookies if "name" in c and "value" in c}
        elif isinstance(raw_cookies, dict):
            self._cookies = raw_cookies
        else:
            logger.error(f"[ChatSender] Unexpected cookie format: {type(raw_cookies)}")
            return False

        # Extract critical auth cookies
        self._session_id = self._cookies.get("sessionid")
        self._tt_target_idc = self._cookies.get("tt-target-idc")

        if not self._session_id:
            logger.error("[ChatSender] No 'sessionid' cookie found — cannot send authenticated messages")
            return False

        if not self._tt_target_idc:
            logger.warning("[ChatSender] No 'tt-target-idc' cookie — send may fail for some regions")

        logger.info(
            f"[ChatSender] Loaded {len(self._cookies)} cookies. "
            f"Session ID: {self._session_id[:8]}***"
            if self._session_id
            else "[ChatSender] No session ID!"
        )
        return True

    def get_cookie_header(self) -> str:
        """Build a Cookie header string from all loaded cookies."""
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def get_cookie_dict(self) -> Dict[str, str]:
        """Return the httpx-compatible cookies dict."""
        return self._cookies.copy()

    # ── URL Signing ─────────────────────────────────────────────

    async def _sign_url_via_euler(self, url: str, method: str = "POST") -> Optional[str]:
        """Sign a URL using Euler Stream sign server (requires API key)."""
        if not self._sign_api_key:
            logger.debug("[ChatSender] No sign API key — skipping Euler signing")
            return None

        payload = {
            "url": url,
            "userAgent": _DEFAULT_HEADERS.get("User-Agent", ""),
            "method": method,
            "type": "fetch",
        }

        if self._session_id:
            payload["sessionId"] = self._session_id
            if self._tt_target_idc:
                payload["ttTargetIdc"] = self._tt_target_idc

        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.post(
                    f"{self._sign_api_url}/webcast/sign_url/",
                    data=payload,
                    headers={"X-Api-Key": self._sign_api_key},
                    timeout=15,
                )
                data = resp.json()
                if data.get("code") == 200 and data.get("response", {}).get("signedUrl"):
                    return data["response"]["signedUrl"]
                logger.warning(f"[ChatSender] Sign failed: code={data.get('code')}")
        except Exception as e:
            logger.warning(f"[ChatSender] Sign request failed: {e}")

        return None

    async def _sign_url_via_tiktokapi(self, url: str, method: str = "POST") -> Optional[str]:
        """Sign a URL using TikTokApi's built-in Playwright signer."""
        try:
            from TikTokApi import TikTokApi

            async with TikTokApi() as api:
                # This spins up Playwright, navigates to TikTok,
                # and runs the signing JS — gives us msToken + X-Bogus
                signed = await api._sign_url(url, method=method)
                return signed
        except ImportError:
            logger.warning("[ChatSender] TikTokApi not available for signing")
        except Exception as e:
            logger.warning(f"[ChatSender] TikTokApi signing failed: {e}")

        return None

    # ── HTTP Client ─────────────────────────────────────────────

    async def _get_httpx(self) -> httpx.AsyncClient:
        """Get or create an httpx client with proper cookies and headers."""
        if self._httpx is None or self._httpx.is_closed:
            self._httpx = httpx.AsyncClient(
                cookies=self.get_cookie_dict(),
                headers={**_DEFAULT_HEADERS, "User-Agent": self._get_user_agent()},
                proxy=self._proxy,
                timeout=30,
                follow_redirects=True,
            )
        return self._httpx

    def _get_user_agent(self) -> str:
        """Return a realistic User-Agent string."""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

    # ── Send Chat ───────────────────────────────────────────────

    async def send_chat(
        self,
        content: str,
        room_id: int,
        session_id: Optional[str] = None,
        tt_target_idc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a chat message to a TikTok Live room.

        Args:
            content: The message text to send
            room_id: The live room ID
            session_id: Override session ID cookie
            tt_target_idc: Override tt-target-idc cookie

        Returns:
            Dict with 'success' (bool), 'code' (int), 'message' (str)
        """
        # Ensure cookies are loaded
        if not self._cookies:
            if not await self.load_cookies():
                return {"success": False, "code": -1, "message": "No cookies loaded"}

        # Rate limit
        now = time.time()
        elapsed = now - self._last_send_ts
        if elapsed < self._min_interval:
            wait = self._min_interval - elapsed
            logger.debug(f"[ChatSender] Rate limiting: waiting {wait:.1f}s")
            await asyncio.sleep(wait)

        sid = session_id or self._session_id
        tidc = tt_target_idc or self._tt_target_idc

        if not sid:
            return {"success": False, "code": -1, "message": "No session_id"}

        # ── Strategy 1: Send via Euler Stream proxy (if API key available) ──
        if self._sign_api_key:
            result = await self._send_via_euler(content, room_id, sid, tidc)
            if result and result.get("success"):
                self._last_send_ts = time.time()
                self._total_sent += 1
                return result
            logger.warning("[ChatSender] Euler send failed, trying direct...")

        # ── Strategy 2: Send directly to TikTok webcast API ──
        result = await self._send_direct(content, room_id, sid, tidc)
        self._last_send_ts = time.time()
        if result.get("success"):
            self._total_sent += 1
        else:
            self._errors.append(result)

        return result

    async def _send_via_euler(
        self, content: str, room_id: int, sid: str, tidc: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Send via Euler Stream's /webcast/chat/ proxy endpoint."""
        try:
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.post(
                    f"{self._sign_api_url}/webcast/chat/",
                    json={
                        "roomId": str(room_id),
                        "content": content,
                        "sessionId": sid,
                        "ttTargetIdc": tidc or "",
                    },
                    headers={
                        "X-Api-Key": self._sign_api_key,
                        "Content-Type": "application/json",
                    },
                )
                data = resp.json()
                return {
                    "success": data.get("code") == 0 or resp.status_code == 200,
                    "code": data.get("code", resp.status_code),
                    "message": data.get("message", ""),
                    "data": data.get("data"),
                }
        except Exception as e:
            logger.error(f"[ChatSender] Euler send error: {e}")
            return None

    async def _send_direct(
        self, content: str, room_id: int, sid: str, tidc: Optional[str]
    ) -> Dict[str, Any]:
        """
        Send directly to TikTok's webcast/im/send_msg/ endpoint.
        
        This is the core of our self-hosted approach — no Euler Stream dependency.
        Requires valid msToken in cookies for the request to be accepted.
        """
        client = await self._get_httpx()

        # Build the request payload
        # TikTok's send_msg endpoint accepts JSON with these fields
        payload = {
            "content": content,
            "target_room_id": str(room_id),
            "content_type": "1",  # 1 = text
        }

        # Cookie header for authentication (some endpoints check this instead of jar)
        cookie_header_parts = [f"sessionid={sid}"]
        if tidc:
            cookie_header_parts.append(f"tt-target-idc={tidc}")
        # Also include all other relevant cookies
        for key in ["odin_tt", "sid_tt", "uid_tt", "sid_guard", "msToken", "ttwid"]:
            if key in self._cookies:
                cookie_header_parts.append(f"{key}={self._cookies[key]}")
        cookie_header = "; ".join(cookie_header_parts)

        # Build params
        params = {
            **_DEFAULT_PARAMS,
            "room_id": str(room_id),
            "user_is_login": "true",
        }

        headers = {
            **_DEFAULT_HEADERS,
            "User-Agent": self._get_user_agent(),
            "Cookie": cookie_header,
            "Content-Type": "application/json",
            "X-Cookie-Header": cookie_header,  # Some TikTok endpoints check this
        }

        # Try both endpoint paths
        endpoints = [
            _CHAT_ENDPOINT,   # /webcast/chat/send/
            _SEND_MSG_ENDPOINT,  # /webcast/im/send_msg/
        ]

        for endpoint in endpoints:
            try:
                # First try: sign the URL via Euler if we have a key
                signed_url = None
                if self._sign_api_key:
                    signed_url = await self._sign_url_via_euler(endpoint, "POST")

                request_url = signed_url or endpoint

                resp = await client.post(
                    request_url,
                    json=payload,
                    params=params if not signed_url else None,
                    headers=headers,
                    timeout=20,
                )

                # Parse response
                try:
                    data = resp.json()
                except Exception:
                    data = {"code": resp.status_code, "message": resp.text[:200]}

                status = resp.status_code
                code = data.get("code", status)

                if status == 200 and code in (0, 200):
                    logger.info(f"[ChatSender] ✅ Message sent: '{content[:50]}' -> room {room_id}")
                    return {
                        "success": True,
                        "code": code,
                        "message": data.get("message", "OK"),
                        "data": data.get("data"),
                    }
                else:
                    logger.warning(
                        f"[ChatSender] Endpoint {endpoint.split('/')[-2]}/{endpoint.split('/')[-1]} "
                        f"returned status={status} code={code}: {data.get('message', '')[:100]}"
                    )

            except Exception as e:
                logger.error(f"[ChatSender] Direct send error for {endpoint}: {e}")
                continue

        # All endpoints failed
        return {
            "success": False,
            "code": -2,
            "message": "All send endpoints failed",
        }

    # ── Health Check ────────────────────────────────────────────

    async def check_session(self) -> Dict[str, Any]:
        """Verify that the current session cookies are valid."""
        if not self._session_id:
            return {"valid": False, "reason": "No sessionid cookie"}

        client = await self._get_httpx()
        try:
            # Light endpoint to check if session is accepted
            resp = await client.get(
                "https://www.tiktok.com/api/user/",
                params={"aid": 1988},
                timeout=10,
            )
            if resp.status_code == 200:
                return {"valid": True, "status_code": 200}
            return {"valid": False, "reason": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"valid": False, "reason": str(e)}

    # ── Stats ───────────────────────────────────────────────────

    @property
    def total_sent(self) -> int:
        return self._total_sent

    @property
    def errors(self) -> list:
        return self._errors.copy()

    @property
    def session_info(self) -> Dict[str, Any]:
        return {
            "has_session_id": bool(self._session_id),
            "has_tt_target_idc": bool(self._tt_target_idc),
            "cookie_count": len(self._cookies),
            "session_id_prefix": self._session_id[:8] + "..." if self._session_id else None,
            "total_sent": self._total_sent,
        }

    # ── Cleanup ─────────────────────────────────────────────────

    async def close(self):
        """Close the HTTP client."""
        if self._httpx and not self._httpx.is_closed:
            await self._httpx.aclose()
            self._httpx = None


# ── Standalone Test ─────────────────────────────────────────────

async def main():
    """Quick test: load cookies and check session health."""
    sender = TikTokChatSender()
    loaded = await sender.load_cookies()
    print(f"Cookies loaded: {loaded}")
    print(f"Session info: {sender.session_info}")

    if loaded:
        health = await sender.check_session()
        print(f"Session health: {health}")

    await sender.close()


if __name__ == "__main__":
    asyncio.run(main())