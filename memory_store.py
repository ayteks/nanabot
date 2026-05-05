"""
Conversation Memory Store for SoCandyShop Discord Bot
Stores last N messages per channel in SQLite for LLM context.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DB_DIR = os.path.expanduser("~/tiktok-backend/data")
DB_PATH = os.path.join(DB_DIR, "discord_chat_memory.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode for concurrent readers/writers
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            user_id TEXT,
            user_name TEXT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_channel_time ON messages(channel_id, created_at)"
    )
    conn.commit()
    conn.close()


def save_message(
    channel_id: str,
    user_id: Optional[str],
    user_name: str,
    role: str,
    content: str,
) -> None:
    """Persist a single message. Trims old records per channel."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (channel_id, user_id, user_name, role, content) VALUES (?, ?, ?, ?, ?)",
        (str(channel_id), user_id, user_name, role, content),
    )
    # Keep only 200 messages per channel
    c.execute(
        """
        DELETE FROM messages
        WHERE channel_id = ?
        AND id NOT IN (
            SELECT id FROM messages
            WHERE channel_id = ?
            ORDER BY created_at DESC
            LIMIT 200
        )
        """,
        (str(channel_id), str(channel_id)),
    )
    conn.commit()
    conn.close()


def get_history(channel_id: str, limit: int = 12) -> list[dict]:
    """Return last `limit` messages for a channel, oldest first (ready for LLM)."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        """
        SELECT user_name, role, content
        FROM messages
        WHERE channel_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (str(channel_id), limit),
    )
    rows = c.fetchall()
    conn.close()
    return [
        {"name": row["user_name"] or "Someone", "role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]
