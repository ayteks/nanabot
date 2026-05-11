"""
Conversation Memory Store for SoCandyShop Discord Bot
Stores last N messages per channel in SQLite for LLM context.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

DB_DIR = os.path.expanduser("~/nanabot/data")
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
    # ── Lessons table for self-improvement ──
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            source TEXT DEFAULT 'auto',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_lessons_active ON lessons(active)"
    )
    # ── User profiles for persistent per-user knowledge ──
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            user_name TEXT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT DEFAULT 'auto',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile ON user_profiles(user_id, key)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_profiles_active ON user_profiles(active)"
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


# ── Lessons (self-improvement) ─────────────────────────────

def add_lesson(lesson: str, category: str = "general", source: str = "auto") -> None:
    """Add a new lesson learned by the bot."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO lessons (lesson, category, source) VALUES (?, ?, ?)",
        (lesson, category, source),
    )
    # Keep max 50 lessons to avoid prompt bloat
    c.execute(
        """
        DELETE FROM lessons
        WHERE id NOT IN (
            SELECT id FROM lessons ORDER BY created_at DESC LIMIT 50
        )
        """
    )
    conn.commit()
    conn.close()


def get_lessons(active_only: bool = True) -> list[str]:
    """Return all active lessons, newest first."""
    conn = _connect()
    c = conn.cursor()
    query = "SELECT lesson FROM lessons"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY created_at DESC"
    c.execute(query)
    rows = c.fetchall()
    conn.close()
    return [row["lesson"] for row in rows]


def deactivate_lesson(lesson_id: int) -> None:
    """Mark a lesson as inactive (e.g. if it's no longer relevant)."""
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lesson_id,))
    conn.commit()
    conn.close()


# ── User Profiles (per-user persistent knowledge) ─────────

def set_user_fact(user_id: str, key: str, value: str, user_name: str = "", source: str = "auto") -> None:
    """Set or update a fact about a user. Upsert by (user_id, key)."""
    conn = _connect()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO user_profiles (user_id, user_name, key, value, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET
            value = excluded.value,
            user_name = excluded.user_name,
            source = excluded.source,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(user_id), user_name, key, value, source),
    )
    conn.commit()
    conn.close()


def get_user_facts(user_id: str, active_only: bool = True) -> list[dict]:
    """Get all facts for a user. Returns list of {key, value, source}."""
    conn = _connect()
    c = conn.cursor()
    query = "SELECT key, value, source, user_name FROM user_profiles WHERE user_id = ?"
    if active_only:
        query += " AND active = 1"
    c.execute(query, (str(user_id),))
    rows = c.fetchall()
    conn.close()
    return [{"key": row["key"], "value": row["value"], "source": row["source"], "user_name": row["user_name"]} for row in rows]


def get_user_profile_text(user_id: str) -> str:
    """Get a compact text summary of a user's profile for LLM injection."""
    facts = get_user_facts(user_id)
    if not facts:
        return ""
    name = facts[0].get("user_name", "")
    lines = [f"- {f['key']}: {f['value']}" for f in facts]
    header = f"Ce que tu sais sur {name} (ID {user_id}):" if name else f"Ce que tu sais sur l'utilisateur {user_id}:"
    return f"{header}\n" + "\n".join(lines)


def deactivate_user_fact(user_id: str, key: str) -> None:
    """Mark a user fact as inactive."""
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE user_profiles SET active = 0 WHERE user_id = ? AND key = ?", (str(user_id), key))
    conn.commit()
    conn.close()
