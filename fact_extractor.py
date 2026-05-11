"""
Fact Extractor for NanaBot — MemPalace Edition
================================================
After each conversation turn, extract key facts about users and store them
in MemPalace Knowledge Graph + drawers. Falls back to SQLite if MemPalace unavailable.

Runs as async background task — never blocks the reply.
"""

import asyncio
import hashlib
import logging
import random
from typing import Optional

import httpx

logger = logging.getLogger("nanabot")

_API_BASE = ""
_API_KEY = ""
_CHAT_MODEL = ""


def _init_config():
    """Lazy-load config from env (same as llm_engine)."""
    global _API_BASE, _API_KEY, _CHAT_MODEL
    if not _API_BASE:
        import os
        _API_BASE = os.getenv("API_BASE_URL", os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com"))
        if not _API_BASE.rstrip("/").endswith("/v1"):
            _API_BASE = _API_BASE.rstrip("/") + "/v1"
        _API_KEY = os.getenv("API_KEY", os.getenv("OLLAMA_API_KEY", os.getenv("OLLAMA_CLOUD_API_KEY", "")))
        _CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "gemma4:31b"))


_EXTRACT_CATEGORIES = ["preference", "location", "question", "experience", "relationship", "other"]

_EXTRACT_PROMPT = """Tu es un extracteur de faits. À partir d'un message, extrais les FAITS UTILES sur l'utilisateur.

Règles :
- Un fait = un truc concret et durable (pas "il dit bonjour" ou "il a une opinion passagère")
- Catégories possibles : preference, location, question, experience, relationship, other
- Format : un fait par ligne = "catégorie | fait court (< 15 mots)"
- Si aucun fait durable, réponds juste "RIEN"
- JAMAIS de faits sur les produits SoCandyShop (on connaît déjà)
- JAMAIS de faits évidents ("c'est un viewer TikTok")

Message de {user_name} : "{content}"
Réponse précédente de Nana : "{bot_reply}"
"""


async def extract_facts(
    user_name: str,
    user_id: str,
    content: str,
    bot_reply: str,
) -> list[dict]:
    """Extract facts from a conversation turn. Returns list of {category, fact}."""
    _init_config()

    # Don't extract from very short messages or bot commands
    if len(content.strip()) < 10:
        return []

    # Only extract ~40% of the time to save API calls
    if random.random() > 0.40:
        return []

    try:
        prompt = _EXTRACT_PROMPT.format(user_name=user_name, content=content, bot_reply=bot_reply)
        messages = [
            {"role": "system", "content": "Tu extrais des faits utiles. Réponds RIEN si rien à retenir."},
            {"role": "user", "content": prompt},
        ]

        url = f"{_API_BASE}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if _API_KEY:
            headers["Authorization"] = f"Bearer {_API_KEY}"

        payload = {
            "model": _CHAT_MODEL,
            "messages": messages,
            "max_tokens": 100,
            "temperature": 0.2,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if not text or text.upper() == "RIEN":
            return []

        facts = []
        for line in text.split("\n"):
            line = line.strip().lstrip("- •").strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|", 1)
            if len(parts) != 2:
                continue
            category = parts[0].strip().lower()
            fact_text = parts[1].strip()
            if category not in _EXTRACT_CATEGORIES:
                category = "other"
            if len(fact_text) > 80:
                fact_text = fact_text[:77] + "..."
            facts.append({"category": category, "fact": fact_text})

        return facts

    except Exception as e:
        logger.debug(f"[FactExtractor] failed: {e}")
        return []


async def process_and_store(
    user_name: str,
    user_id: str,
    content: str,
    bot_reply: str,
) -> int:
    """Extract facts and store them in MemPalace KG.
    Falls back to SQLite only if MemPalace is unavailable.
    Returns count of facts stored."""
    facts = await extract_facts(user_name, user_id, content, bot_reply)
    stored = 0

    for f in facts:
        category = f["category"]
        fact_text = f["fact"]

        # ── Primary: MemPalace Knowledge Graph ──
        try:
            from mempalace_bridge import store_user_fact
            await store_user_fact(
                user_name=user_name,
                user_id=user_id,
                category=category,
                fact=fact_text,
            )
            stored += 1
            logger.info(f"[FactExtractor] MemPalace: {user_name} {category}={fact_text}")
            continue  # Skip fallback if MemPalace succeeded
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[FactExtractor] MemPalace failed, falling back: {e}")

        # ── Fallback: SQLite only (no ChromaDB) ──
        try:
            import memory_store as mem
            mem.set_user_fact(
                user_id=user_id,
                key=category,
                value=fact_text,
                user_name=user_name,
                source="fact-extractor",
            )
            stored += 1
            logger.info(f"[FactExtractor] SQLite fallback: {user_name} {category}={fact_text}")
        except Exception:
            pass

    return stored