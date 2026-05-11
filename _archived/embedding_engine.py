"""
Embedding Engine for NanaBot
Singleton sentence-transformers model for vector embeddings.
Uses paraphrase-multilingual-MiniLM-L12-v2 (384-dim, multilingual).
"""

import logging
import threading
from typing import Union

logger = logging.getLogger("nanabot")

_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_model = None
_lock = threading.Lock()


def _get_model():
    """Lazy-load the embedding model (thread-safe)."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                logger.info(f"[Embeddings] Loading model {_MODEL_NAME}...")
                _model = SentenceTransformer(_MODEL_NAME)
                logger.info(f"[Embeddings] Model loaded ✅ (dim={_model.get_embedding_dimension()})")
    return _model


def embed(texts: Union[str, list[str]]) -> list[list[float]]:
    """Embed one or more texts. Returns list of 384-dim vectors."""
    model = _get_model()
    if isinstance(texts, str):
        texts = [texts]
    vectors = model.encode(texts, show_progress_bar=False, normalize_embeddings=True)
    return vectors.tolist()


def embed_single(text: str) -> list[float]:
    """Embed a single text string. Returns one 384-dim vector."""
    return embed([text])[0]