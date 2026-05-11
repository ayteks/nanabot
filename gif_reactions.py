"""
GIF Reactions for NanaBot Discord
Searches Tenor for contextual GIFs to send as reactions.

Uses the free Tenor API key for basic access.
Sends GIF links directly — Discord auto-embeds Tenor URLs as playable GIFs.
"""

import logging
import random
import re
from typing import Optional

import httpx

logger = logging.getLogger("nanabot")

_TENOR_API_KEY = "LIVDSRZULELA"  # Free Tenor API key
_TENOR_BASE = "https://g.tenor.com/v1"

# ── GIF search terms mapped to message keywords ──
# When a message matches a keyword, Nana searches Tenor for the corresponding term
_GIF_SEARCH_MAP = {
    # Positive / hype
    "super": ["excited", "yay", "happy dance"],
    "génial": ["amazing", "mind blown", "yay"],
    "bravo": ["applause", "clapping", "bravo"],
    "grave": ["yes", "agreed", "facts"],
    "cool": ["cool", "awesome", "nice"],
    "j'adore": ["love it", "heart eyes", "obsessed"],
    "kiffe": ["loving it", "vibing", "enjoy"],
    "trop bien": ["amazing", "so good", "yay"],
    "merci": ["thank you", "thanks", "grateful"],
    "ouais": ["nod yes", "yep", "agreed"],
    "oui": ["yes", "nod", "agreed"],

    # Funny / laughter
    "haha": ["laughing", "lol", "dying laughing"],
    "mdr": ["laughing", "lol", "dying"],
    "ptdr": ["dying laughing", "laughing so hard", "lmao"],
    "lol": ["laughing", "lol"],

    # Sad / comfort
    "désolé": ["sorry", "hug", "comforting"],
    "désolée": ["sorry", "hug", "comforting"],
    "galère": ["struggle", "hard", "sending hugs"],
    "triste": ["sad", "crying", "hug"],

    # Love
    "love": ["love", "heart", "i love you"],
    "aime": ["love", "heart"],

    # Surprised
    "wouah": ["wow", "mind blown", "shocked"],
    "wow": ["wow", "mind blown", "shocked"],
    "nooon": ["shocked", "no way", "omg"],

    # Anger / drama
    "stop": ["stop", "nope", "enough"],
    "arrête": ["stop", "enough"],

    # Food
    "bonbon": ["candy", "sweets", "yummy"],
    "faim": ["hungry", "food", "yummy"],
    "miam": ["yummy", "delicious", "food"],

    # Questions
    "pourquoi": ["confused", "thinking", "wait what"],
    "comment": ["thinking", "hmm"],

    # General chill vibes
    "chill": ["chill", "relaxing", "vibing"],
    "bonne nuit": ["goodnight", "sleepy", "night"],
    "dormir": ["sleepy", "goodnight", "tired"],
}

# Default searches when no keyword matches
_DEFAULT_GIF_SEARCHES = [
    "thumbs up", "heart", "wave", "hi", "yes", "lol",
    "cool", "cute", "wow", "party", "celebrate",
]

# Cache: search_term -> [url1, url2, ...] to avoid hammering the API
_gif_cache: dict[str, list[str]] = {}


async def search_gif(search_term: str, limit: int = 8) -> list[str]:
    """Search Tenor for GIFs matching the search term. Returns list of Tenor URLs."""
    # Check cache first
    if search_term in _gif_cache:
        return _gif_cache[search_term]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{_TENOR_BASE}/search",
                params={
                    "q": search_term,
                    "key": _TENOR_API_KEY,
                    "limit": limit,
                    "contentfilter": "medium",  # no NSFW
                    "locale": "fr_FR",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        urls = []
        for result in data.get("results", []):
            # Use the tinygif for quick preview, or gif for full
            # Discord auto-embeds tenor.com URLs
            media = result.get("media", [])
            if media:
                # Get the tenor URL (itemurl) — Discord embeds this as playable GIF
                item_url = result.get("itemurl", "")
                if item_url:
                    urls.append(item_url)
                    continue
                # Fallback: get the gif URL from media array
                tiny = media[0].get("tinygif", {})
                gif_url = tiny.get("url", "")
                if gif_url:
                    urls.append(gif_url)

        if urls:
            _gif_cache[search_term] = urls
            # Keep cache under 50 entries
            if len(_gif_cache) > 50:
                oldest = list(_gif_cache.keys())[:10]
                for k in oldest:
                    _gif_cache.pop(k, None)

        return urls

    except Exception as e:
        logger.debug(f"[GIF] Tenor search failed for '{search_term}': {e}")
        return []


def pick_search_terms(text: str) -> list[str]:
    """Pick relevant Tenor search terms based on message content."""
    lowered = text.lower()
    terms = []
    for keyword, searches in _GIF_SEARCH_MAP.items():
        if keyword in lowered:
            terms.extend(searches)
    if not terms:
        # Pick a random default
        terms = random.sample(_DEFAULT_GIF_SEARCHES, k=2)
    return terms[:3]  # max 3 terms to try


async def get_reaction_gif(text: str) -> Optional[str]:
    """Get a relevant GIF URL for reacting to a message.
    Returns a Tenor URL that Discord will auto-embed, or None.
    """
    search_terms = pick_search_terms(text)

    for term in search_terms:
        urls = await search_gif(term, limit=5)
        if urls:
            return random.choice(urls)

    # Fallback: try one more generic search
    fallback = random.choice(_DEFAULT_GIF_SEARCHES)
    urls = await search_gif(fallback, limit=5)
    if urls:
        return random.choice(urls)

    return None