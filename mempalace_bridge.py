"""
MemPalace Bridge for NanaBot
=============================
Replaces vector_store.py + fact_extractor.py + user_profiles in memory_store.py
with MemPalace's semantic search, knowledge graph, and verbatim storage.

Architecture:
  - Recent context (last 12 msgs/channel): still in SQLite (memory_store.py) — instant
  - Semantic search (past conversations, facts): MemPalace wing_nanabot
  - User knowledge (profiles, preferences): MemPalace Knowledge Graph
  - Conversation summaries: MemPalace wing_nanabot/room_summaries
  - Lessons learned: MemPalace wing_nanabot/room_lessons

All async — uses asyncio.to_thread() for blocking MemPalace calls.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

logger = logging.getLogger("nanabot")

# ── MemPalace setup ─────────────────────────────────────────────
_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")

def _ensure_mempalace():
    """Lazy-init: add MemPalace venv to sys.path if needed."""
    venv_site = os.path.expanduser("~/mempalace/.venv/lib/python3.11/site-packages")
    if venv_site not in sys.path:
        sys.path.insert(0, venv_site)


# ── Knowledge Graph (fast, SQLite) ─────────────────────────────
_kg = None

def _get_kg():
    """Get or create the KnowledgeGraph singleton."""
    global _kg
    if _kg is None:
        from mempalace.knowledge_graph import KnowledgeGraph
        _kg = KnowledgeGraph()
        logger.info("[MemPalace] Knowledge Graph initialized")
    return _kg


# ── Semantic Search ─────────────────────────────────────────────

async def search(
    query: str,
    wing: str = "nanabot",
    room: Optional[str] = None,
    n_results: int = 3,
    max_distance: float = 0.65,
) -> list[dict]:
    """Search MemPalace for relevant context. Async-safe."""
    def _sync():
        _ensure_mempalace()
        from mempalace.searcher import search_memories
        results = search_memories(
            query=query,
            palace_path=_PALACE_PATH,
            wing=wing,
            room=room,
            n_results=n_results,
            max_distance=max_distance,
        )
        if "error" in results:
            logger.debug(f"[MemPalace] Search error: {results.get('error')}")
            return []
        return results.get("results", results.get("drawers", []))

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.debug(f"[MemPalace] search failed: {e}")
        return []


# ── Store Content ──────────────────────────────────────────────

async def store_drawer(
    content: str,
    wing: str = "nanabot",
    room: str = "conversations",
    source_file: Optional[str] = None,
    added_by: str = "nanabot",
) -> Optional[str]:
    """Store verbatim content in a MemPalace drawer. Async-safe."""
    def _sync():
        _ensure_mempalace()
        from mempalace.palace import get_collection
        from mempalace.config import get_configured_collection_name, MempalaceConfig, sanitize_name, sanitize_content
        import hashlib
        from datetime import datetime as _dt

        # Generate unique ID
        content_hash = hashlib.md5(content.encode()).hexdigest()[:16]
        drawer_id = f"nb_{_dt.utcnow().strftime('%Y%m%d_%H%M')}_{content_hash}"

        col = get_collection(_PALACE_PATH)
        metadata = {
            "wing": sanitize_name(wing),
            "room": sanitize_name(room),
            "source_file": source_file or "",
            "added_by": added_by,
            "created_at": _dt.utcnow().isoformat(),
        }

        # Embed and upsert
        from mempalace.embedding import get_embedding_function
        ef = get_embedding_function()
        vector = ef([content])[0]

        col.upsert(
            ids=[drawer_id],
            embeddings=[vector],
            documents=[content],
            metadatas=[metadata],
        )
        return drawer_id

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.debug(f"[MemPalace] store_drawer failed: {e}")
        return None


# ── Knowledge Graph Operations ─────────────────────────────────

async def kg_add_fact(
    subject: str,
    predicate: str,
    obj: str,
    valid_from: Optional[str] = None,
    valid_to: Optional[str] = None,
    source: str = "nanabot",
) -> None:
    """Add a fact to the knowledge graph. Async-safe."""
    def _sync():
        kg = _get_kg()
        kg.add_triple(
            subject=subject,
            predicate=predicate,
            obj=obj,
            valid_from=valid_from,
            valid_to=valid_to,
            source_file=source,
        )

    try:
        await asyncio.to_thread(_sync)
    except Exception as e:
        logger.debug(f"[MemPalace] kg_add_fact failed: {e}")


async def kg_invalidate(
    subject: str,
    predicate: str,
    obj: str,
    ended: Optional[str] = None,
) -> None:
    """Mark a fact as no longer true. Async-safe."""
    def _sync():
        kg = _get_kg()
        kg.invalidate(
            subject=subject,
            predicate=predicate,
            obj=obj,
            ended=ended or datetime.utcnow().date().isoformat(),
        )

    try:
        await asyncio.to_thread(_sync)
    except Exception as e:
        logger.debug(f"[MemPalace] kg_invalidate failed: {e}")


async def kg_query(
    entity: str,
    as_of: Optional[str] = None,
    direction: str = "both",
) -> list[dict]:
    """Query knowledge graph for an entity. Async-safe."""
    def _sync():
        kg = _get_kg()
        return kg.query_entity(entity, as_of=as_of, direction=direction)

    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.debug(f"[MemPalace] kg_query failed: {e}")
        return []


async def kg_get_user_profile(user_name: str, user_id: str) -> str:
    """Get a compact text summary of a user's KG profile for LLM injection."""
    facts = await kg_query(user_name, direction="both")
    if not facts:
        # Try with user_id as fallback
        facts = await kg_query(user_id, direction="both")
    if not facts:
        return ""

    lines = []
    for f in facts:
        if not f.get("current", True):
            continue  # skip expired facts
        pred = f.get("predicate", "?")
        obj = f.get("object", "?")
        lines.append(f"- {pred}: {obj}")

    if not lines:
        return ""

    header = f"Ce que tu sais sur {user_name} :"
    return f"{header}\n" + "\n".join(lines)


# ── High-Level NanaBot Operations ──────────────────────────────

async def store_conversation_turn(
    user_name: str,
    user_id: str,
    content: str,
    role: str,
    channel_id: str,
) -> None:
    """Store a conversation turn as a MemPalace drawer + KG extraction."""
    room = f"channel_{channel_id[-6:]}"  # last 6 chars of channel ID
    await store_drawer(
        content=f"[{role}] {user_name}: {content}",
        wing="nanabot",
        room=f"conv_{channel_id[-6:]}",
        source_file=f"discord:{channel_id}",
        added_by="nanabot-auto",
    )


async def store_lesson(lesson: str, category: str = "general", source: str = "auto") -> None:
    """Store a learned lesson in MemPalace."""
    await store_drawer(
        content=f"[LEÇON] {category}: {lesson}",
        wing="nanabot",
        room="lessons",
        source_file=f"lesson:{source}",
        added_by="nanabot-self-eval",
    )


async def store_summary(
    channel_id: str,
    summary: str,
) -> None:
    """Store a conversation summary."""
    await store_drawer(
        content=f"[RÉSUMÉ] {summary}",
        wing="nanabot",
        room="summaries",
        source_file=f"summarizer:{channel_id}",
        added_by="nanabot-summarizer",
    )


async def store_user_fact(
    user_name: str,
    user_id: str,
    category: str,
    fact: str,
) -> None:
    """Store a fact about a user in the Knowledge Graph + a drawer reference."""
    # KG — fast lookup
    await kg_add_fact(
        subject=user_name,
        predicate=category,
        obj=fact,
        source=f"nanabot:user:{user_id}",
    )
    # Drawer — verbatim context for semantic search
    await store_drawer(
        content=f"[{user_name}] {category}: {fact}",
        wing="nanabot",
        room=f"user_{user_id[-4:]}",
        source_file=f"user:{user_id}",
        added_by="nanabot-fact-extractor",
    )


async def get_relevant_context(
    comment: str,
    user_name: Optional[str] = None,
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """Fetch relevant context from MemPalace for LLM injection.

    CRITICAL: Keep this SHORT and HIGHLY relevant. Too much context
    causes Nana to respond to old topics instead of the current conversation.
    Max 2 semantic hits (similarity > 0.55) + user KG profile.
    """
    context_parts = []

    # 1. Semantic search — STRICT: max 2 results, high threshold
    try:
        results = await search(
            query=comment,
            wing="nanabot",
            n_results=3,
            max_distance=0.55,
        )
        if results:
            lines = []
            for r in results[:2]:  # max 2 hits
                text = r.get("text", "")[:100]  # truncate for brevity
                sim = r.get("similarity", 0)
                if isinstance(sim, (int, float)) and sim > 0.55:
                    lines.append(f"- {text}")
            if lines:
                context_parts.append("Souvenir pertinent :\n" + "\n".join(lines))
    except Exception as e:
        logger.debug(f"[MemPalace] semantic search failed: {e}")

    # 2. KG user facts — compact, max 3 facts
    if user_name:
        try:
            profile = await kg_get_user_profile(user_name, user_id or "")
            if profile:
                # Keep it short: max 3 lines
                profile_lines = profile.split("\n")[:4]  # header + 3 facts
                context_parts.append("\n".join(profile_lines))
        except Exception as e:
            logger.debug(f"[MemPalace] kg profile failed: {e}")

    return "\n\n".join(context_parts)


# ── Init ────────────────────────────────────────────────────────

def init():
    """Initialize MemPalace bridge. Call once at startup."""
    _ensure_mempalace()
    _get_kg()
    logger.info("[MemPalace] Bridge initialized ✅")