"""TikTok Live Chat Poster v2 — manages its own authenticated page."""

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("chat_poster")

try:
    from playwright.async_api import Page
    _HAS_PLAYWRIGHT = True
except Exception:
    _HAS_PLAYWRIGHT = False

_SHOP_HANDLE = os.getenv("TIKTOK_SHOP_HANDLE", "soetsopains")
_MAX_MSG_RATE = int(os.getenv("BOT_MAX_MSG_RATE", "3"))
_COOKIE_PATH = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")

_SELECTORS = {
    "input": [
        'div[data-e2e="room-chat-input-field"]',
        '[data-e2e="chat-input"]',
        'textarea[placeholder*="chat" i]',
        'div[contenteditable="true"]',
        'div[contenteditable="plaintext-only"]',
        'input[type="text"]',
        '[role="textbox"]',
    ],
    "send_btn": [
        'button[data-e2e="room-chat-send-button"]',
        '[data-e2e="chat-send-button"]',
        'button[type="submit"]',
        'button svg[class*="SendMessage"]',
        'button:has-text("Send")',
        'button[aria-label*="send" i]',
        'button[aria-label*="envoyer" i]',
    ],
}

class ChatPoster:
    """Posts messages to TikTok Live chat using a dedicated authenticated page.

    The constructor accepts a TikTokApi session to reuse the browser context.
    If no session is available, it creates a standalone browser.
    """

    def __init__(self):
        self.page: Optional[Page] = None
        self._last_post_ts = 0.0
        self._posts_this_minute = 0
        self._minute_start = 0.0
        self._total_posts = 0
        self._errors: list[str] = []

    # ── Public API ────────────────────────────────────────────

    async def bind(self, api_page=None, context=None) -> None:
        """Initialize a dedicated page, navigate to live, and authenticate.
        
        Args:
            api_page: either a Playwright Page directly, or a TikTokApi session object with `.page`.
        """
        from playwright.async_api import async_playwright, Page
        
        # Check if we received a Playwright Page directly
        if api_page and isinstance(api_page, Page):
            self.page = api_page
            if os.path.isfile(_COOKIE_PATH):
                try:
                    await self._inject_cookies_into_context(self.page.context)
                except Exception as e:
                    logger.warning(f"[ChatPoster] cookie inject on existing page failed: {e}")
            try:
                await self._goto_live()
                logger.info(f"[ChatPoster] bound to TikTokApi page: {self.page.url}")
                return
            except Exception:
                pass
        
        # Check if we received a TikTokApi session object
        if api_page and getattr(api_page, 'page', None):
            try:
                self.page = api_page.page
                if os.path.isfile(_COOKIE_PATH):
                    await self._inject_cookies_into_context(self.page.context)
                await self._goto_live()
                logger.info(f"[ChatPoster] bound to TikTokApi session page: {self.page.url}")
                return
            except Exception:
                pass

        # Create standalone browser
        logger.info("[ChatPoster] creating standalone browser page for chat")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self._ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        self.page = await self._ctx.new_page()

        if os.path.isfile(_COOKIE_PATH):
            await self._inject_cookies_into_context(self._ctx)

        await self._goto_live()
        logger.info(f"[ChatPoster] standalone page ready: {self.page.url}")

    async def _goto_live(self) -> None:
        if not self.page or self.page.is_closed():
            return
        url = f"https://www.tiktok.com/@{_SHOP_HANDLE}/live"

        # First, visit TikTok.com so the cookies establish a session
        await self.page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)

        # Then navigate to the live page
        await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(8)  # SPA must mount chat panel

    async def _inject_cookies_into_context(self, ctx) -> None:
        with open(_COOKIE_PATH, "r") as f:
            raw = json.load(f)
        cookies = []
        for c in raw:
            nc = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
            }
            exp = c.get("expires")
            if exp not in (None, -1):
                nc["expires"] = int(exp)
            ss = c.get("sameSite")
            if isinstance(ss, str):
                if ss in ("Strict", "Lax", "None"):
                    nc["sameSite"] = ss
                elif ss.lower() in ("no_restriction", "unspecified"):
                    nc["sameSite"] = "None"
            if "httpOnly" in c:
                nc["httpOnly"] = bool(c["httpOnly"])
            if "secure" in c:
                nc["secure"] = bool(c["secure"])
            cookies.append(nc)
        await ctx.add_cookies(cookies)
        logger.info(f"[ChatPoster] added {len(cookies)} cookies to context")

    async def post(self, text: str) -> dict:
        if not self.page or self.page.is_closed():
            return {"ok": False, "error": "No page bound"}

        now = time.time()
        if now - self._last_post_ts < _MAX_MSG_RATE:
            await asyncio.sleep(_MAX_MSG_RATE - (now - self._last_post_ts))
            now = time.time()

        if now - self._minute_start >= 60:
            self._minute_start = now
            self._posts_this_minute = 0
        if self._posts_this_minute >= 12:
            return {"ok": False, "error": "Per-minute cap reached"}

        result = await self._do_post(text)
        if result["ok"]:
            self._last_post_ts = time.time()
            self._posts_this_minute += 1
            self._total_posts += 1
        else:
            err = result.get("error", "unknown")
            self._errors.append(f"{int(time.time())}: {err}")
            if len(self._errors) > 50:
                self._errors.pop(0)
        return result

    def get_stats(self) -> dict:
        return {
            "total_posts": self._total_posts,
            "posts_this_minute": self._posts_this_minute,
            "last_post_ago": round(time.time() - self._last_post_ts, 1),
            "errors": list(self._errors)[-10:],  # avoid mutation
        }

    async def _do_post(self, text: str) -> dict:
        page = self.page
        if not page:
            return {"ok": False, "error": "page is None"}

        if "/live" not in (page.url or ""):
            logger.info(f"[ChatPoster] redirecting to live page for @{_SHOP_HANDLE}")
            try:
                await self._goto_live()
            except Exception as e:
                return {"ok": False, "error": f"navigate failed: {e}"}

        # Find chat input
        input_el = None
        for sel in _SELECTORS["input"]:
            try:
                input_el = await page.wait_for_selector(sel, timeout=5000)
                if input_el:
                    logger.debug(f"[ChatPoster] input found via {sel}")
                    break
            except Exception:
                continue

        if not input_el:
            return {"ok": False, "error": "chat input not found"}

        # Type
        try:
            await input_el.scroll_into_view_if_needed()
            await input_el.click()
            await asyncio.sleep(0.3)
            await input_el.fill("")
            await asyncio.sleep(0.2)

            tag = await input_el.evaluate("el => el.tagName")
            is_editable = await input_el.evaluate("el => el.isContentEditable")

            if is_editable or str(tag).lower() == "div":
                await input_el.fill(text)
            else:
                await input_el.type(text, delay=30)
            await asyncio.sleep(0.5)
        except Exception as e:
            return {"ok": False, "error": f"type failed: {e}"}

        # Send
        try:
            await input_el.press("Enter")
            await asyncio.sleep(0.6)

            current = ""
            try:
                if is_editable:
                    current = await input_el.evaluate("el => el.innerText") or ""
                else:
                    current = await input_el.evaluate("el => el.value") or ""
            except Exception:
                pass

            if text.strip() in current.strip():
                btn = None
                for sel in _SELECTORS["send_btn"]:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(0.4)
                        break
                if not btn:
                    return {"ok": False, "error": "message stuck, no send button"}
        except Exception as e:
            return {"ok": False, "error": f"send failed: {e}"}

        logger.info(f"[ChatPoster] posted: {text[:60]}")
        return {"ok": True, "error": None}
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.page and not self.page.is_closed():
            try:
                await self.page.close()
            except Exception:
                pass
        if hasattr(self, '_browser') and self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if hasattr(self, '_pw') and self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
