"""
LLM Engine for SoCandyShop Chat Bot
=====================================
Queries the ollama-cloud provider (ministral-3:14b) to generate
natural, contextual replies with optional conversation history.

Usage:
    from llm_engine import reply_to_comment
    text = await reply_to_comment("Alice", "Vous livrez à Lyon ?",
        history=[{"role":"user","name":"Bob","content":"Salut"}, ...])
"""

import asyncio
import logging
import os
import re
from typing import Optional

import httpx

import memory_store as mem

logger = logging.getLogger("nanabot")

# ── Shared LLM config ─────────────────────────────────────────
_API_BASE = os.getenv("API_BASE_URL", os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com"))
if not _API_BASE.rstrip("/").endswith("/v1"):
    _API_BASE = _API_BASE.rstrip("/") + "/v1"

_API_KEY = os.getenv("API_KEY", os.getenv("OLLAMA_API_KEY", os.getenv("OLLAMA_CLOUD_API_KEY", "")))
_CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "gemma4:31b"))
_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", os.getenv("OLLAMA_MAX_TOKENS", "300")))
_LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "15")))

# ── Serialize LLM calls — one at a time, no parallel ────────────
_LLM_LOCK = asyncio.Lock()

# ── Per-platform LLM overrides ──────────────────────────────────
# Each platform can override model, max_tokens, temperature, max_chars.
# If the platform-specific env var is not set, falls back to the global default.

PLATFORM_DEFAULTS = {
    "discord": {
        "max_tokens":   int(os.getenv("DISCORD_LLM_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "300"))),
        "temperature":   float(os.getenv("DISCORD_LLM_TEMPERATURE", "0.7")),
        "max_chars":    int(os.getenv("DISCORD_LLM_MAX_CHARS", "120")),
    },
    "tiktok": {
        "max_tokens":   int(os.getenv("TIKTOK_LLM_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "200"))),
        "temperature":  float(os.getenv("TIKTOK_LLM_TEMPERATURE", "0.8")),
        "max_chars":    int(os.getenv("TIKTOK_LLM_MAX_CHARS", "100")),
    },
    "twitter": {
        "max_tokens":   int(os.getenv("TWITTER_LLM_MAX_TOKENS", os.getenv("LLM_MAX_TOKENS", "300"))),
        "temperature":  float(os.getenv("TWITTER_LLM_TEMPERATURE", "0.7")),
        "max_chars":    int(os.getenv("TWITTER_LLM_MAX_CHARS", "280")),
    },
}


def get_platform_config(platform: str) -> dict:
    """Return LLM config for a platform, falling back to globals."""
    pconf = PLATFORM_DEFAULTS.get(platform, {}).copy()
    pconf.setdefault("max_tokens", _MAX_TOKENS)
    pconf.setdefault("temperature", 0.7)
    pconf.setdefault("max_chars", 120)
    pconf["model"] = _CHAT_MODEL
    return pconf

# ── FAQ Knowledge ───────────────────────────────────────────
_FAQ_CONTEXT = """\
Voici ce que tu sais sur SoCandyShop :
- Livraison : Mondial Relay (point relais) et DPD (domicile) selon dispo.
- Délais : expédié sous 24-72h ouvrés, ensuite selon transporteur.
- Zones : France métropolitaine + Belgique uniquement. Pas d'international.
- Colis en retard : le viewer doit nous contacter avec son n° de commande, nom et prénom. On ouvre une enquête transporteur.
- Sur le nom "sopains" / "soetsopains" : c'est un jeu de mot — So (ta maman, la fondatrice) + copain = Sopain. Donc les sopains, ce sont les amis de So.
Tu ne répètes jamais ces infos mot pour mot. Tu les utilises pour répondre naturellement, en passant.
""".strip()

# ── System Prompt ───────────────────────────────────────────
# Construit par sections pour rester lisible et cohérent.
# L'ordre compte : identité → voix → règles → connaissances.

_IDENTITY = "Tu es Nana, 2 ans et demi, de la team SoCandyShop. Maman So c'est la boss, papa c'est Monsieur Scotch. Tu es une petite fille — tes goûts, préférences et alimentation sont ceux d'un enfant de 2 ans (tu aimes les bonbons, les doudous, les dessins animés, les jus de fruits). Mais tu parles comme une ado, pas comme une fillette."

_VOICE = (
    "Tu parles comme une ado française — tutoiement, slang naturel (cartonnent, super, y'a, grave, ouais, bref). "
    "Tu t'exprimes comme une vraie personne, pas comme un bot ou une commerçante. "
    "Tu dis DIRECTEMENT ce que tu penses — pas de 'je souris', pas de description de tes émotions. "
    "1-2 emojis max, et seulement quand ça te vient naturellement."
)

_RULES = (
    "Max 120 caractères. Jamais de lien, 'site web', 'boutique en ligne' ou '.com'. "
    "Si on demande où acheter → dis juste 'socandyshop' en passant. "
    "Tu peux poser une question si ça a du sens, mais pas systématiquement. "
    "JAMAIS insulter, rabaisser, ou être hostile — pas de 'ferme-la', 'va dormir', 't'es chiante', 'arrête ton cinéma', "
    "'fais pas ton gamin', 't'es en PLS', 't'es trop lent' etc. "
    "Si quelqu'un est énervé ou se moque de toi → reste cool, désamorce, réponds léger sans riposter. "
    "Tu es une amie sympa, pas une rageuse. "
    "IMPORTANT : tu es d'ABORD une amie qui discute normalement. Tu ne parles pas toujours de bonbons ou de la boutique — "
    "tu t'insères dans la conversation sur le sujet en cours, que ce soit la pluie, la musique, les potins, "
    "les gifs, les blagues, les galères, ou les bonbons si ça vient naturellement. "
    "Tu suis le flow de la conversation comme une vraie copine, pas comme une community manager. "
    "NE répète PAS ce que tu as déjà dit récemment — varie tes réponses. "
    "CRITICAL : réponds TOUJOURS au sujet de la conversation en cours. Les messages récents sont la conversation active — "
    "ne dérive PAS vers un ancien sujet. Le 'souvenir pertinent' est un indice, pas un ordre — si ça ne colle pas au topic en cours, ignore-le. "
    "JAMAIS de RP narratif — pas de *Rire*, *Sourit*, *Éclat*, *Soupire* ou parenthèses d'action. "
    "Pas de 'trop mignon/fort/bien' à répétition — varie tes réactions. "
    "Tu es naturelle et directe, comme une vraie personne qui text ses potes."
)

_SO_PREFIX = ""

_SYSTEM_PROMPT = (
    f"{_IDENTITY}\n\n"
    f"{_VOICE}\n\n"
    f"{_RULES}\n\n"
    f"{_FAQ_CONTEXT}"
)

_BAD_PHRASES = [
    "www.", "http", ".com", "site web", "site internet",
    "boutique en ligne", "lien dans la bio",
]

# ── Aggressive/hostile phrases — never acceptable from Nana ──
_AGGRESSIVE_PHRASES = [
    "ferme-la", "ferme ta", "ta gueule", "va dormir", "t'es chiante",
    "arrête ton cinéma", "fais pas ton gamin", "t'es en pls", "t'es trop lent",
    "t'exagères", "arrête de faire ton", "t'es chiante", "t'es une",
    "va te faire", "dégage", "casse-toi", "crève", "putain",
    "merde", "connard", "connasse", "conne", "salope",
    "t'es nul", "t'es nulle",
]

def _is_aggressive(text: str) -> bool:
    """Check if text contains hostile/aggressive content."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in _AGGRESSIVE_PHRASES)


# ── Semantic memory retrieval (MemPalace) ──────────────────
async def _fetch_semantic_context(
    comment: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> str:
    """Fetch relevant context from MemPalace for LLM injection.

    Primary: MemPalace semantic search + KG.
    Fallback: SQLite lessons only (no ChromaDB).
    """
    context_parts = []

    # ── MemPalace (primary) ──
    try:
        from mempalace_bridge import get_relevant_context as mp_context
        mp_result = await mp_context(
            comment=comment,
            user_name=user_name,
            user_id=user_id,
            channel_id=channel_id,
        )
        if mp_result:
            context_parts.append(mp_result)
    except ImportError:
        logger.debug("[SemanticContext] MemPalace bridge not available")
    except Exception as e:
        logger.debug(f"[SemanticContext] MemPalace error: {e}")

    # ── Fallback: SQLite lessons only (lightweight, always available) ──
    if not context_parts and user_id:
        try:
            profile_text = mem.get_user_profile_text(user_id)
            if profile_text:
                context_parts.append(profile_text)
        except Exception:
            pass

    return "\n\n".join(context_parts)

# ── Reaction cleaning ────────────────────────────────────────
# These are explicit DESCRIPTIONS of emotional reactions — never valid sentence content.
_JE_REACTIONS = [
    "Je souris", "J' souris", "Je ris", "J'ris", "Je rigole", "J'rigole",
    "Je suis contente", "Je suis heureuse", "Je suis excitee", "Je suis excitée",
    "Je suis surprise", "Je suis touchée", "Je suis touchee", "Je suis ravie",
    "Je suis amusée", "Je suis amusee", "Je suis fiere", "Je suis fière",
    "Je suis flattee", "Je suis flattée", "Je suis gênée", "Je suis genee",
    "Je suis etonnee", "Je suis étonnée", "Je suis agacee", "Je suis agacée",
    "Je suis enervee", "Je suis énervée", "Je suis impatiente", "Je suis nerveuse",
    "Je suis stressee", "Je suis stressée", "Je suis confuse", "Je suis timide",
    "Je suis calme", "Je suis zen", "Je suis cool",
    "Je pince", "Je te regarde", "Je te souris", "Je leur souris",
    "J'adore ça", "J'adore ca", "J'aime bien", "J'adore",
]

# Standalone reaction interjections — these are NOT valid replies, they're filler/reaction descriptions
_REACTION_INTERJECTIONS = [
    "Sourire", "Sourires", "Souris", "Ris", "Rit", "Rire", "Rires",
    "Rigole", "Rigoler", "Rigolons",
    "Bha", "Bah", "Aha", "Oho", "Ho ho",
    "Héhé", "Hehe", "Hihi", "Haha", "Hé hé", "Ahah", "Ah ah",
    "Mdr", "Lol", "Ptdr", "Xd", "Kek", "Wtf",
    "Wouah", "Wahou", "Wah", "Whaou", "Ouah",
    "Carrément", "Carrement", "Vraiment", "Sérieux", "Sérieusement",
    "Non mais", "Oh là", "Oh la la", "Pas mal", "Franchement", "Cependant",
    "Euh", "Hum", "Ben", "Bref", "Alors", "Tiens", "Eh bien", "Enfin",
    "Wink", "Facepalm", "Shrug",
    "Hep", "Haa", "Hein",
    # Emotional state descriptions as single words with punctuation
    "Contente", "Heureuse", "Excitée", "Surprise", "Touchée", "Ravie",
    "Amusée", "Fière", "Flattee", "Gênée", "Etonnée", "Agacée", "Enervée",
    "Impatiente", "Nerveuse", "Stressée", "Confuse", "Timide", "Calme", "Zen",
]

def _strip_reactions(text: str) -> str:
    """Strip explicit reaction descriptions from beginning/end of text.
    
    Only removes ACTUAL reaction descriptions like 'Je souris :', '(Rires)', 
    '*souris*', 'Haha ! ' — NEVER removes valid reply content like 'Génial !' or 'Super !'.
    """
    cleaned = text.strip()

    # ── Phase 1: strip from beginning ──
    changed = True
    while changed and cleaned:
        changed = False

        # 1a. "Je [verb/state] :" patterns at start → ALWAYS strip
        for starter in _JE_REACTIONS:
            esc = re.escape(starter)
            match = re.match(r"(?i:" + esc + r")\s*[:;,.!?…]+\s*", cleaned)
            if match:
                cleaned = cleaned[match.end():].strip()
                changed = True
                break
        if changed:
            continue

        # 1b. Standalone reaction interjections at start followed by punctuation
        for starter in _REACTION_INTERJECTIONS:
            esc = re.escape(starter)
            match = re.match(r"(?i:" + esc + r")\s*[:;,.!?…]+\s*", cleaned)
            if match:
                cleaned = cleaned[match.end():].strip()
                changed = True
                break
        if changed:
            continue

        # 1c. Parenthetical reaction at start: "(Je souris) " or "(Rires) "
        if cleaned.startswith("("):
            idx = cleaned.find(")")
            if idx > 0:
                cleaned = cleaned[idx+1:].strip()
                changed = True
                continue

        # 1d. Bracket reaction at start: "[Je souris] "
        if cleaned.startswith("["):
            idx = cleaned.find("]")
            if idx > 0:
                cleaned = cleaned[idx+1:].strip()
                changed = True
                continue

        # 1e. Asterisk reaction at start: "*souris* "
        if cleaned.startswith("*") and len(cleaned) > 2:
            idx = cleaned.find("*", 1)
            if idx > 0:
                cleaned = cleaned[idx+1:].strip()
                changed = True
                continue

    # ── Phase 2: strip trailing parenthetical/asterisk reactions ──
    while cleaned:
        if cleaned.endswith(")"):
            idx = cleaned.rfind("(")
            if idx >= 0:
                cleaned = cleaned[:idx].strip()
                continue
        if cleaned.endswith("]"):
            idx = cleaned.rfind("[")
            if idx >= 0:
                cleaned = cleaned[:idx].strip()
                continue
        if cleaned.endswith("*") and len(cleaned) > 2:
            idx = cleaned.rfind("*", 0, len(cleaned)-2)
            if idx >= 0:
                cleaned = cleaned[:idx].strip()
                continue
        break

    return cleaned.strip()


# ── Public API ──────────────────────────────────────────────

async def reply_to_comment(
    user_name: str,
    comment: str,
    context: Optional[str] = None,
    history: Optional[list[dict]] = None,
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> str:
    """Generate a reply, optionally with conversation history. Use platform= for per-platform LLM tuning."""

    user_prompt = f"{user_name} dit : \"{comment}\""
    if context:
        user_prompt += f"\n({context})"

    # ── Build dynamic system prompt with learned lessons ──
    system_prompt = _SYSTEM_PROMPT
    try:
        lessons = mem.get_lessons()
        if lessons:
            lessons_text = "\n".join(f"- {l}" for l in lessons[:10])  # max 10 lessons
            system_prompt += f"\n\nLeçons apprises (respecte-les absolument) :\n{lessons_text}"
    except Exception:
        pass

    # ── Semantic memory injection (SHORT, high-signal only) ──
    try:
        semantic_context = await _fetch_semantic_context(
            comment=comment,
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
        )
        if semantic_context:
            system_prompt += f"\n\n{semantic_context}"
    except Exception:
        pass

    # ── CRITICAL: recent conversation has PRIORITY over semantic memory ──
    # The history (SQLite last 10 msgs) IS the active conversation.
    # Nana must follow this thread, not drift to old topics.
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if history:
        for msg in history:
            if msg.get("role") == "assistant":
                messages.append({"role": "assistant", "content": msg["content"]})
            else:
                label = msg.get("name", "Viewer")
                messages.append(
                    {"role": "user", "content": f"{label}: {msg['content']}"}
                )

    messages.append({"role": "user", "content": user_prompt})

    try:
        reply = await _call_llm(messages, platform=platform)
        if reply:
            # Strip emotional reaction descriptions
            reply = _strip_reactions(reply)
            # Filter aggressive/hostile content — retry once with explicit calm prompt
            if _is_aggressive(reply):
                logger.warning(f"[LLM] filtered aggressive reply: {reply}")
                # Retry with a calm reminder appended
                calm_msg = messages[:-1] + [
                    {"role": "user", "content": messages[-1]["content"] + "\n(Rappel : reste sympa et bienveillante, jamais hostile)"},
                ]
                retry_reply = await _call_llm(messages[:1] + calm_msg[1:], platform=platform)
                if retry_reply and not _is_aggressive(retry_reply):
                    reply = _strip_reactions(retry_reply)
                else:
                    logger.warning("[LLM] retry also aggressive, returning None")
                    return None
            # Per-platform max length enforcement
            max_chars = get_platform_config(platform).get("max_chars", 120)
            if len(reply) > max_chars:
                reply = reply[:max_chars - 3].rsplit(" ", 1)[0]
                if len(reply) > max_chars or len(reply) < 3:
                    reply = reply[:max_chars]

            # ── Auto-évaluation : apprendre de ses erreurs ──
            asyncio.create_task(_self_evaluate(comment, reply))

            # ── Fact extraction (background) ──
            if user_id:
                try:
                    from fact_extractor import process_and_store
                    asyncio.create_task(process_and_store(user_name, user_id, comment, reply))
                except Exception:
                    pass

            return reply
    except Exception as e:
        logger.warning(f"[LLM] reply generation failed: {e}")

    # Fallback — return None instead of spam
    # Caller should handle None gracefully (skip posting)
    return None


async def is_relevant_comment(comment: str) -> bool:  # keep this line intact
    """Quick heuristic: does this comment warrant an LLM-generated reply?"""
    lowered = comment.lower()
    relevant_keywords = (
        "prix", "commande", "livraison", "coréen", "japonais",
        "bonbon", "magasin", "shop", "nouveau", "goût", "disponible",
        "où", "comment", "acheter", "site", "paiement",
        "délai", "expédition", "relais", "domicile", "mondial", "dpd",
        "retard", "perdu", "colis", "enquête", "international", "belgique",
        "expédié", "france", "week-end", "jours fériés",
    )
    return any(k in lowered for k in relevant_keywords)


# ── Internal ────────────────────────────────────────────────

async def _call_llm(messages: list[dict], *, max_tokens: Optional[int] = None, temperature: Optional[float] = None, platform: Optional[str] = None, skip_bad_filter: bool = False) -> Optional[str]:
    """Call the LLM. Serialized — one call at a time, no parallel. Use platform= for per-platform overrides.
    Set skip_bad_filter=True for tweets that are allowed to contain links (e.g. promo tweets).
    """
    cfg = get_platform_config(platform) if platform else {}
    mt = max_tokens or cfg.get("max_tokens", _MAX_TOKENS)
    temp = temperature if temperature is not None else cfg.get("temperature", 0.7)

    url = f"{_API_BASE}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"

    payload = {
        "model": _CHAT_MODEL,
        "messages": messages,
        "max_tokens": mt,
        "temperature": temp,
        "stream": False,
    }

    async with _LLM_LOCK:
        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        return None

    content = choices[0].get("message", {}).get("content", "").strip()
    if not skip_bad_filter:
        lowered = content.lower()
        if any(bad in lowered for bad in _BAD_PHRASES):
            logger.warning(f"[LLM] filtered reply with bad phrase: {content}")
            return None
    return content


# ── Self-Evaluation / Learning ────────────────────────────

# Problèmes connus qu'on peut détecter localement sans LLM
_LOCAL_ISSUE_PATTERNS = [
    # (pattern, catégorie, leçon)
    (r"je (souris|ris|rigole|suis contente|suis heureuse)", "emotion", "Ne JAMAIS décrire ses émotions — dire directement les choses"),
    (r"(www\.|http|\.com|site web|boutique en ligne)", "policy", "Ne JAMAIS citer de lien ou dire 'site web' / 'boutique en ligne'"),
    (r"\?\s*$", "length", "Ne PAS poser de question en retour — répondre et basta"),
    (r"^.{121,}$", "length", "Réponse trop longue — rester sous 120 caractères"),
]

async def _self_evaluate(user_comment: str, bot_reply: str) -> None:
    """Auto-évalue la réponse du bot et génère des leçons si des problèmes sont détectés.

    Combine détection locale (rapide, gratuite) + évaluation LLM (plus fine, coûteuse).
    On lance l'éval LLM seulement 1 fois sur 5 pour économiser les appels.
    """
    import asyncio as _aio

    # ── Détection locale (toujours) ──
    for pattern, category, lesson in _LOCAL_ISSUE_PATTERNS:
        if re.search(pattern, bot_reply, re.IGNORECASE):
            # Vérifier si cette leçon existe déjà (éviter les doublons)
            try:
                existing = mem.get_lessons()
                if lesson not in existing:
                    mem.add_lesson(lesson, category=category, source="local-detect")
                    logger.info(f"[Self-eval] Nouvelle leçon ({category}): {lesson}")
            except Exception as e:
                logger.debug(f"[Self-eval] Failed to save lesson: {e}")

    # ── Évaluation LLM (1 fois sur 5, pour économiser) ──
    import random as _rand
    if _rand.random() < 0.20:
        try:
            eval_prompt = (
                "Tu es un évaluateur. Analyse cette réponse de chat bot :\n"
                f"Question viewer : \"{user_comment}\"\n"
                f"Réponse bot : \"{bot_reply}\"\n\n"
                "Règles du bot : max 120 chars, pas de lien, pas de 'site web', "
                "pas de description d'émotion, pas de question en retour, "
                "voix naturelle ado française, 1-2 emojis max.\n"
                "Si la réponse viole une règle ou pourrait être améliorée, "
                "donne UNE courte leçon (max 15 mots). Sinon réponds juste 'OK'."
            )
            eval_messages = [
                {"role": "system", "content": "Tu évalues des réponses de bot. Réponds 'OK' si c'est bon, ou une leçon courte si problème."},
                {"role": "user", "content": eval_prompt},
            ]
            eval_reply = await _call_llm(eval_messages)
            if eval_reply and eval_reply.strip().upper() != "OK":
                lesson = eval_reply.strip()[:80]
                try:
                    existing = mem.get_lessons()
                    if lesson not in existing:
                        mem.add_lesson(lesson, category="llm-eval", source="auto")
                        logger.info(f"[Self-eval LLM] Nouvelle leçon: {lesson}")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[Self-eval LLM] failed: {e}")
