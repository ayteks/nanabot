# NanaBot LLM Engine Documentation

## Overview

The LLM engine powers the responsive persona of Nana on TikTok, Twitter, and Discord using `gemma4:31b` (via Ollama Cloud) to give natural, emotionally-aware, and brand-consistent responses.

It is invoked by:
1. **TikTok Live Chat Bot** (`live_chat_bot_v2.py`) for replies to mentions and interactions
2. **Twitter Bot** (`twitter_bot.py`) for replying to comments/DMs
3. **Discord auto-responder** passively in channels via `discord_commands.py`

## Core Components

### 1. Identity and Personality Stack

Built into the system prompt:
- **Voice**: Teen-like (not childish), speaks slang naturally (e.g., "grave", "bref")
- **Age**: Claims to be 2.5 years old—but talks like a teen
- **Interests**: Bonbons, doudous, juice boxes → toddler worldviews
- **Tone Rules**: Emphasis on directness, brevity, no emotional commentary like *"je souris"*

**Prohibition Filters**:
> No links, no corporate jargon, no redundant questions, no hostility.

### 2. Platform Tuning

Each communication channel is adjusted for context-sensitive responses via environment overrides:

```python
PLATFORM_DEFAULTS = {
    "discord": {
        "max_tokens": 300,
        "temperature": 0.7,
        "max_chars": 120,
    },
    "tiktok": {
        "max_tokens": 200,
        "temperature": 0.8,
        "max_chars": 100,
    },
    "twitter": {
        "max_tokens": 300,
        "temperature": 0.7,
        "max_chars": 280,
    },
}
```

### 3. Context Injection Pipeline

The bot generates replies enriched by four layers of personalized knowledge:

#### Layer 1: Historical Conversation Memory

Stored in SQLite via `memory_store.py`. Each platform stores:
- Last N messages per channel/user ID
- Roles: "user", "assistant"

#### Layer 2: Learned Self-Improvement Lessons

Saved into SQLite `lessons` table when:
- Manual rules detect bad patterns locally
- Automated LLM evaluator flags inconsistencies

#### Layer 3: Semantic Memory Retrieval

Uses ChromaDB backed by `sentence_transformers` with two collections:
- `messages`: All saved chat history
- `facts`: Extracted user knowledge

#### Layer 4: User Profile Persistence

SQLite table `user_profiles` keeps track of key info about individuals across sessions.

## Processing Pipeline Flow

1. Message arrives
2. Preprocessing filters run on raw input
3. Build conversation context
4. Inject all four knowledge layers
5. Invoke `_call_llm()`
6. Apply reactive post-processing
7. Learn From Mistakes Automatically
8. Capture long-term insights
9. Post reply and record activity

## Key Features

| Component | Role |
|----------|------|
| `llm_engine.reply_to_comment()` | Central API endpoint for response synthesis |
| `_call_llm` | Unified interface managing rate-limiting + serialization |
| `_strip_reactions()` | Removes verbose narrative filler |
| `_is_aggressive()` | Guards against offensive replies |
| `_self_evaluate()` | Automatic improvement feedback loop |
| `memory_store` | SQL persistence of recent conversations & lessons |
| `vector_store` | Semantic indexing layer enabling intelligent recall |
| `fact_extractor.process_and_store()` | Asynchronous fact gathering and storage |

## Security Controls

- Link filtering via `_BAD_PHRASES`
- Hate moderation via `_AGGRESSIVE_PHRASES`
- Prompt sanitization to prevent hijacks
- Cooldown-based rate limits for burst prevention

## Environment Variables

```
API_BASE_URL=https://ollama.com/v1
OLLAMA_CLOUD_API_KEY=your_api_key
CHAT_MODEL=gemma4:31b
LLM_MAX_TOKENS=300
LLM_TIMEOUT=15
```

## Platform-Specific Overrides

```
TIKTOK_LLM_MAX_TOKENS=200
TIKTOK_LLM_TEMPERATURE=0.8
TIKTOK_LLM_MAX_CHARS=100

TWITTER_LLM_MAX_TOKENS=300
TWITTER_LLM_TEMPERATURE=0.7
TWITTER_LLM_MAX_CHARS=280

DISCORD_LLM_MAX_TOKENS=300
DISCORD_LLM_TEMPERATURE=0.7
DISCORD_LLM_MAX_CHARS=120
```