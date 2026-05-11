"""
Vector Store for NanaBot
ChromaDB-backed semantic search over conversation history.
Two collections: 'messages' (all chat history) and 'facts' (extracted user knowledge).
"""

import logging
import os
from datetime import datetime
from typing import Optional

import chromadb

logger = logging.getLogger("nanabot")

DB_DIR = os.path.expanduser("~/nanabot/data/chroma")

_client = None


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        os.makedirs(DB_DIR, exist_ok=True)
        _client = chromadb.PersistentClient(path=DB_DIR)
    return _client


def init_store() -> None:
    """Initialize collections. Call once at startup."""
    client = _get_client()
    client.get_or_create_collection(
        "messages",
        metadata={"hnsw:space": "cosine"}
    )
    client.get_or_create_collection(
        "facts",
        metadata={"hnsw:space": "cosine"}
    )
    logger.info("[VectorStore] ChromaDB initialized ✅")


def store_message(
    msg_id: str,
    content: str,
    channel_id: str,
    user_name: str,
    role: str,
    created_at: str,
) -> None:
    """Store a single message in the vector store."""
    from embedding_engine import embed_single
    client = _get_client()
    col = client.get_collection("messages")
    vector = embed_single(content)
    col.upsert(
        ids=[msg_id],
        embeddings=[vector],
        documents=[content],
        metadatas=[{
            "channel_id": channel_id,
            "user_name": user_name,
            "role": role,
            "created_at": created_at,
        }],
    )


def search_messages(
    query: str,
    channel_id: Optional[str] = None,
    n_results: int = 5,
    min_relevance: float = 0.3,
) -> list[dict]:
    """Semantic search over stored messages. Optionally filter by channel."""
    from embedding_engine import embed_single
    client = _get_client()
    col = client.get_collection("messages")
    query_vec = embed_single(query)

    where_filter = None
    if channel_id:
        where_filter = {"channel_id": channel_id}

    results = col.query(
        query_embeddings=[query_vec],
        n_results=n_results,
        where=where_filter,
        include=["documents", "metadatas", "distances"],
    )

    hits = []
    if results and results["ids"] and results["ids"][0]:
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]
            relevance = 1.0 - dist  # cosine distance → similarity
            if relevance >= min_relevance:
                meta = results["metadatas"][0][i]
                hits.append({
                    "content": doc,
                    "user_name": meta.get("user_name", ""),
                    "role": meta.get("role", ""),
                    "channel_id": meta.get("channel_id", ""),
                    "created_at": meta.get("created_at", ""),
                    "relevance": round(relevance, 3),
                })
    return hits


def store_fact(
    fact_id: str,
    content: str,
    source: str,
    category: str = "general",
) -> None:
    """Store an extracted fact/knowledge in the facts collection."""
    from embedding_engine import embed_single
    client = _get_client()
    col = client.get_collection("facts")
    vector = embed_single(content)
    col.upsert(
        ids=[fact_id],
        embeddings=[vector],
        documents=[content],
        metadatas=[{
            "source": source,
            "category": category,
            "created_at": datetime.utcnow().isoformat(),
        }],
    )


def search_facts(
    query: str,
    n_results: int = 3,
    min_relevance: float = 0.35,
) -> list[dict]:
    """Semantic search over stored facts."""
    from embedding_engine import embed_single
    client = _get_client()
    col = client.get_collection("facts")
    query_vec = embed_single(query)

    results = col.query(
        query_embeddings=[query_vec],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    hits = []
    if results and results["ids"] and results["ids"][0]:
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]
            relevance = 1.0 - dist
            if relevance >= min_relevance:
                meta = results["metadatas"][0][i]
                hits.append({
                    "fact": doc,
                    "category": meta.get("category", ""),
                    "source": meta.get("source", ""),
                    "relevance": round(relevance, 3),
                })
    return hits


def get_stats() -> dict:
    """Return collection sizes for health check."""
    client = _get_client()
    stats = {}
    for name in ["messages", "facts"]:
        col = client.get_collection(name)
        stats[name] = col.count()
    return stats