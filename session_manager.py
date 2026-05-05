"""
TikTok Session Manager — persistent browser profile + auto cookie refresh.

Keeps a Playwright browser profile alive so TikTok sees consistent fingerprints,
and refreshes cookies before they expire (every ~20h, before 24h expiry).

Architecture:
  1. Creates a persistent browser profile at ~/.config/tiktok-browser/
  2. Launches Playwright with undetected-playwright patches
  3. If not logged in, opens the login page (requires manual QR / credentials once)
  4. Extracts all cookies and saves to tiktok_cookies.json
  5. Runs a health check every hour to verify session validity
  6. Auto-refreshes cookies every 20 hours
  7. Alerts via Telegram if session expires and manual re-login is needed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tiktok-session-manager")

# ── Paths ─────────────────────────────────────────────────────
_DEFAULT_PROFILE_DIR = os.path.expanduser("~/.config/tiktok-browser")
_DEFAULT_COOKIE_OUTPUT = os.path.expanduser("~/tiktok-backend/tiktok_cookies.json")
_TIKTOK_LOGIN_URL = "https://www.tiktok.com/login"
_TIKTOK_LIVE_URL = "https://www.tiktok.com/@{handle}/live"

# ── Telegram Alert ────────────────────────────────────────────
_TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")


class TikTokSessionManager:
    """
    Manages a persistent Playwright browser profile for TikTok session cookies.

    - Keeps a browser profile directory persistent across restarts
    - Refreshes cookies on a schedule (default: every 20h)
    - Health-checks session validity every hour
    - Sends Telegram alerts if session needs manual intervention
    """

    def __init__(
        self,
        profile_dir: str = _DEFAULT_PROFILE_DIR,
        cookie_output: str = _DEFAULT_COOKIE_OUTPUT,
        refresh_interval_hours: float = 20.0,
        health_check_interval_hours: float = 1.0,
        shop_handle: str = "soetsopains",
    ):
        self._profile_dir = profile_dir
        self._cookie_output = cookie_output
        self._refresh_hours = refresh_interval_hours
        self._health_hours = health_check_interval_hours
        self._shop_handle = shop_handle

        self._browser = None
        self._context = None
        self._playwright = None
        self._last_refresh: float = 0.0
        self._last_health_check: float = 0.0
        self._is_healthy: bool = False
        self._cookie_cache: Dict[str, str] = {}

    # ── Browser Management ──────────────────────────────────────

    async def start(self) -> bool:
        """Initialize the persistent browser context."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("[SessionMgr] Playwright not installed")
            return False

        try:
            self._playwright = await async_playwright().start()

            # Launch Chromium with persistent context (profile survives restart)
            # Use stealth-like args to reduce bot detection
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--no-pings",
            ]

            # Use persistent context to keep the profile
            os.makedirs(self._profile_dir, exist_ok=True)

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._profile_dir,
                headless=True,
                args=launch_args,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                # Don't block resources — we want TikTok to fully render
            )

            # Apply basic stealth patches
            for page in self._context.pages:
                await self._apply_stealth_patches(page)

            logger.info(f"[SessionMgr] Browser started with profile: {self._profile_dir}")
            return True

        except Exception as e:
            logger.error(f"[SessionMgr] Failed to start browser: {e}")
            return False

    async def _apply_stealth_patches(self, page) -> None:
        """Apply basic anti-detection patches to the page."""
        try:
            # Remove navigator.webdriver flag
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => false,
                });
                
                // Remove 'cdc_' traces from Chrome
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                
                // Override chrome runtime
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {},
                };
                
                // Fix permissions API
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """)
        except Exception as e:
            logger.debug(f"[SessionMgr] Stealth patch error (non-fatal): {e}")

    # ── Cookie Operations ───────────────────────────────────────

    async def extract_cookies(self) -> Dict[str, str]:
        """Extract all cookies from the browser and save to file."""
        if not self._context:
            logger.error("[SessionMgr] No browser context")
            return {}

        try:
            cookies = await self._context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            # Save in EditThisCookie format for compatibility
            export_list = []
            for c in cookies:
                export_list.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".tiktok.com"),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", True),
                    "httpOnly": c.get("httpOnly", False),
                    "sameSite": c.get("sameSite", "Lax"),
                })

            with open(self._cookie_output, "w") as f:
                json.dump(export_list, f, indent=2)

            self._cookie_cache = cookie_dict
            self._last_refresh = time.time()

            session_id = cookie_dict.get("sessionid", "NONE")
            logger.info(
                f"[SessionMgr] Extracted {len(cookies)} cookies, "
                f"saved to {self._cookie_output}. "
                f"Session: {session_id[:8]}***"
            )

            return cookie_dict

        except Exception as e:
            logger.error(f"[SessionMgr] Cookie extraction failed: {e}")
            return {}

    async def inject_cookies(self, cookies: List[Dict]) -> bool:
        """Inject cookies into the browser context."""
        if not self._context:
            return False

        try:
            await self._context.add_cookies(cookies)
            logger.info(f"[SessionMgr] Injected {len(cookies)} cookies into context")
            return True
        except Exception as e:
            logger.error(f"[SessionMgr] Cookie injection failed: {e}")
            return False

    async def load_and_inject_cookies(self, cookie_path: str = _DEFAULT_COOKIE_OUTPUT) -> bool:
        """Load cookies from file and inject them into the browser context."""
        if not os.path.isfile(cookie_path):
            logger.error(f"[SessionMgr] Cookie file not found: {cookie_path}")
            return False

        try:
            with open(cookie_path, "r") as f:
                raw = json.load(f)
        except Exception as e:
            logger.error(f"[SessionMgr] Failed to read cookie file: {e}")
            return False

        # Convert EditThisCookie format to Playwright format
        playwright_cookies = []
        for c in raw:
            if isinstance(c, dict) and "name" in c:
                playwright_cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".tiktok.com"),
                    "path": c.get("path", "/"),
                    "secure": c.get("secure", True),
                    "httpOnly": c.get("httpOnly", False),
                    "sameSite": c.get("sameSite", "Lax"),
                })

        return await self.inject_cookies(playwright_cookies)

    # ── Session Health ───────────────────────────────────────────

    async def check_session_health(self) -> Dict[str, Any]:
        """Navigate to TikTok and check if the session is valid."""
        if not self._context:
            return {"healthy": False, "reason": "No browser context"}

        try:
            page = self._context.pages[0] if self._context.pages else await self._context.new_page()

            # Navigate to TikTok profile page — if logged in, we see our profile
            await page.goto(
                f"https://www.tiktok.com/@{self._shop_handle}",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await page.wait_for_timeout(3000)

            # Check if we're logged in by looking for login indicators
            # If not logged in, TikTok shows a login button or redirects
            login_button = await page.query_selector('[data-e2e="profile-login"]')
            login_redirect = "login" in page.url.lower()

            if login_button or login_redirect:
                self._is_healthy = False
                result = {"healthy": False, "reason": "Session expired — redirected to login"}
                logger.warning(f"[SessionMgr] {result['reason']}")
                return result

            # Check for sessionid cookie
            cookies = await self._context.cookies()
            session_id = next((c["value"] for c in cookies if c["name"] == "sessionid"), None)

            if not session_id:
                self._is_healthy = False
                return {"healthy": False, "reason": "No sessionid cookie found"}

            self._is_healthy = True
            self._last_health_check = time.time()
            return {
                "healthy": True,
                "session_id_prefix": session_id[:8] + "...",
                "cookie_count": len(cookies),
                "page_url": page.url,
            }

        except Exception as e:
            self._is_healthy = False
            return {"healthy": False, "reason": str(e)}

    # ── Cookie Refresh ──────────────────────────────────────────

    async def refresh_cookies(self) -> bool:
        """
        Refresh session cookies by navigating to TikTok and letting
        the persistent profile naturally extend the session.
        """
        if not self._context:
            logger.error("[SessionMgr] No browser context for refresh")
            return False

        try:
            page = self._context.pages[0] if self._context.pages else await self._context.new_page()

            # Navigate to TikTok to trigger cookie refresh
            # TikTok sets new cookies on each page load
            await page.goto(
                f"https://www.tiktok.com/@{self._shop_handle}/live",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Wait for page to fully load and TikTok to set cookies
            await page.wait_for_timeout(5000)

            # Apply stealth patches to new page
            await self._apply_stealth_patches(page)

            # Extract the fresh cookies
            cookies = await self.extract_cookies()

            if not cookies:
                logger.error("[SessionMgr] Cookie refresh: no cookies extracted")
                return False

            # Verify sessionid was refreshed
            if "sessionid" not in cookies:
                logger.warning("[SessionMgr] Cookie refresh: no sessionid after refresh")
                # Session may have expired — need manual re-login
                await self._alert_session_expired()
                return False

            logger.info("[SessionMgr] ✅ Cookies refreshed successfully")
            return True

        except Exception as e:
            logger.error(f"[SessionMgr] Cookie refresh failed: {e}")
            await self._alert_session_expired()
            return False

    # ── Login Flow ──────────────────────────────────────────────

    async def check_needs_login(self) -> bool:
        """Check if the browser profile needs an initial login."""
        if not self._context:
            return True

        try:
            cookies = await self._context.cookies()
            has_sessionid = any(c["name"] == "sessionid" for c in cookies)
            return not has_sessionid
        except Exception:
            return True

    async def wait_for_login(self, timeout: int = 300) -> bool:
        """
        Open TikTok login page and wait for manual login.
        In headless mode, this requires cookies to be injected from external source.
        
        For headful mode: user scans QR code or enters credentials, 
        then this function detects when login completes.
        """
        if not self._context:
            return False

        try:
            page = await self._context.new_page()
            await page.goto(_TIKTOK_LOGIN_URL, wait_until="domcontentloaded", timeout=20000)

            logger.info("[SessionMgr] Waiting for login (scanning for sessionid cookie)...")

            start = time.time()
            while time.time() - start < timeout:
                cookies = await self._context.cookies()
                if any(c["name"] == "sessionid" for c in cookies):
                    logger.info("[SessionMgr] ✅ Login detected! Extracting cookies...")
                    await self.extract_cookies()
                    return True
                await asyncio.sleep(5)

            logger.warning(f"[SessionMgr] Login timeout ({timeout}s)")
            return False

        except Exception as e:
            logger.error(f"[SessionMgr] Login wait failed: {e}")
            return False

    # ── Maintenance Loop ─────────────────────────────────────────

    async def run_maintenance_loop(self) -> None:
        """
        Background loop that:
        1. Health-checks session every hour
        2. Refreshes cookies every 20 hours
        3. Alerts if session expires
        """
        logger.info(
            f"[SessionMgr] Starting maintenance loop "
            f"(health: {self._health_hours}h, refresh: {self._refresh_hours}h)"
        )

        while True:
            try:
                # Health check
                if time.time() - self._last_health_check > self._health_hours * 3600:
                    health = await self.check_session_health()
                    logger.info(f"[SessionMgr] Health check: {health}")

                # Cookie refresh
                if time.time() - self._last_refresh > self._refresh_hours * 3600:
                    refreshed = await self.refresh_cookies()
                    if not refreshed:
                        logger.error("[SessionMgr] Cookie refresh failed!")
                        await self._alert_session_expired()

            except Exception as e:
                logger.error(f"[SessionMgr] Maintenance loop error: {e}")

            # Sleep 10 minutes between checks
            await asyncio.sleep(600)

    # ── Alerts ──────────────────────────────────────────────────

    async def _alert_session_expired(self) -> None:
        """Send Telegram alert that session needs manual re-login."""
        if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
            logger.warning("[SessionMgr] No Telegram config for alerts")
            return

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": _TELEGRAM_CHAT_ID,
                        "text": (
                            "⚠️ TikTok Session Expired\n\n"
                            "The bot's TikTok session has expired and needs manual re-login.\n"
                            "Cookies need to be exported from a browser where you're logged into TikTok.\n\n"
                            "Run: python3 session_manager.py --inject-cookies"
                        ),
                    },
                )
        except Exception as e:
            logger.error(f"[SessionMgr] Telegram alert failed: {e}")

    # ── Cleanup ──────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the browser and playwright."""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"[SessionMgr] Cleanup error (expected): {e}")


# ── CLI ─────────────────────────────────────────────────────────

async def cli_main():
    """CLI entry point for manual operations."""
    import sys

    mgr = TikTokSessionManager()

    if "--inject-cookies" in sys.argv:
        # Load cookies from the JSON file and inject into browser
        started = await mgr.start()
        if started:
            success = await mgr.load_and_inject_cookies()
            if success:
                # Navigate to verify
                health = await mgr.check_session_health()
                print(f"Session health after injection: {health}")
                # Refresh to extend
                await mgr.refresh_cookies()
            await mgr.close()
        return

    if "--refresh" in sys.argv:
        started = await mgr.start()
        if started:
            # First check if we need login
            needs_login = await mgr.check_needs_login()
            if needs_login:
                print("⚠️ No sessionid found. Injecting cookies from file...")
                await mgr.load_and_inject_cookies()
            
            refreshed = await mgr.refresh_cookies()
            print(f"Cookie refresh: {'✅ success' if refreshed else '❌ failed'}")
            await mgr.close()
        return

    if "--health" in sys.argv:
        started = await mgr.start()
        if started:
            health = await mgr.check_session_health()
            print(f"Session health: {json.dumps(health, indent=2)}")
            await mgr.close()
        return

    # Default: start the persistent manager
    print("Starting TikTok Session Manager (persistent)...")
    started = await mgr.start()
    if not started:
        print("❌ Failed to start browser")
        return

    # Load existing cookies if available
    if os.path.isfile(_DEFAULT_COOKIE_OUTPUT):
        await mgr.load_and_inject_cookies()

    # Check if login is needed
    if await mgr.check_needs_login():
        print("⚠️ Login required. Inject cookies from browser or run with --inject-cookies")
    else:
        # Refresh on startup
        await mgr.refresh_cookies()

    # Run the maintenance loop
    try:
        await mgr.run_maintenance_loop()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        await mgr.close()


if __name__ == "__main__":
    asyncio.run(cli_main())