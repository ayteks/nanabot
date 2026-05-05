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

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("socandyshop-tiktok")

# ── Shared LLM config ─────────────────────────────────────────
_API_BASE = os.getenv("API_BASE_URL", os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com"))
if not _API_BASE.rstrip("/").endswith("/v1"):
    _API_BASE = _API_BASE.rstrip("/") + "/v1"

_API_KEY = os.getenv("API_KEY", os.getenv("OLLAMA_API_KEY", os.getenv("OLLAMA_CLOUD_API_KEY", "")))
_CHAT_MODEL = os.getenv("CHAT_MODEL", os.getenv("OLLAMA_CHAT_MODEL", "ministral-3:14b"))
_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", os.getenv("OLLAMA_MAX_TOKENS", "80")))
_LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "15")))

# ── FAQ Knowledge ───────────────────────────────────────────
_FAQ_CONTEXT = """\
Voici ce que tu sais sur SoCandyShop :
- Livraison : Mondial Relay (point relais) et DPD (domicile) selon dispo.
- Délais : expédié sous 24-72h ouvrés, ensuite selon transporteur.
- Zones : France métropolitaine + Belgique uniquement. Pas d'international.
- Colis en retard : le viewer doit nous contacter avec son n° de commande, nom et prénom. On ouvre une enquête transporteur.
Tu ne répètes jamais ces infos mot pour mot. Tu les utilises pour répondre naturellement, en passant.
""".strip()

_SYSTEM_PROMPT = (
    "Tu es Nana, la community manager de SoCandyShop. "
    "Tu réponds en direct sur les lives de manière ultra naturelle, comme entre potes. "
    "RÈGLE ABSOLUE : ta réponse doit faire STRICTEMENT MOINS de 120 caractères (espaces compris). "
    "Sois brève, percutante. Pas de question en retour. "
    "Si quelqu'un demande où acheter, dis juste 'socandyshop' en passant. "
    "Jamais de lien, jamais 'site web', jamais 'boutique en ligne'. "
    "Sois décontractée, pas vendeuse. Réponds comme une humaine chill qui kiffe les bonbons.\n\n"
    f"{_FAQ_CONTEXT}"
)

_BAD_PHRASES = [
    "www.", "http", ".com", "site web", "site internet",
    "boutique en ligne", "lien dans la bio",
]

# ── Public API ──────────────────────────────────────────────

async def reply_to_comment(
    user_name: str,
    comment: str,
    context: Optional[str] = None,
    history: Optional[list[dict]] = None,
) -> str:
    """Generate a reply, optionally with conversation history."""

    user_prompt = (
        f"Le viewer {user_name} dit : \"{comment}\"\n"
        "Réponds TRÈS brièvement. MAX 120 caractères. Pas de question."
    )
    if context:
        user_prompt += f" Contexte : {context}"

    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]

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
        reply = await _call_llm(messages)
        if reply:
            # Hard max length enforcement
            if len(reply) > 120:
                reply = reply[:117].rsplit(" ", 1)[0]
                if len(reply) > 120 or len(reply) < 3:
                    reply = reply[:120]
            return reply
    except Exception as e:
        logger.warning(f"[LLM] reply generation failed: {e}")

    # Fallback — short and vague
    return f"Hey {user_name} ! socandyshop 🍬"


async def is_relevant_comment(comment: str) -> bool:
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

async def _call_llm(messages: list[dict]) -> Optional[str]:
    url = f"{_API_BASE}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"

    payload = {
        "model": _CHAT_MODEL,
        "messages": messages,
        "max_tokens": _MAX_TOKENS,
        "temperature": 0.55,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        return None

    content = choices[0].get("message", {}).get("content", "").strip()
    lowered = content.lower()
    if any(bad in lowered for bad in _BAD_PHRASES):
        logger.warning(f"[LLM] filtered reply with bad phrase: {content}")
        return None
    return content
