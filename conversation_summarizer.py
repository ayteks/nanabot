"""
Conversation Summarizer for NanaBot — MemPalace Edition
=========================================================
Background task that periodically compresses old conversations into summaries.
Summaries are stored in MemPalace drawers for semantic recall.
Falls back to SQLite if MemPalace is unavailable.
"""

import asyncio
import hashlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger("nanabot")

_API_BASE = ""
_API_KEY = ""
_CHAT_MODEL = ""


def _init_config():
    global _API_BASE, _API_KEY, _CHAT_MODEL
    if not _API_BASE:
        import os
        _API_BASE = os.getenv("API_BASE_URL", os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com"))
        if not _API_BASE.rstrip("/").endswith("/v1"):
            _API_BASE = _API_BASE.rstrip("/") + "/v1"
        _API_KEY = os.getenv("API_KEY", os.getenv("OLLAMA_API_KEY", os.getenv("OLLAMA_CLOUD_API_KEY", "")))
        _CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "ministral-3:14b"))


_SUMMARIZE_PROMPT = """Résume cette conversation Discord en 2-3 phrases. Garde les FAITS UTILES :
- préférences exprimées
- questions posées et réponses données
- infos sur les utilisateurs
- topics discutés

Ignore : salutations, réactions simples, messages vides.

Conversation :
{conversation}

Résumé (2-3 phrases, en français) :"""


async def summarize_conversation(messages: list[dict]) -> Optional[str]:
    """Summarize a list of messages into 2-3 sentences."""
    _init_config()
    if not messages or len(messages) < 5:
        return None

    # Format conversation
    lines = []
    for msg in messages:
        name = msg.get("user_name", msg.get("name", "?"))
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prefix = "Nana" if role == "assistant" else name
        lines.append(f"{prefix}: {content}")

    conversation = "\n".join(lines[-30:])  # last 30 messages max

    try:
        prompt = _SUMMARIZE_PROMPT.format(conversation=conversation)
        url = f"{_API_BASE}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if _API_KEY:
            headers["Authorization"] = f"Bearer {_API_KEY}"

        payload = {
            "model": _CHAT_MODEL,
            "messages": [
                {"role": "system", "content": "Tu résumes des conversations Discord en 2-3 phrases."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 150,
            "temperature": 0.3,
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        summary = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if summary and len(summary) > 10:
            return summary
        return None

    except Exception as e:
        logger.debug(f"[Summarizer] failed: {e}")
        return None


async def run_summarization_cycle() -> int:
    """Run one summarization cycle over all channels. Returns summaries generated.
    Stores in MemPalace. Falls back to SQLite if MemPalace unavailable."""
    import memory_store as mem
    import sqlite3

    conn = sqlite3.connect(mem.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get all channels
    c.execute("SELECT DISTINCT channel_id FROM messages")
    channels = [row["channel_id"] for row in c.fetchall()]
    conn.close()

    summaries_generated = 0
    mempalace_ok = False

    # Try MemPalace first
    try:
        from mempalace_bridge import store_summary
        mempalace_ok = True
    except ImportError:
        logger.debug("[Summarizer] MemPalace bridge not available")

    for ch_id in channels:
        # Get ALL messages for this channel
        all_messages = mem.get_history(ch_id, limit=200)

        # Only summarize if we have enough messages
        if len(all_messages) < 20:
            continue

        # Generate summary
        summary = await summarize_conversation(all_messages)
        if not summary:
            continue

        # ── Primary: Store in MemPalace ──
        if mempalace_ok:
            try:
                await store_summary(
                    channel_id=ch_id,
                    summary=summary,
                )
                summaries_generated += 1
                logger.info(f"[Summarizer] MemPalace summary for channel {ch_id}: {summary[:80]}...")
                continue
            except Exception as e:
                logger.debug(f"[Summarizer] MemPalace failed: {e}")

        # ── Fallback: log only (no ChromaDB) ──
        logger.info(f"[Summarizer] No storage available, skipping summary for {ch_id}")

    return summaries_generated


async def summarization_loop(interval_hours: float = 6.0) -> None:
    """Background loop: run summarization every N hours."""
    logger.info(f"[Summarizer] Starting loop — every {interval_hours}h")
    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)
            count = await run_summarization_cycle()
            if count > 0:
                logger.info(f"[Summarizer] Generated {count} summaries")
        except asyncio.CancelledError:
            logger.info("[Summarizer] Loop cancelled")
            break
        except Exception as e:
            logger.warning(f"[Summarizer] Error: {e}")
            await asyncio.sleep(300)  # retry in 5 min on error