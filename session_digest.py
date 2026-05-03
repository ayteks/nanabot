#!/usr/bin/env python3
"""
Daily Session Digest — posts a summary of the day's work to Discord.
Reads ~/.hermes/sessions/*.jsonl, filters for today, generates a
markdown digest with:
  • Topics worked on
  • Key decisions / commits / deployments
  • Files modified
  • Open items / next steps (if any)

Usage:
  python3 session_digest.py           # preview to stdout
  python3 session_digest.py --post  # post to Discord #updates
"""

import argparse
import asyncio
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx

SESSION_DIR = os.path.expanduser("~/.hermes/sessions")
DISCORD_CHANNEL = os.getenv("DISCORD_ALERTS_CHANNEL", "1499361259395485717")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _parse_ts(ts: str) -> datetime:
    """Parse ISO timestamp, handle ±HH:MM or Z suffix."""
    ts = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.now(timezone.utc)


def _is_today(ts: str) -> bool:
    try:
        dt = _parse_ts(ts)
        return dt.strftime("%Y%m%d") == _today_iso()
    except Exception:
        return False


def _extract_actions(content: str) -> list[str]:
    """Extract commit messages, service restarts, deployments from text."""
    actions = []
    # git commits
    for m in re.finditer(r"git commit -m [\"']([^\"']+)[\"']", content):
        actions.append(f"📝 Commit: `{m.group(1)}`")
    for m in re.finditer(r"\[main ([a-f0-9]+)\]\s*(.+?)(?:\n|$)", content):
        actions.append(f"📝 Commit `{m.group(1)}`: {m.group(2).strip()}")
    # service restarts
    for m in re.finditer(r"(systemctl.*restart\s+\S+)", content):
        actions.append(f"🔧 Restart: `{m.group(1)}`")
    # pushes
    for m in re.finditer(r"(git push\S*)", content):
        actions.append(f"🚀 Push: `{m.group(1)}`")
    # installs
    for m in re.finditer(r"(pip install\s+.+?)(?:\n|\"|$)", content):
        actions.append(f"📦 Install: `{m.group(1).strip()}`")
    return actions


def _extract_files(content: str) -> list[str]:
    """Extract file paths that were modified/created."""
    files = set()
    for m in re.finditer(r"[~]?/([\w/.-]+\.(?:py|js|css|liquid|json|yaml|yml|toml|md|service|env))", content):
        files.add(f"`~/{m.group(1)}`")
    for m in re.finditer(r"`([\w/.-]+\.(?:py|js|css|liquid|json|yaml|yml|toml|md|service|env))`", content):
        files.add(f"`{m.group(1)}`")
    return sorted(files)


def _extract_topic(user_msg: str, assistant_reasoning: str) -> str:
    """Guess the topic from the first user message + assistant reasoning."""
    # Priority: user explicit topic
    lower = user_msg.lower()
    keywords = [
        ("socandyshop", "SoCandyShop"),
        ("tiktok", "TikTok"),
        ("shopify", "Shopify"),
        ("discord", "Discord"),
        ("krain88", "Krain88"),
        ("thermal", "Thermal/CPU"),
        ("cpu", "Thermal/CPU"),
        ("temperature", "Thermal/CPU"),
        ("git", "Git"),
        ("commit", "Git"),
        ("deploy", "Deploy"),
        ("service", "Systemd"),
        ("backend", "Backend"),
        ("live", "Livestream"),
        ("alert", "Alerts"),
        ("cron", "Automation"),
    ]
    for kw, label in keywords:
        if kw in lower:
            return label
    # Fallback: assistant reasoning
    r_lower = assistant_reasoning.lower()
    for kw, label in keywords:
        if kw in r_lower:
            return label
    return "General"


def build_digest() -> str:
    today = _today_iso()
    pattern = os.path.join(SESSION_DIR, f"{today}*.jsonl")
    files = glob.glob(pattern)
    if not files:
        # fallback: any jsonl from today by mtime
        all_files = glob.glob(os.path.join(SESSION_DIR, "*.jsonl"))
        files = [f for f in all_files if datetime.fromtimestamp(os.path.getmtime(f), tz=timezone.utc).strftime("%Y%m%d") == today]

    if not files:
        return ""

    sessions_data = []
    for path in sorted(files):
        topic = "General"
        actions: list[str] = []
        files_modified: set[str] = set()
        open_items: list[str] = []
        msg_count = 0
        start_ts = ""

        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role", "")
                ts = obj.get("timestamp", "")
                content = obj.get("content", "")
                reasoning = obj.get("reasoning", "")

                if role == "user" and content and not start_ts:
                    start_ts = ts
                    topic = _extract_topic(content, reasoning)

                if role in ("user", "assistant") and content:
                    msg_count += 1
                    acts = _extract_actions(content + " " + reasoning)
                    actions.extend(acts)
                    fms = _extract_files(content + " " + reasoning)
                    files_modified.update(fms)

                if role == "assistant" and reasoning:
                    # Look for actual TODO / checklist markers in reasoning
                    for m in re.finditer(
                        r"(?:^|\n)\s*(?:[-*]\s+\[\s*\]|[✓✗])\s*(.+?)(?:\n|$)",
                        reasoning,
                        re.MULTILINE,
                    ):
                        item = m.group(1).strip()
                        if 10 < len(item) < 150:
                            open_items.append(item)
                    # Also catch explicit "Next steps:" or "TODO:" blocks
                    for m in re.finditer(
                        r"(?:Next steps?|TODOs?|Open items?|Pending)[^\n]*\n((?:\s*[-*]\s+.+?\n)+)",
                        reasoning,
                        re.IGNORECASE | re.DOTALL,
                    ):
                        block = m.group(1)
                        for line in block.strip().split("\n"):
                            line = line.strip().lstrip("-* ").strip()
                            if 10 < len(line) < 150:
                                open_items.append(line)

        if msg_count == 0:
            continue

        sessions_data.append({
            "topic": topic,
            "actions": list(dict.fromkeys(actions)),  # dedup preserve order
            "files": sorted(files_modified),
            "open": list(dict.fromkeys(open_items))[:3],
            "msgs": msg_count,
        })

    if not sessions_data:
        return ""

    # ── Build markdown digest ───────────────────────────────
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    lines = [
        f"## 📋 Daily Digest — {date_str}",
        "",
    ]

    for i, sess in enumerate(sessions_data, 1):
        lines.append(f"**{i}. {sess['topic']}**  ({sess['msgs']} messages)")
        if sess["actions"]:
            for a in sess["actions"][:5]:
                lines.append(f"  • {a}")
        if sess["files"]:
            lines.append(f"  • 🗂️ Files: {', '.join(sess['files'][:4])}")
            if len(sess["files"]) > 4:
                lines.append(f"    (+{len(sess['files']) - 4} more)")
        if sess["open"]:
            lines.append(f"  • ⏳ Open: {sess['open'][0]}")
        lines.append("")

    return "\n".join(lines)


async def post_to_discord(content: str) -> bool:
    if not BOT_TOKEN:
        print("[digest] DISCORD_BOT_TOKEN not set — skipping post", file=sys.stderr)
        return False
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages"
    payload = {
        "content": content[:2000],  # Discord limit
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "Content-Type": "application/json",
        }, json=payload)
        if resp.status_code in (200, 201):
            print("[digest] Posted to Discord #updates")
            return True
        print(f"[digest] Discord post failed: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", action="store_true", help="Post digest to Discord instead of stdout")
    args = parser.parse_args()

    digest = build_digest()
    if not digest:
        print("[digest] No sessions found for today.")
        sys.exit(0)

    if args.post:
        asyncio.run(post_to_discord(digest))
    else:
        print(digest)
