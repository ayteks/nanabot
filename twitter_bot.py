"""
SoCandyShop Twitter Bot
========================
Playwright-based Twitter/X automation for @Socandyshopfr.
NO Twitter Developer API required — everything runs in-browser.

Features:
  • Auto-post new Shopify products
  • Read mentions & replies
  • Auto-reply using LLM (Nana persona)
  • Monitor keywords & engage
  • Periodic shop promo tweets

Environment:
  TWITTER_HANDLE       — shop Twitter handle (default: Socandyshopfr)
  TWITTER_ENABLED      — "true" to enable (default: false)
  TWITTER_COOKIES_PATH  — path to cookies JSON (default: ~/nanabot/twitter_cookies.json)
  TWITTER_CHECK_INTERVAL — seconds between mention checks (default: 120)
  TWITTER_PROMO_INTERVAL — seconds between promo tweets (default: 3600, 0=off)
  TWITTER_PRODUCT_POLL   — seconds between Shopify product polls (default: 300)
  SHOP_SHORT_URL         — Shopify URL (default: https://socandyshop.fr)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("twitter-bot")

# ── Config ──────────────────────────────────────────────────
HANDLE = os.getenv("TWITTER_HANDLE", "Socandyshopfr")
ENABLED = os.getenv("TWITTER_ENABLED", "false").lower() == "true"
COOKIES_PATH = os.getenv("TWITTER_COOKIES_PATH", os.path.expanduser("~/nanabot/twitter_cookies.json"))
CHECK_INTERVAL = int(os.getenv("TWITTER_CHECK_INTERVAL", "120"))
PROMO_INTERVAL = int(os.getenv("TWITTER_PROMO_INTERVAL", "0"))
PRODUCT_POLL = int(os.getenv("TWITTER_PRODUCT_POLL", "300"))
SHOP_URL = os.getenv("SHOP_SHORT_URL", "https://socandyshop.fr")
DAILY_REPLY_QUOTA = int(os.getenv("TWITTER_DAILY_REPLY_QUOTA", "12"))

# ── SQLite for state ────────────────────────────────────────
DB_DIR = os.path.expanduser("~/nanabot/data")
DB_PATH = os.path.join(DB_DIR, "twitter_bot.db")


def _connect_db() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    conn = _connect_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS known_products (
            handle TEXT PRIMARY KEY,
            title TEXT,
            tweeted_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS replied_tweets (
            tweet_id TEXT PRIMARY KEY,
            author_handle TEXT,
            text TEXT,
            our_reply TEXT,
            replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS posted_tweets (
            tweet_id TEXT PRIMARY KEY,
            text TEXT,
            posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS daily_engagement (
            date TEXT PRIMARY KEY,
            reply_count INTEGER DEFAULT 0,
            promo_count INTEGER DEFAULT 0,
            mention_count INTEGER DEFAULT 0
        );
    """)
    conn.close()


_init_db()

# ── LLM Engine ──────────────────────────────────────────────
try:
    from llm_engine import reply_to_comment
    _HAS_LLM = True
except Exception:
    _HAS_LLM = False
    logger.warning("LLM engine not available for Twitter bot")

# ── Discord alerts ──────────────────────────────────────────
try:
    import discord_alerts
    _HAS_DISCORD = True
except Exception:
    _HAS_DISCORD = False


# ═══════════════════════════════════════════════════════════
# TwitterBot — Playwright browser automation
# ═══════════════════════════════════════════════════════════

class TwitterBot:
    """Automates Twitter/X via Playwright — no API keys needed."""

    def __init__(self):
        self.page = None
        self._pw = None
        self._browser = None
        self._ctx = None
        self._ready = False
        self._last_mention_id: str = ""
        self._last_promo_ts: float = 0
        self._engage_keywords = [
            # Kept for backward compat — engagement now scrapes live trending topics
        ]
        # Universe of topics the bot engages with — if a trend/tweet doesn't
        # match at least one keyword, it's skipped entirely.
        self._topic_universe = [
            # Jeunesse / youth culture
            "jeunesse", "ado", "ados", "teen", "enfant", "enfants", "collège", "lycée",
            # Manga / anime
            "manga", "animé", "anime", "otaku", "shonen", "shônen", "seinen", "manhwa",
            "naruto", "one piece", "onepiece", "dragon ball", "demon slayer", "jujutsu",
            "attack on titan", "snk", "luffy", "zoro", "goku", "tanjiro", "gojo",
            "my hero academia", "bnha", "hunter x", "bleach", "solo leveling",
            "chainsaw man", "sakamoto", "kaiju", "boruto", "dandadan",
            "tokyo revengers", "tokyorevengers", "jujutsu kaisen",
            "scan", "scans", "scan vf", "chapitre",
            # K-pop / K-pop groups
            "kpop", "k-pop", "kpopfr", "bangtan", "bts", "army", "blackpink", "blink",
            "stray kids", "stay", "twice", "onces", "aespa", "newjeans", "le sserafim",
            "itzy", "ive", "txt", "enhypen", "nct", "seventeen", "carat",
            "red velvet", "gidle", "mamamoo", "ATEEZ", "atiny", "straykids",
            "comeback", "mv", "m/v", "teaser", "album", "bias", "fancam", "fancams",
            "idol", "idols", "trainee",
            # Korea / kdrama
            "kdrama", "k-drama", "drama coréen", "drama coréenne", "korean drama",
            "doréenne", "squid game", "webtoon", "manhwa",
            # Asia / Asian culture
            "asie", "asiatique", "corée", "coréen", "coréenne", "japon", "japonais",
            "japonaise", "chinois", "chinoise", "thaï", "vietnamien", "wuxia",
            "culture asiatique", "nourriture asiatique", "ramen", "boba", "bubble tea",
            "matcha", "mochi", "sushi", "ramyeon", "dalgona",
            "hanbok", "kimono", "cosplay", "cosplayfr",
            # Jeux vidéo
            "jeux vidéo", "jeux vidéo", "jeu vidéo", "gaming", "gamer", "gamers",
            "switch", "nintendo", "ps5", "playstation", "xbox", "pc gamer",
            "minecraft", "fortnite", "valorant", "lol", "league of legends",
            "genshin", "zenless", "honkai", "roblox", "zelda", "mario",
            "pokémon", "pokemon", "splatoon", "animal crossing",
            "carte pokémon", "cartes pokémon", "carte pokemon", "cartes pokemon",
            "pokémon card", "pokemon card", "pokemon cards", "pokémon cards",
            "card pokemon", "booster pokemon", "booster pokémon", "etb pokemon",
            "etb pokémon", "elite trainer", "tcg pokemon", "tcg pokémon",
            "pokemon tcg", "pokémon tcg", "pokémon TCG", "pokedex",
            "carte one piece", "cartes one piece", "one piece card", "one piece cards",
            "opcg", "opcgfr", "one piece card game", "booster one piece",
            "carte rare", "cartes rares", "card shop", "collection carte",
            "carte collection", "booster", "etb", "pre-release", "prerelease",
            "carte shiny", "carte v", "carte vmax", "carte ex",
            "full art", "secret rare", "rainbow", "holo",
            "fps", "mmorpg", "rpg", "streamer", "twitch", "esport",
            # Football
            "football", "foot", "footfr", "soccer", "ligue 1", "ligue1",
            "championnat", "classement", "ballon d'or", "mercato",
            "psg", "om", "ol", "asmonaco", "losc", "ogc nice",
            "mbappé", "mbappe", "neymar", "messi", "ronaldo", "dembele",
            "griezmann", "haaland", "vinicius", "salah",
            "champions league", "championsleague", "champion", "coupedefrance", "coupe de france",
            "europa league", "europa", "premier league", "premierleague", "laliga", "serie a", "seriea",
            "equipe de france", "edf", "bleus", "diables rouges",
            "mercato", "transfert", "but", "goal", "penalty", "passe décisive",
            "stade", "supporter", "ultras", "tifo", "winner", "match", "victoire",
            "liguedeschampions", "ligue des champions",
        ]
        self._avoid_words = [
            # Politics & news
            "politi", "élection", "président", "gouvernement", "guerre",
            "attentat", "terror", "manifestation", "grève", "protest",
            "conflit", "crise", "pandémie", "covid", "vaccin",
            "immigrat", "réfugié", "Macron", "Le Pen", "Zemmour",
            # Violence & serious topics
            "mort", "meurtre", "viol", "agression", "armé", "blessé",
            "accident", "incendie", "attaque", "braquage", "kidnapp",
            # Hate & discrimination
            "racis", "homopho", "sexis", "islamopho", "antisémit",
            # Adult / inappropriate
            "porno", "sexe", "sexy", "nude", "OnlyFans", " escort ",
            "strip", "cocaine", "drogue", "cannabis", "extasie",
            # Health & medical
            "diabète", "diabétique", "obésité", "obèse", "carie", "dentaire",
            "régime", " régime ", "calories",
            # Spam & marketing
            "concours", "gagné", "follow back", "abonnés", "followers",
            "promo code", "code promo", "lien bio", "cliquez ici",
            # Negative candy context
            "allergi", "intolérance", "carie dentaire", "mal aux dents",
            "déteste les bonbons", "plus de bonbons",
            # Body-shaming / mean words
            "obèse", "gros", "dégueu", "nul", "bidon", "pourri",
        ]
        self._stats = {
            "posts": 0,
            "replies": 0,
            "mentions_read": 0,
            "products_tweeted": 0,
            "errors": 0,
            "last_activity": None,
        }

    # ── Browser lifecycle ───────────────────────────────────

    async def start(self) -> bool:
        """Launch browser, load cookies, verify login."""
        from playwright.async_api import async_playwright

        logger.info("[TwitterBot] starting browser...")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self._ctx = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="fr-FR",
        )
        self.page = await self._ctx.new_page()

        # Inject cookies
        if os.path.isfile(COOKIES_PATH):
            await self._inject_cookies()
        else:
            logger.warning(f"[TwitterBot] no cookies at {COOKIES_PATH} — bot won't be logged in")

        # Navigate to X to verify session
        try:
            await self.page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(4)

            # Check if we're actually logged in — multiple signals
            url = self.page.url
            page_content = await self.page.content()

            # Strong check: look for login form or redirect
            if "/login" in url or "/i/flow/login" in url:
                logger.error("[TwitterBot] NOT LOGGED IN — redirecting to login page. Need fresh cookies.")
                self._ready = False
                return False

            # Check for logged-in indicators (tweet textbox, profile avatar, etc.)
            has_home_timeline = await self.page.query_selector('div[data-testid="primaryColumn"]')
            has_tweet_box = await self.page.query_selector('a[data-testid="AppTabBar_Home_Link"]')
            has_login_button = await self.page.query_selector('a[data-testid="loginButton"]')

            if has_login_button and not has_home_timeline:
                logger.error("[TwitterBot] NOT LOGGED IN — login button present. Need cookies.")
                self._ready = False
                return False

            if not has_home_timeline and not has_tweet_box:
                logger.error("[TwitterBot] NOT LOGGED IN — no home timeline detected. Need cookies.")
                self._ready = False
                return False

            self._ready = True
            logger.info(f"[TwitterBot] logged in as @{HANDLE} — ready")
            return True
        except Exception as e:
            logger.error(f"[TwitterBot] startup failed: {e}")
            self._ready = False
            return False

    async def stop(self) -> None:
        """Close browser."""
        logger.info("[TwitterBot] stopping...")
        self._ready = False
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.page = None

    async def _inject_cookies(self) -> None:
        """Load cookies from JSON file into browser context."""
        try:
            with open(COOKIES_PATH, "r") as f:
                raw = json.load(f)
            cookies = []
            for c in raw:
                nc = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ".x.com"),
                    "path": c.get("path", "/"),
                }
                exp = c.get("expires")
                if exp not in (None, -1):
                    nc["expires"] = int(exp)
                ss = c.get("sameSite", "")
                if ss in ("Strict", "Lax", "None"):
                    nc["sameSite"] = ss
                if "httpOnly" in c:
                    nc["httpOnly"] = bool(c["httpOnly"])
                if "secure" in c:
                    nc["secure"] = bool(c["secure"])
                cookies.append(nc)
            await self._ctx.add_cookies(cookies)
            logger.info(f"[TwitterBot] injected {len(cookies)} cookies")
        except Exception as e:
            logger.error(f"[TwitterBot] cookie inject failed: {e}")

    def _require_ready(self) -> bool:
        if not self._ready or not self.page or self.page.is_closed():
            return False
        return True

    # ── Posting ─────────────────────────────────────────────

    async def post_tweet(self, text: str) -> dict:
        """Post a tweet. Returns {'ok': bool, 'tweet_id': str|None, 'error': str|None}."""
        if not self._require_ready():
            return {"ok": False, "tweet_id": None, "error": "not ready"}

        if len(text) > 280:
            text = text[:277] + "..."

        logger.info(f"[TwitterBot] post_tweet: attempting to post ({len(text)} chars): {text[:80]}")

        try:
            # Navigate to compose
            logger.info("[TwitterBot] post_tweet: navigating to compose page")
            await self.page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
            logger.info(f"[TwitterBot] post_tweet: compose page loaded, URL = {self.page.url}")

            # Find the compose textbox
            textbox = None
            for sel in [
                'div[role="dialog"] div[role="textbox"]',
                'div[data-testid="tweetTextarea_0"]',
                'div[role="textbox"][data-testid*="tweetTextarea"]',
                'div[role="textbox"]',
            ]:
                try:
                    textbox = await self.page.wait_for_selector(sel, timeout=4000)
                    if textbox:
                        logger.info(f"[TwitterBot] post_tweet: found textbox with selector: {sel}")
                        break
                except Exception:
                    continue

            if not textbox:
                current_url = self.page.url
                logger.error(f"[TwitterBot] post_tweet: compose textbox not found, URL={current_url}")
                return {"ok": False, "tweet_id": None, "error": "compose textbox not found"}

            # Type the tweet
            await textbox.click()
            await asyncio.sleep(0.3)
            await textbox.fill(text)
            await asyncio.sleep(1)
            logger.info(f"[TwitterBot] post_tweet: text filled into compose box")

            # Click send button
            send_btn = None
            for sel in [
                'div[role="dialog"] button[data-testid="tweetButton"]',
                'button[data-testid="tweetButtonInline"]',
                'button[data-testid="tweetButton"]',
            ]:
                try:
                    send_btn = await self.page.wait_for_selector(sel, timeout=3000)
                    if send_btn:
                        logger.info(f"[TwitterBot] post_tweet: found send button with selector: {sel}")
                        break
                except Exception:
                    continue

            if not send_btn:
                logger.error("[TwitterBot] post_tweet: send button not found")
                return {"ok": False, "tweet_id": None, "error": "send button not found"}

            # Check if button is disabled (empty tweet etc.)
            disabled = await send_btn.get_attribute("disabled")
            if disabled:
                logger.error("[TwitterBot] post_tweet: send button is disabled")
                return {"ok": False, "tweet_id": None, "error": "send button disabled"}

            logger.info("[TwitterBot] post_tweet: clicking send button")
            # Use force=True to bypass overlay divs that intercept pointer events
            await send_btn.click(force=True)
            logger.info("[TwitterBot] post_tweet: send button clicked, waiting for response...")
            await asyncio.sleep(3)

            # Check URL after clicking
            current_url = self.page.url
            logger.info(f"[TwitterBot] post_tweet: after click, URL = {current_url}")

            # Try to extract tweet ID from URL or page
            tweet_id = None
            try:
                # Look for the new tweet in the timeline
                await self.page.wait_for_url("**/status/*", timeout=5000)
                current_url = self.page.url
                logger.info(f"[TwitterBot] post_tweet: URL matched status pattern: {current_url}")
                match = re.search(r"/status/(\d+)", current_url)
                if match:
                    tweet_id = match.group(1)
                    logger.info(f"[TwitterBot] post_tweet: extracted tweet_id = {tweet_id}")
                else:
                    logger.warning(f"[TwitterBot] post_tweet: URL contains /status/ but no numeric ID found: {current_url}")
            except Exception as e:
                logger.warning(f"[TwitterBot] post_tweet: could not extract tweet_id from URL ({e}), current URL = {self.page.url}")

            if not tweet_id:
                logger.warning(f"[TwitterBot] post_tweet: tweet_id unknown, tweet may have still been posted. URL = {self.page.url}")

            self._stats["posts"] += 1
            self._stats["last_activity"] = datetime.now(timezone.utc).isoformat()

            # Save to DB
            conn = _connect_db()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO posted_tweets (tweet_id, text) VALUES (?, ?)",
                    (tweet_id or "unknown", text),
                )
                conn.commit()
            finally:
                conn.close()

            logger.info(f"[TwitterBot] post_tweet: SUCCESS — tweet_id={tweet_id or 'unknown'}, text={text[:80]}")

            # Notify Discord
            if _HAS_DISCORD:
                try:
                    await discord_alerts.alert_twitter_post(text, tweet_id)
                except Exception as e:
                    logger.warning(f"[TwitterBot] Discord alert failed: {e}")

            return {"ok": True, "tweet_id": tweet_id, "error": None}

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"[TwitterBot] post failed: {e}")
            return {"ok": False, "tweet_id": None, "error": str(e)}

    async def delete_tweet(self, tweet_id: str) -> dict:
        """Delete a tweet by navigating to it and clicking delete. Returns {'ok': bool, 'error': str|None}."""
        if not self._require_ready():
            return {"ok": False, "error": "not ready"}

        try:
            url = f"https://x.com/Socandyshop/status/{tweet_id}"
            logger.info(f"[TwitterBot] delete_tweet: navigating to {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            # Try to find and click the caret/more menu button on our own tweet
            # Use JavaScript eval for reliability
            clicked_more = await self.page.evaluate("""() => {
                // Find the caret button inside the article tweet
                const article = document.querySelector('article[data-testid="tweet"]');
                if (!article) return 'no article';
                const caret = article.querySelector('button[data-testid="caret"]');
                if (!caret) return 'no caret';
                caret.click();
                return 'clicked';
            }""")
            logger.info(f"[TwitterBot] delete_tweet: more menu click result = {clicked_more}")
            await asyncio.sleep(2)

            if clicked_more != 'clicked':
                return {"ok": False, "error": f"more menu click failed: {clicked_more}"}

            # Now find and click Delete / Supprimer menu item
            clicked_delete = await self.page.evaluate("""() => {
                const menuItems = document.querySelectorAll('[role="menuitem"]');
                for (const item of menuItems) {
                    const text = item.textContent || '';
                    if (text.includes('Supprimer') || text.includes('Delete')) {
                        item.click();
                        return 'clicked: ' + text.trim();
                    }
                }
                // Fallback: check for any element with Delete/Supprimer text
                const allElems = document.querySelectorAll('span, div, a');
                for (const el of allElems) {
                    const text = el.textContent || '';
                    if ((text.trim() === 'Supprimer' || text.trim() === 'Delete') && el.offsetParent !== null) {
                        el.click();
                        return 'clicked_fallback: ' + text.trim();
                    }
                }
                return 'not_found';
            }""")
            logger.info(f"[TwitterBot] delete_tweet: delete menu click result = {clicked_delete}")
            await asyncio.sleep(2)

            if 'not_found' in str(clicked_delete):
                return {"ok": False, "error": f"delete menu item not found: {clicked_delete}"}

            # Confirm deletion in the dialog
            confirmed = await self.page.evaluate("""() => {
                // Look for the confirmation button
                const confirmBtn = document.querySelector('button[data-testid="confirmationSheetConfirm"]');
                if (confirmBtn) {
                    confirmBtn.click();
                    return 'confirmed';
                }
                // Fallback: find button with Delete/Supprimer text in dialog
                const dialog = document.querySelector('div[role="dialog"]');
                if (dialog) {
                    const btns = dialog.querySelectorAll('button');
                    for (const btn of btns) {
                        const text = btn.textContent || '';
                        if (text.includes('Supprimer') || text.includes('Delete')) {
                            btn.click();
                            return 'confirmed_fallback: ' + text.trim();
                        }
                    }
                }
                return 'no_confirm';
            }""")
            logger.info(f"[TwitterBot] delete_tweet: confirm result = {confirmed}")
            await asyncio.sleep(2)

            if 'no_confirm' in str(confirmed):
                return {"ok": False, "error": f"confirm button not found: {confirmed}"}

            logger.info(f"[TwitterBot] deleted tweet {tweet_id}")
            return {"ok": True, "error": None}

        except Exception as e:
            logger.error(f"[TwitterBot] delete_tweet failed: {e}")
            return {"ok": False, "error": str(e)}

    async def reply_to_tweet(self, tweet_url: str, text: str) -> dict:
        """Reply to a specific tweet by URL. Returns {'ok': bool, 'error': str|None}."""
        if not self._require_ready():
            return {"ok": False, "error": "not ready"}

        try:
            # Navigate to the tweet
            await self.page.goto(tweet_url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            # Click reply button
            reply_btn = None
            for sel in [
                'button[data-testid="reply"]',
                'div[role="button"][data-testid="reply"]',
            ]:
                try:
                    reply_btn = await self.page.wait_for_selector(sel, timeout=5000)
                    if reply_btn:
                        break
                except Exception:
                    continue

            if not reply_btn:
                return {"ok": False, "error": "reply button not found"}

            await reply_btn.click(force=True)
            await asyncio.sleep(2)

            # Find the reply textbox
            textbox = None
            for sel in [
                'div[role="dialog"] div[role="textbox"]',
                'div[data-testid="tweetTextarea_0"]',
                'div[role="textbox"]',
            ]:
                try:
                    textbox = await self.page.wait_for_selector(sel, timeout=4000)
                    if textbox:
                        break
                except Exception:
                    continue

            if not textbox:
                return {"ok": False, "error": "reply textbox not found"}

            await textbox.click()
            await asyncio.sleep(0.3)
            await textbox.fill(text)
            await asyncio.sleep(1)

            # Send
            send_btn = None
            for sel in [
                'div[role="dialog"] button[data-testid="tweetButtonInline"]',
                'button[data-testid="tweetButton"]',
            ]:
                try:
                    send_btn = await self.page.wait_for_selector(sel, timeout=3000)
                    if send_btn:
                        break
                except Exception:
                    continue

            if not send_btn:
                return {"ok": False, "error": "send button not found"}

            await send_btn.click(force=True)
            await asyncio.sleep(3)

            self._stats["replies"] += 1
            self._stats["last_activity"] = datetime.now(timezone.utc).isoformat()

            logger.info(f"[TwitterBot] replied to {tweet_url}: {text[:60]}")

            # Discord alert is handled by the caller (has original tweet context)

            return {"ok": True, "error": None}

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"[TwitterBot] reply failed: {e}")
            return {"ok": False, "error": str(e)}

    # ── Reading mentions ─────────────────────────────────────

    async def read_mentions(self, count: int = 10) -> list[dict]:
        """Read recent mentions. Returns list of {tweet_id, author, text, url}."""
        if not self._require_ready():
            return []

        mentions = []
        try:
            url = f"https://x.com/search?q=%40{HANDLE}&f=live"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(3)

            # Parse tweet articles from search results
            articles = await self.page.query_selector_all('article[data-testid="tweet"]')
            for article in articles[:count]:
                try:
                    tweet_data = await self._parse_tweet_article(article)
                    if tweet_data:
                        mentions.append(tweet_data)
                except Exception:
                    continue

            self._stats["mentions_read"] += len(mentions)
            logger.info(f"[TwitterBot] read {len(mentions)} mentions")
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"[TwitterBot] read_mentions failed: {e}")

        return mentions

    async def _parse_tweet_article(self, article) -> Optional[dict]:
        """Extract tweet data from an article element."""
        try:
            # Get tweet text
            text_el = await article.query_selector('div[data-testid="tweetText"]')
            text = ""
            if text_el:
                text = await text_el.inner_text()

            # Get author handle
            user_el = await article.query_selector('div[data-testid="User-Name"] a[role="link"]')
            author = ""
            tweet_url = ""
            if user_el:
                href = await user_el.get_attribute("href") or ""
                author = href.strip("/").split("/")[0] if href else ""
                tweet_url = f"https://x.com{href}" if href else ""

            # Get tweet ID from time element's parent link
            time_el = await article.query_selector("time")
            tweet_id = ""
            if time_el:
                parent_a = await time_el.evaluate_handle("el => el.closest('a')")
                if parent_a:
                    href = await parent_a.get_attribute("href") or ""
                    match = re.search(r"/status/(\d+)", href)
                    if match:
                        tweet_id = match.group(1)
                        if not tweet_url or "/status/" not in tweet_url:
                            tweet_url = f"https://x.com{href}" if href.startswith("/") else href

            # Get reply count from the reply button area
            reply_count = 0
            reply_btn = await article.query_selector('[data-testid="reply"]')
            if reply_btn:
                # The count is usually in a span near the reply button
                count_span = await reply_btn.query_selector("span")
                if count_span:
                    count_text = await count_span.inner_text()
                    try:
                        reply_count = int(count_text.strip().replace("\xa0", "").replace(",", "").replace(" ", ""))
                    except (ValueError, TypeError):
                        # If not a plain number (e.g. "1.2K"), keep 0
                        pass

            return {
                "tweet_id": tweet_id,
                "author": author,
                "text": text,
                "url": tweet_url,
                "reply_count": reply_count,
            }
        except Exception:
            return None

    async def _fetch_parent_context(self, tweet_url: str) -> dict | None:
        """Navigate to a tweet page and extract the parent tweet text if this is a reply.
        Returns dict with keys:
          - 'context': str — formatted parent context string
          - 'restricted': bool — True if the thread has limited visibility (can't see parent)
        Returns None if it's an original tweet (no parent).
        """
        try:
            await self.page.goto(tweet_url, wait_until="domcontentloaded", timeout=12000)
            await asyncio.sleep(random.uniform(2, 4))

            # ── Check for visibility restriction banners ──
            # X shows these when the author limits who can view/reply:
            # - "Who can reply?" / "Qui peut répondre ?"
            # - "This Tweet is from an account that limits who can view" (limited account)
            # - "Ces Tweets ne sont pas disponibles" / "These Tweets are not available"
            page_text = await self.page.inner_text("body")
            restricted_markers = [
                "limit who can view", "limits who can view",
                "qui peut répondre", "qui peut voir",
                "ne sont pas disponibles", "not available",
                "limité", "limited visibility",
                "ce contenu n'est pas disponible", "this content is not available",
                "you're not able to view", "vous ne pouvez pas voir",
                "protected tweets", "tweets protégés",
            ]
            page_lower = page_text.lower()
            if any(marker in page_lower for marker in restricted_markers):
                logger.info(f"[TwitterBot] restricted/limited visibility tweet, skipping: {tweet_url}")
                return {"context": "", "restricted": True}

            # On a tweet detail page, the parent/original tweet appears ABOVE the reply.
            # The page shows multiple article[data-testid="tweet"] — the first one is the parent.
            articles = await self.page.query_selector_all('article[data-testid="tweet"]')
            if not articles:
                # No tweets visible at all — likely restricted
                logger.info(f"[TwitterBot] no tweet articles visible, likely restricted: {tweet_url}")
                return {"context": "", "restricted": True}

            # The LAST article in the thread view is the reply itself.
            # The FIRST article is the parent (original) tweet.
            # If there's only one article, it's an original tweet — no parent.
            if len(articles) < 2:
                logger.debug(f"[TwitterBot] single tweet (no parent context): {tweet_url}")
                return None

            # Parse the first article as the parent
            parent_data = await self._parse_tweet_article(articles[0])
            if parent_data and parent_data.get("text"):
                parent_author = parent_data.get("author", "")
                parent_text = parent_data.get("text", "")
                logger.info(f"[TwitterBot] found parent tweet by @{parent_author}: {parent_text[:80]}")
                return {"context": f"[Tweet original de @{parent_author}] : « {parent_text[:300]} »", "restricted": False}

            # Parent article exists but has no text — likely restricted/incomplete
            logger.info(f"[TwitterBot] parent tweet has no visible text, likely restricted: {tweet_url}")
            return {"context": "", "restricted": True}
        except Exception as e:
            logger.debug(f"[TwitterBot] could not fetch parent context for {tweet_url}: {e}")
            return None

    # ── Auto-reply to mentions ───────────────────────────────

    async def process_mentions(self) -> int:
        """Check mentions and auto-reply to new ones. Returns count of new replies."""
        mentions = await self.read_mentions(count=20)
        if not mentions:
            return 0

        conn = _connect_db()
        replied = 0
        try:
            for m in mentions:
                if not m.get("tweet_id") or not m.get("text"):
                    continue
                # Skip our own tweets
                if m.get("author", "").lower() == HANDLE.lower():
                    continue

                # Already replied?
                row = conn.execute(
                    "SELECT 1 FROM replied_tweets WHERE tweet_id = ?",
                    (m["tweet_id"],),
                ).fetchone()
                if row:
                    continue

                # Generate reply via LLM
                if not _HAS_LLM:
                    logger.debug(f"[TwitterBot] skipping mention (no LLM): {m['text'][:50]}")
                    continue

                # Inject current shop knowledge as context
                shop_ctx = ""
                try:
                    shop = await self._refresh_shop_cache()
                    newest = shop.get("newest", [])[:5]
                    if newest:
                        lines = [f"{p['title']} ({p['price']}€)" for p in newest]
                        shop_ctx = f"Produits du moment : {', '.join(lines)}"
                except Exception:
                    pass

                try:
                    reply_text = await reply_to_comment(
                        m.get("author", "someone"),
                        m["text"],
                        context=shop_ctx if shop_ctx else None,
                    )
                except Exception as e:
                    logger.warning(f"[TwitterBot] LLM reply failed: {e}")
                    continue

                if not reply_text or len(reply_text.strip()) < 2:
                    continue

                # Actually reply
                result = await self.reply_to_tweet(m["url"], reply_text)

                if result.get("ok"):
                    # Notify Discord with mention context
                    if _HAS_DISCORD:
                        try:
                            await discord_alerts.alert_twitter_reply(
                                our_reply=reply_text,
                                original_text=m.get("text", ""),
                                original_author=m.get("author", ""),
                                tweet_url=m["url"],
                                keyword="mention",
                            )
                        except Exception as e:
                            logger.warning(f"[TwitterBot] Discord mention alert failed: {e}")

                    conn.execute(
                        "INSERT OR IGNORE INTO replied_tweets (tweet_id, author_handle, text, our_reply) VALUES (?, ?, ?, ?)",
                        (m["tweet_id"], m.get("author", ""), m["text"], reply_text),
                    )
                    conn.commit()
                    _incr_daily_count("reply_count")
                    _incr_daily_count("mention_count")
                    replied += 1
                    # Rate limit: wait between replies
                    await asyncio.sleep(random.uniform(8, 15))
        finally:
            conn.close()

        if replied:
            logger.info(f"[TwitterBot] replied to {replied} new mentions")
        return replied

    # ── Trending engagement ───────────────────────────────────

    async def monitor_keywords(self, keywords: list[str] = None) -> list[dict]:
        """Scrape Twitter trending topics and search them for tweets.
        Filters out politics, heavy topics, and OLD tweets (>3h).
        """
        if not self._require_ready():
            return []

        # Instead of hardcoded keywords, scrape live trending topics
        trends = await self._scrape_trends()
        if trends:
            logger.info(f"[TwitterBot] found {len(trends)} trending topics: {trends[:10]}")
        else:
            logger.info("[TwitterBot] no trends found, skipping engagement")
            return []

        # Only reply to tweets from the last 3 hours
        _MAX_AGE_HOURS = 3
        _now = datetime.now(timezone.utc)

        results = []
        for kw in trends[:10]:  # cap at 10 trends to avoid over-scraping
            try:
                # Search recent French tweets for this trend, exclude our own
                query = f'"{kw}" -from:{HANDLE} lang:fr'
                url = f"https://x.com/search?q={_url_encode(query)}&f=live"
                await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(random.uniform(3, 5))

                articles = await self.page.query_selector_all('article[data-testid="tweet"]')
                for article in articles[:12]:  # check more tweets to find highly engaged ones
                    try:
                        tweet_data = await self._parse_tweet_article(article)
                        if not tweet_data or tweet_data.get("author", "").lower() == HANDLE.lower():
                            continue
                        # Skip old tweets — parse the <time> datetime attribute
                        time_el = await article.query_selector("time")
                        if time_el:
                            dt_str = await time_el.get_attribute("datetime") or ""
                            try:
                                tweet_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                                age_hours = (_now - tweet_dt).total_seconds() / 3600
                                if age_hours > _MAX_AGE_HOURS:
                                    logger.debug(f"[TwitterBot] skipping old tweet ({age_hours:.1f}h): {tweet_data.get('text', '')[:60]}")
                                    continue
                            except (ValueError, TypeError):
                                # Can't parse date — check the displayed text for "h" (e.g. "2h")
                                time_text = await time_el.inner_text() if time_el else ""
                                if _is_old_time_text(time_text):
                                    logger.debug(f"[TwitterBot] skipping old tweet (time text: '{time_text}'): {tweet_data.get('text', '')[:60]}")
                                    continue
                        # Skip political/heavy content
                        text_lower = (tweet_data.get("text") or "").lower()
                        if any(avoid in text_lower for avoid in self._avoid_words):
                            logger.debug(f"[TwitterBot] skipping political/heavy tweet: {text_lower[:60]}")
                            continue
                        # Skip non-French tweets (simple heuristic: must contain French common words or accents)
                        text = tweet_data.get("text") or ""
                        french_markers = ("é", "è", "ê", "ç", "à", "ù", "î", "ô", "ï",
                                          "bon", "tres", "fait", "comme", "dans", "pour",
                                          "avec", "sur", "mais", "des", "les", "une",
                                          "pas", "qui", "est", "sont", "avez", "avoir",
                                          "veux", "trop", "pourquoi", "quand", "comment")
                        if not any(m in text.lower() for m in french_markers):
                            logger.debug(f"[TwitterBot] skipping non-French tweet: {text[:60]}")
                            continue
                        # ── Universe filter: only engage tweets in our target topics ──
                        if self._topic_universe:
                            # Check both the tweet text AND the keyword (trend name) against universe
                            combined = f"{text_lower} {kw.lower()}"
                            if not _is_in_universe(combined, self._topic_universe):
                                logger.debug(f"[TwitterBot] skipping off-universe tweet: {text_lower[:60]}")
                                continue
                        tweet_data["keyword"] = kw
                        results.append(tweet_data)
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"[TwitterBot] trend monitor failed for '{kw}': {e}")

        # Sort by reply_count descending — prioritize high-engagement tweets
        # Give extra weight to tweets with significant engagement (more than 3 replies)
        results.sort(key=lambda r: (
            r.get("reply_count", 0) * 2 if r.get("reply_count", 0) > 3 else r.get("reply_count", 0),
            r.get("text", "")  # Secondary sort by text for consistency
        ), reverse=True)

        # Filter out zero-reply tweets only if we have many options
        # But keep some trending topics even without replies to increase engagement opportunities
        if len(results) >= 15:
            filtered = [r for r in results if r.get("reply_count", 0) > 0 or r.get("keyword", "").startswith("#")]
            logger.info(f"[TwitterBot] trend monitor: {len(results)} tweets, {len(filtered)} with replies or hashtags (filtering out low-engagement)")
            results = filtered
        else:
            logger.info(f"[TwitterBot] trend monitor: fewer than 15 results ({len(results)}), keeping all")

        logger.info(f"[TwitterBot] trend monitor found {len(results)} recent tweets (after reply filter)")
        return results

    async def _scrape_trends(self) -> list[str]:
        """Navigate to Twitter Explore/Trending page and scrape trending topic names.
        Returns a list of trending topic strings.
        """
        if not self._require_ready():
            return []

        trends = []
        try:
            await self.page.goto("https://x.com/explore", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(random.uniform(3, 5))

            # Known sidebar / nav items that must never be treated as trends
            _nav_items = {
                "accueil", "explorer", "notifications", "suivre", "chat",
                "grok", "signets", "creator studio", "profil", "communauté",
                "paramètres", "se déconnecter", "plus", "moins",
                "pour voir les raccourcis clavier, appuyez sur le point d'interrogation",
                "voir les raccourcis clavier", "messages", "listes",
            }

            # Try multiple selectors for trending items — X UI changes often
            for selector in [
                'div[data-testid="trend"]',           # classic trend container
                'a[href*="/trending/"]',               # trend links
                'section[aria-label] div[role="link"]', # trend section items
                'div.r-1vr29vt span',                  # fallback: trend text spans
                'span.css-1jxf684.r-bcqeeo.r-qvutc0', # another known trend text class
            ]:
                try:
                    elements = await self.page.query_selector_all(selector)
                    for el in elements[:20]:
                        text = (await el.inner_text()).strip()
                        # Clean up: take just the trend name, skip category labels
                        lines = text.split("\n")
                        for line in lines:
                            line = line.strip()
                            lower = line.lower()
                            # Filter out noise: category labels (contain ·), numbers, too short/long, nav items
                            if (line
                                and len(line) > 2
                                and len(line) < 80
                                and "·" not in line              # category labels like "Sport · Tendances"
                                and not line.startswith(("Tendances", "Trending", "Tendance"))
                                and not any(avoid in lower for avoid in self._avoid_words)
                                and not line.replace(".", "").replace(",", "").replace("K", "").replace("M", "").strip().isdigit()
                                and "publications" not in lower
                                and lower not in _nav_items
                                and not any(nav in lower for nav in ("raccourcis clavier",))
                            ):
                                if line not in trends:
                                    trends.append(line)
                    if trends:
                        break  # found trends with this selector, stop trying others
                except Exception:
                    continue

            # Fallback: if no trends found, try the explore tab directly
            if not trends:
                try:
                    await self.page.goto("https://x.com/explore", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(random.uniform(3, 5))
                    # Look for anything that looks like a hashtag or trending phrase
                    all_spans = await self.page.query_selector_all('span')
                    seen = set()
                    for span in all_spans[:100]:
                        text = (await span.inner_text()).strip()
                        if (text
                            and text.startswith("#")
                            and text not in seen
                            and len(text) < 60
                            and not any(avoid in text.lower() for avoid in self._avoid_words)
                        ):
                            seen.add(text)
                            trends.append(text)
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[TwitterBot] trend scraping failed: {e}")

        # ── Universe filter: only keep trends in our target topics ──
        if self._topic_universe and trends:
            before = len(trends)
            filtered = [t for t in trends if _is_in_universe(t, self._topic_universe)]
            logger.info(f"[TwitterBot] universe filter: {before} trends → {len(filtered)} in-topic (manga/kpop/gaming/Asie/etc)")
            if filtered:
                trends = filtered
            else:
                logger.info("[TwitterBot] no trends match our topic universe, widening to all non-avoided trends")

        return trends

    async def engage_keywords(self, keywords: list[str] = None) -> int:
        """Search trending/fun keywords and like + reply. Returns reply count.
        Only engages on lighthearted content — never politics.
        Only likes tweets we actually reply to.
        
        Prioritizes tweets with existing engagement (replies/comments) to join 
        ongoing conversations rather than starting new ones.
        """
        tweets = await self.monitor_keywords(keywords)
        if not tweets:
            return 0

        MAX_ENGAGED_PER_CYCLE = 15

        daily_left = _daily_quota_remaining()
        if daily_left <= 0:
            logger.info(f"[TwitterBot] daily reply quota exhausted ({DAILY_REPLY_QUOTA}/day), skipping engagement")
            return 0
        logger.info(f"[TwitterBot] daily quota: {daily_left} replies remaining today")

        conn = _connect_db()
        replied = 0
        try:
            for t in tweets:
                if not t.get("tweet_id") or not t.get("url"):
                    continue

                # Already engaged?
                row = conn.execute(
                    "SELECT 1 FROM replied_tweets WHERE tweet_id = ?",
                    (t["tweet_id"],),
                ).fetchone()
                if row:
                    continue

                # Generate fun Nana-style reply FIRST — decide if we engage at all
                if not _HAS_LLM:
                    continue

                # Inject shop context for natural product mentions
                shop_ctx = ""
                try:
                    shop = await self._refresh_shop_cache()
                    newest = shop.get("newest", [])[:3]
                    if newest:
                        lines = [f"{p['title']} ({p['price']}€)" for p in newest]
                        shop_ctx = f"Produits du moment : {', '.join(lines)}"
                except Exception:
                    pass

                try:
                    # ── Fetch parent tweet context if this is a reply ──
                    parent_ctx = ""
                    tweet_url = t.get("url", "")
                    if tweet_url:
                        parent_result = await self._fetch_parent_context(tweet_url)
                        # parent_result is now a dict {context, restricted} or None
                        if parent_result is not None and parent_result.get("restricted"):
                            logger.info(f"[TwitterBot] skipping tweet with restricted visibility: {t.get('text', '')[:60]}")
                            continue
                        if parent_result and parent_result.get("context"):
                            parent_ctx = f"\n\nContexte : ce tweet est une réponse. {parent_result['context']}"

                    # Generate reply — react like a normal person, not a brand robot
                    tweet_text_preview = t.get('text', '')[:200]
                    prompt = (
                        f"Tu vois ce tweet : « {tweet_text_preview} » de @{t.get('author', 'someone')}."
                        f"{parent_ctx}"
                        f"\n\nRéagis NATURELLEMENT, comme une ado qui scrolle Twitter et tombe sur ce tweet. "
                        f"Dis ce que tu aurais envie de dire — un truc fun, mignon, relatable, ou juste un petit mot de soutien. "
                        f"NE PARLE PAS de bonbons, sucreries, ou boutique SAUF si le tweet en parle déjà. "
                        f"Tu es Nana, 2 ans et demi, team SoCandyShop — mais tu es AVANT TOUT quelqu'un de cool qui discute normalement. "
                        f"Si t'as rien de naturel à répondre, mets juste 'PASS'. "
                        f"Max 200 caractères. JAMAIS de lien, 'site web', 'boutique', '.com', ou promo. "
                        f"INTERDIT : insulter, moquer, utiliser 'obèse', 'gros', 'dégueu', ou tout mot blessant."
                    )
                    system = (
                        "Tu es Nana, 2 ans et demi, team SoCandyShop. Tu parles comme une ado française — cool, bienveillante, naturelle. "
                        "Tu scrolles Twitter comme tout le monde : tu réagis aux trucs fun, mignons, relatables. "
                        "TU NE ES PAS UNE MARQUE. Tu es une ado qui discute. "
                        "Ne mentionne PAS les bonbons, la boutique, ou socandyshop sauf si le tweet en parle déjà. "
                        "JAMAIS de moquerie, insulte, mot blessant (obèse, gros, dégueu, nul…). "
                        "JAMAIS de politique, drama, ou sujet sensible. "
                        "JAMAIS de lien, 'site web', 'boutique' ou '.com' dans les replies d'engagement. "
                        "Tes réponses doivent avoir un rapport direct et naturel avec le tweet original. "
                        "Si tu n'as rien de naturel à dire, réponds simplement 'PASS'. "
                        f"{shop_ctx}"
                    )
                    reply_text = await self._llm_generate(system, prompt, max_tokens=250)
                    if not reply_text or len(reply_text.strip()) < 3:
                        logger.debug(f"[TwitterBot] no reply generated for @{t.get('author', '?')}, skipping")
                        continue
                    # If LLM decided to pass, skip this tweet
                    if reply_text.strip().upper() in ("PASS", "PASS.", "PASSE", "PASSE."):
                        logger.info(f"[TwitterBot] LLM passed on tweet: {t.get('text', '')[:60]}")
                        continue
                except Exception:
                    continue

                # Log what the LLM generated before any filtering
                logger.info(f"[TwitterBot] engagement reply for @{t.get('author', '?')}: « {reply_text[:100]} » (original: « {t.get('text', '')[:80]} »)")

                # Double-check: no political/bad content in our reply
                reply_lower = reply_text.lower()
                if any(avoid in reply_lower for avoid in self._avoid_words):
                    logger.warning(f"[TwitterBot] filtered bad reply: {reply_text[:80]}")
                    continue

                # Only like if we're going to reply — like AFTER reply to confirm engagement
                result = await self.reply_to_tweet(t["url"], reply_text)
                if result.get("ok"):
                    # Like the tweet now that we've engaged
                    try:
                        await self.page.goto(t["url"], wait_until="domcontentloaded", timeout=10000)
                        await asyncio.sleep(2)
                        for sel in ['button[data-testid="like"]']:
                            try:
                                btn = await self.page.wait_for_selector(sel, timeout=3000)
                                if btn:
                                    await btn.click(force=True)
                                    await asyncio.sleep(1)
                                    logger.info(f"[TwitterBot] liked tweet by @{t.get('author', '?')} (after reply)")
                                    break
                            except Exception:
                                continue
                    except Exception:
                        pass

                    # Notify Discord with full context
                    if _HAS_DISCORD:
                        try:
                            await discord_alerts.alert_twitter_reply(
                                our_reply=reply_text,
                                original_text=t.get("text", ""),
                                original_author=t.get("author", ""),
                                tweet_url=t["url"],
                                keyword=t.get("keyword", ""),
                            )
                        except Exception as e:
                            logger.warning(f"[TwitterBot] Discord reply alert failed: {e}")

                    conn.execute(
                        "INSERT OR IGNORE INTO replied_tweets (tweet_id, author_handle, text, our_reply) VALUES (?, ?, ?, ?)",
                        (t["tweet_id"], t.get("author", ""), t.get("text", ""), reply_text),
                    )
                    conn.commit()
                    # Track daily quota
                    _incr_daily_count("reply_count")
                    replied += 1
                    daily_left -= 1
                    if replied >= MAX_ENGAGED_PER_CYCLE or daily_left <= 0:
                        logger.info(f"[TwitterBot] stopping engagement cycle: replied={replied}, daily_left={daily_left}")
                        break
                    await asyncio.sleep(random.uniform(15, 35))  # don't spam
        finally:
            conn.close()

        return replied

    # ── Shop knowledge cache ──────────────────────────────────
    _shop_cache: dict | None = None
    _shop_cache_ts: float = 0
    _SHOP_CACHE_TTL = 3600  # 1h

    async def _refresh_shop_cache(self) -> dict:
        """Fetch all products from Shopify and cache them for 1h.
        Returns dict with 'products' list, 'tags' set, 'newest' list, etc.
        """
        now = time.time()
        if self._shop_cache and now - self._shop_cache_ts < self._SHOP_CACHE_TTL:
            return self._shop_cache

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{SHOP_URL}/products.json?limit=250")
                if resp.status_code != 200:
                    logger.warning(f"[TwitterBot] Shopify fetch failed: {resp.status_code}")
                    return self._shop_cache or {"products": [], "available": [], "newest": [], "tags": set(), "summary": ""}
                raw_products = resp.json().get("products", [])
        except Exception as e:
            logger.error(f"[TwitterBot] Shopify cache error: {e}")
            return self._shop_cache or {"products": [], "available": [], "newest": [], "tags": set(), "summary": ""}

        available = []
        all_tags = set()
        for p in raw_products:
            variants = p.get("variants", [])
            if variants and variants[0].get("available"):
                price = variants[0].get("price", "?")
                available.append({
                    "title": p["title"],
                    "handle": p["handle"],
                    "price": price,
                    "tags": p.get("tags", []),
                    "product_type": p.get("product_type", ""),
                    "body_html": (p.get("body_html") or "")[:200],
                    "published_at": p.get("published_at", ""),
                    "updated_at": p.get("updated_at", ""),
                })
                for t in p.get("tags", []):
                    all_tags.add(t.lower())

        # Sort by published_at to find newest
        available.sort(key=lambda x: x.get("published_at") or "", reverse=True)

        # Build a short summary for LLM context
        top_new = available[:5]
        summary_lines = [f"- {p['title']} ({p['price']}€)" for p in top_new]
        summary = "\n".join(summary_lines)

        self._shop_cache = {
            "products": raw_products,
            "available": available,
            "newest": available[:10],
            "tags": all_tags,
            "summary": summary,
            "count": len(available),
        }
        self._shop_cache_ts = now
        logger.info(f"[TwitterBot] shop cache refreshed: {len(available)} available products, {len(all_tags)} tags")
        return self._shop_cache

    # ── Shopify product watcher ──────────────────────────────

    async def poll_shopify_products(self) -> int:
        """Check Shopify for GENUINELY NEW products and auto-tweet them.
        Only tweets products that are in stock and uses LLM to write an accurate tweet.
        """
        if not self._require_ready():
            return 0

        shop = await self._refresh_shop_cache()

        conn = _connect_db()
        tweeted = 0
        try:
            for p in shop.get("available", []):
                handle = p.get("handle", "")
                title = p.get("title", "")
                if not handle:
                    continue

                # Already tweeted?
                row = conn.execute(
                    "SELECT 1 FROM known_products WHERE handle = ?",
                    (handle,),
                ).fetchone()
                if row:
                    continue

                # New + in-stock product — generate truthful tweet via LLM
                price = p.get("price", "")
                tags = p.get("tags", [])
                product_type = p.get("product_type", "")

                tweet_text = await self._generate_product_tweet(title, handle, price, tags, product_type)

                if not tweet_text:
                    # Fallback: tweet with product link
                    if handle:
                        tweet_text = f"🍬 {title} — {price}€ 👉 {SHOP_URL}/products/{handle}"
                    else:
                        tweet_text = f"🍬 {title}"
                        if price:
                            tweet_text += f" — {price}€"

                if len(tweet_text) > 280:
                    tweet_text = tweet_text[:277] + "..."

                result = await self.post_tweet(tweet_text)
                if result.get("ok"):
                    conn.execute(
                        "INSERT OR IGNORE INTO known_products (handle, title, tweeted_at) VALUES (?, ?, ?)",
                        (handle, title, datetime.now(timezone.utc).isoformat()),
                    )
                    conn.commit()
                    tweeted += 1
                    self._stats["products_tweeted"] += 1
                    await asyncio.sleep(random.uniform(30, 60))  # don't spam
                else:
                    logger.warning(f"[TwitterBot] product tweet failed for {handle}: {result.get('error')}")
        finally:
            conn.close()

        if tweeted:
            logger.info(f"[TwitterBot] tweeted {tweeted} new products")
        return tweeted

    async def _llm_generate(self, system: str, prompt: str, max_tokens: int | None = None, allow_links: bool = False) -> str | None:
        """LLM call routed through llm_engine._call_llm — serialized with global lock.
        Set allow_links=True for promo tweets that may contain product links.
        """
        try:
            from llm_engine import _call_llm, get_platform_config
            cfg = get_platform_config("twitter")
            mt = max_tokens or cfg.get("max_tokens", 500)
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
            content = await _call_llm(messages, max_tokens=mt, platform="twitter", skip_bad_filter=allow_links)
            if not content:
                return None
            # Light filter for Twitter — only block awkward phrases, not links
            lowered = content.lower()
            for bad in ("site internet", "boutique en ligne", "lien dans la bio"):
                if bad in lowered:
                    logger.warning(f"[TwitterBot] filtered tweet with bad phrase: {content[:80]}")
                    return None
            return content
        except Exception as e:
            logger.warning(f"[TwitterBot] LLM call failed: {e}")
            return None

    async def _generate_product_tweet(self, title: str, handle: str, price: str, tags: list[str], product_type: str) -> str | None:
        """Use LLM to write a truthful tweet about a real product."""
        if not _HAS_LLM:
            return None
        try:
            tag_str = ", ".join(tags[:5]) if tags else ""
            price_info = f"{price}€" if price else "prix sur le site"
            product_url = f"{SHOP_URL}/products/{handle}"
            prompt = (
                f"Écris un tweet (max 260 car.) pour présenter ce produit SoCandyShop : "
                f"« {title} » à {price_info}. Tags : {tag_str}. Type : {product_type or 'bonbons'}. "
                f"Lien du produit : {product_url} "
                f"Tu es Nana (2 ans et demi, team SoCandyShop). Parle comme une ado française. "
                f"Tu PEUX inclure le lien du produit dans le tweet. "
                f"Les infos doivent être EXACTES — pas d'invention de prix, goût ou caractéristique."
            )
            system = "Tu es Nana, la mascotte de SoCandyShop. Tu parles comme une ado française. Tu peux inclure des liens vers les produits SoCandyShop. Tu ne mens JAMAIS sur les produits — tu donnes que des infos vraies. Max 260 caractères."
            result = await self._llm_generate(system, prompt, allow_links=True)
            if result and len(result) <= 280:
                return result.strip()
            logger.warning(f"[TwitterBot] LLM product tweet too long or empty: {result}")
            return None
        except Exception as e:
            logger.warning(f"[TwitterBot] LLM product tweet failed: {e}")
            return None

    # ── Promo tweets ────────────────────────────────────────

    async def maybe_post_promo(self) -> bool:
        """Post a promo tweet based on REAL shop products. Never invent stuff."""
        now = time.time()
        elapsed = now - self._last_promo_ts
        logger.info(f"[TwitterBot] maybe_post_promo called — elapsed={elapsed:.0f}s, interval={PROMO_INTERVAL}s")
        if now - self._last_promo_ts < PROMO_INTERVAL:
            logger.info(f"[TwitterBot] promo: too early ({elapsed:.0f}s < {PROMO_INTERVAL}s), skipping")
            return False
        if PROMO_INTERVAL <= 0:
            logger.info("[TwitterBot] promo: PROMO_INTERVAL <= 0, skipping")
            return False

        logger.info(f"[TwitterBot] promo: interval check passed ({elapsed:.0f}s >= {PROMO_INTERVAL}s), proceeding")

        shop = await self._refresh_shop_cache()
        available = shop.get("available", [])
        if not available:
            logger.info("[TwitterBot] promo: no available products for promo")
            return False

        # Pick a random available product to promote truthfully
        product = random.choice(available[:20])  # top 20 newest
        title = product["title"]
        price = product["price"]
        tags = product.get("tags", [])
        logger.info(f"[TwitterBot] promo: picked product « {title} » à {price}€")

        handle = product.get("handle", "")
        product_url = f"{SHOP_URL}/products/{handle}" if handle else ""

        if _HAS_LLM:
            try:
                tag_str = ", ".join(tags[:5]) if tags else ""
                price_info = f"{price}€" if price else "prix accessible"
                prompt = (
                    f"Écris un tweet promo (max 260 car.) pour SoCandyShop. "
                    f"Mentionne un produit qui existe VRAIMENT : « {title} » à {price_info}. "
                    f"Lien : {product_url} "
                    f"Tags : {tag_str}. "
                    f"Tu es Nana (2 ans et demi, team SoCandyShop). Parle comme une ado française. "
                    f"Tu PEUX inclure le lien du produit. "
                    f"Les infos doivent être EXACTES — le produit et le prix doivent être vrais."
                )
                system = "Tu es Nana, la mascotte de SoCandyShop. Tu parles comme une ado française. Tu peux inclure des liens vers les produits SoCandyShop. Tu ne mens JAMAIS sur les produits. Max 260 caractères."
                logger.info(f"[TwitterBot] promo: calling LLM for product « {title} »")
                text = await self._llm_generate(system, prompt, allow_links=True)
                logger.info(f"[TwitterBot] promo: LLM returned ({len(text) if text else 0} chars): {text[:80] if text else 'None'}")
                if text and len(text) > 280:
                    logger.warning(f"[TwitterBot] promo: LLM text too long ({len(text)} chars), discarding")
                    text = None
            except Exception as e:
                logger.warning(f"[TwitterBot] LLM promo failed: {e}")
                text = None
        else:
            logger.info("[TwitterBot] promo: no LLM available, will use fallback text")
            text = None

        # Fallback: simple truthful tweet with product link
        if not text:
            if handle:
                text = f"🍬 {title} à {price}€ 👉 {SHOP_URL}/products/{handle}"
            else:
                text = f"🍬 {title} à {price}€ sur socandyshop !"
            if len(text) > 280:
                text = text[:277] + "..."
            logger.info(f"[TwitterBot] promo: using fallback text: {text[:80]}")

        logger.info(f"[TwitterBot] promo: posting tweet ({len(text)} chars): {text[:80]}")
        result = await self.post_tweet(text)
        logger.info(f"[TwitterBot] promo: post_tweet result = {result}")
        # Always update timestamp to prevent retry spam on failure
        self._last_promo_ts = now
        if result.get("ok"):
            logger.info(f"[TwitterBot] promo: tweet posted successfully, tweet_id={result.get('tweet_id')}")
            _incr_daily_count("promo_count")
            return True
        else:
            logger.warning(f"[TwitterBot] promo: tweet post failed, error={result.get('error')}")
        return False

    # ── Stats ────────────────────────────────────────────────

    def get_state(self) -> dict:
        return {
            "enabled": ENABLED,
            "ready": self._ready,
            "handle": HANDLE,
            "stats": dict(self._stats),
        }


def _get_daily_count(col: str) -> int:
    """Get today's count for a given column in daily_engagement (reply_count, promo_count, mention_count)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _connect_db()
    try:
        row = conn.execute(
            "SELECT {} FROM daily_engagement WHERE date = ?".format(col),
            (today,),
        ).fetchone()
        if row:
            return row[col] or 0
        return 0
    finally:
        conn.close()


def _incr_daily_count(col: str, n: int = 1) -> None:
    """Increment today's count for a given column in daily_engagement."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _connect_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO daily_engagement (date) VALUES (?)",
            (today,),
        )
        conn.execute(
            "UPDATE daily_engagement SET {} = {} + ? WHERE date = ?".format(col, col),
            (n, today),
        )
        conn.commit()
    finally:
        conn.close()


def _daily_quota_remaining() -> int:
    """How many engagement replies we can still send today."""
    used = _get_daily_count("reply_count")
    return max(0, DAILY_REPLY_QUOTA - used)


def _url_encode(s: str) -> str:
    """Simple URL encode without importing urllib."""
    return s.replace(" ", "%20").replace('"', "%22").replace("#", "%23")


def _is_in_universe(text: str, universe_keywords: list[str]) -> bool:
    """Check if a text is relevant to the bot's topic universe.
    Returns True if at least one universe keyword is found in the text,
    OR if the text contains a keyword embedded in a hashtag (e.g. #TokyoRevengers matches 'anime').
    Also tries reverse: check if any universe keyword is a substring of the text.
    """
    t = text.lower()
    for kw in universe_keywords:
        kw_lower = kw.lower()
        if kw_lower in t:
            return True
        # Reverse check: trend text contained in keyword (e.g. "psg" in "champions league psg")
        if t in kw_lower:
            return True
    # Hashtag decomposition: split camelCase and #prefix (e.g. #StrayKids -> stray kids)
    import re as _re
    decomp = t.replace("#", "")
    # Split on camelCase boundaries: "TokyoRevengers" -> "tokyo revengers"
    decomp = _re.sub(r'([a-z])([A-Z])', r'\1 \2', decomp).lower()
    # Split on numbers: "OnePiece2" -> "one piece 2"
    decomp = _re.sub(r'([a-zA-Z])(\d)', r'\1 \2', decomp)
    decomp = _re.sub(r'(\d)([a-zA-Z])', r'\1 \2', decomp)
    for kw in universe_keywords:
        if kw.lower() in decomp:
            return True
    return False


def _is_old_time_text(time_text: str) -> bool:
    """Check Twitter's relative time text to decide if a tweet is too old (>3h).
    
    Twitter displays: '1m', '5m', '2h', '18h', '1j', '2 juin', etc.
    Returns True if the tweet is older than 3 hours.
    """
    import re as _re
    t = time_text.strip().lower()
    # Minutes — always fresh
    if _re.match(r"\d+\s*m(?:in)?$", t):
        return False
    # Hours — check the number
    m = _re.match(r"(\d+)\s*h(?:r|eure)?$", t)
    if m:
        return int(m.group(1)) > 3
    # Anything with 'j' (jour/day) or a full date — too old
    if "j" in t or "jourd" in t or "juin" in t or "mai" in t or _re.search(r"\d{1,2}\s+\w{3,}", t):
        return True
    # Can't parse — be safe, skip
    return True


# ═══════════════════════════════════════════════════════════
# Background runner — started by main.py
# ═══════════════════════════════════════════════════════════

_bot: Optional[TwitterBot] = None
_task: Optional[asyncio.Task] = None


def get_twitter_bot() -> Optional[TwitterBot]:
    return _bot


async def start_twitter_bot() -> bool:
    """Start the Twitter bot background loop."""
    global _bot, _task

    if not ENABLED:
        logger.info("[TwitterBot] disabled (TWITTER_ENABLED != true)")
        return False

    _bot = TwitterBot()
    ok = await _bot.start()
    if not ok:
        logger.error("[TwitterBot] failed to start — check cookies")
        return False

    _task = asyncio.create_task(_main_loop())
    logger.info("[TwitterBot] background loop started")
    return True


async def stop_twitter_bot() -> None:
    """Stop the Twitter bot."""
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    if _bot:
        await _bot.stop()


async def _main_loop() -> None:
    """Main background loop: review shop, poll mentions, keywords, products, promos."""
    # First cycle: review the full shop catalog (no tweeting, just warm the cache)
    try:
        shop = await _bot._refresh_shop_cache()
        # Mark ALL existing products as "known" so we don't spam old products on first run
        conn = _connect_db()
        try:
            for p in shop.get("available", []):
                handle = p.get("handle", "")
                if handle:
                    conn.execute(
                        "INSERT OR IGNORE INTO known_products (handle, title, tweeted_at) VALUES (?, ?, ?)",
                        (handle, p.get("title", ""), datetime.now(timezone.utc).isoformat()),
                    )
            conn.commit()
            logger.info(f"[TwitterBot] first-run: marked {len(shop.get('available', []))} existing products as known")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[TwitterBot] first-run shop review error: {e}")

    cycle = 0
    while True:
        try:
            if not _bot or not _bot._ready:
                logger.warning("[TwitterBot] not ready, skipping cycle")
                await asyncio.sleep(60)
                continue

            cycle += 1

            # 1. Check mentions & auto-reply
            try:
                await _bot.process_mentions()
            except Exception as e:
                logger.error(f"[TwitterBot] mention processing error: {e}")

            await asyncio.sleep(random.uniform(5, 10))

            # 2. Engage with trending tweets (every cycle)
            if True:
                try:
                    await _bot.engage_keywords()
                except Exception as e:
                    logger.error(f"[TwitterBot] trending engagement error: {e}")

            # 3. Poll Shopify for new products
            try:
                await _bot.poll_shopify_products()
            except Exception as e:
                logger.error(f"[TwitterBot] product poll error: {e}")

            # 4. Maybe post a promo
            try:
                await _bot.maybe_post_promo()
            except Exception as e:
                logger.error(f"[TwitterBot] promo error: {e}")

            # Wait for next cycle
            jitter = random.uniform(-10, 10)
            await asyncio.sleep(max(30, CHECK_INTERVAL + jitter))

        except asyncio.CancelledError:
            logger.info("[TwitterBot] main loop cancelled")
            break
        except Exception as e:
            logger.error(f"[TwitterBot] unexpected loop error: {e}")
            await asyncio.sleep(120)