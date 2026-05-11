"""
SoCandyShop Discord Command Bot
==============================
Commandes slash pour contrôler le backend TikTok.
Exécuté en tant que tâche d'arrière-plan dans l'application FastAPI.

Commandes:
  /status      — État du backend + statut live
  /bot-on      — Activer le chat bot
  /bot-off     — Désactiver le chat bot
  /bot-mode    — Afficher la configuration actuelle
  /live-status — Vérifier si soetsopains est en live
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import memory_store as mem

logger = logging.getLogger("socandyshop-discord-bot")

# ── Config ──────────────────────────────────────────────────
BOT_TOKEN = os.getenv("DISCORD_BOT_COMMAND_TOKEN", os.getenv("DISCORD_BOT_TOKEN", ""))
COMMAND_GUILD_ID = int(os.getenv("DISCORD_COMMAND_GUILD", "0"))  # 0 = global, or set guild ID for instant sync
TARGET_CHANNEL_ID = int(os.getenv("DISCORD_COMMAND_CHANNEL", "1335247887440809996"))
REPLY_CHANNEL_ID = int(os.getenv("DISCORD_REPLY_CHANNEL", "1496876160142016713"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ── Emoji reactions for passive engagement ──────────────────
# Guild custom emojis (SoCandyShop server)
_GUILD_EMOJIS = {
    "Coeur": "<:Coeur:1335304491003674646>",
    "Sad": "<:Sad:1335304952687362099>",
    "Voleur": "<:Voleur:1335304965740302467>",
    "KappaHi": "<:KappaHi:1365337912773836882>",
    "Sosofighting": "<:Sosofighting:1365337914262683780>",
    "SosoHi": "<:SosoHi:1365337916192063499>",
    "SosoNice": "<:SosoNice:1365337917672919130>",
    "Tatadodo": "<:Tatadodo:1365337919249711154>",
    "Tatalol": "<:Tatalol:1365337920860454963>",
    "PoliceSTOP": "<:PoliceSTOP:1368027002786807910>",
    "Voleusemdr": "<:Voleusemdr:1368027018460926083>",
    "VoleuseGrr": "<:VoleuseGrr:1368027030859546755>",
    "Kappapleure": "<:Kappapleure:1368027045107470469>",
    "Kikicoeur": "<:Kikicoeur:1384973076206190612>",
}

_EMOJI_MAP = {
    # Positive / agreement — use guild emojis + unicode
    "oui": ["✅", "👌", _GUILD_EMOJIS["Kikicoeur"]],
    "ouais": ["👌", _GUILD_EMOJIS["Tatalol"]],
    "grave": [_GUILD_EMOJIS["SosoNice"], "💯"],
    "super": [_GUILD_EMOJIS["SosoNice"], "✨", "💯"],
    "cool": ["😎", _GUILD_EMOJIS["SosoHi"]],
    "génial": ["✨", "🎉", _GUILD_EMOJIS["SosoNice"]],
    "bien": ["👍", _GUILD_EMOJIS["Kikicoeur"]],
    "merci": [_GUILD_EMOJIS["Kikicoeur"], "😘"],
    "bravo": ["🎉", "👏", _GUILD_EMOJIS["SosoNice"]],
    # Funny — guild reaction emojis
    "haha": [_GUILD_EMOJIS["Tatalol"], "😂", "💀"],
    "mdr": [_GUILD_EMOJIS["Tatalol"], _GUILD_EMOJIS["Voleusemdr"], "😂"],
    "ptdr": [_GUILD_EMOJIS["Tatalol"], "💀"],
    "lol": [_GUILD_EMOJIS["Tatalol"], "😂"],
    # Love / cute
    "j'adore": [_GUILD_EMOJIS["Kikicoeur"], "😍", "✨"],
    "kiffe": [_GUILD_EMOJIS["Kikicoeur"], "🔥"],
    "aime": [_GUILD_EMOJIS["Kikicoeur"], "❤️"],
    "love": [_GUILD_EMOJIS["Kikicoeur"], "❤️"],
    "trop bien": [_GUILD_EMOJIS["SosoNice"], "🔥"],
    # Surprise / excitement
    "wouah": ["🤩", "✨", "😮"],
    "wow": [_GUILD_EMOJIS["KappaHi"], "🤩"],
    "nooon": ["😱", "😮"],
    # Support / comfort
    "désolé": [_GUILD_EMOJIS["Kikicoeur"], "🫂"],
    "désolée": [_GUILD_EMOJIS["Kikicoeur"], "🫂"],
    "galère": [_GUILD_EMOJIS["Kikicoeur"], "💪"],
    "dur": ["🫂", "💪"],
    "triste": [_GUILD_EMOJIS["Sad"], "🫂"],
    "stress": ["💪", "🫂"],
    # Food / candy (Nana's domain)
    "bonbon": ["🍬", "🍭", "✨"],
    "candy": ["🍬", _GUILD_EMOJIS["SosoNice"]],
    "sucre": ["🍬", "🍭"],
    "faim": ["😋", "🍬"],
    "miam": ["😋", _GUILD_EMOJIS["SosoNice"]],
    # Questions (curious)
    "pourquoi": ["🤔", "👀"],
    "comment": ["🤔", "👀"],
    "où": ["🤔", "👀"],
    "quoi": ["🤔", _GUILD_EMOJIS["KappaHi"]],
    # Disapproval / stop
    "stop": [_GUILD_EMOJIS["PoliceSTOP"]],
    "non": [_GUILD_EMOJIS["PoliceSTOP"], "🙅"],
    "arrête": [_GUILD_EMOJIS["VoleuseGrr"], _GUILD_EMOJIS["PoliceSTOP"]],
    # Fight / dramatique
    "fight": [_GUILD_EMOJIS["Sosofighting"]],
    "grr": [_GUILD_EMOJIS["VoleuseGrr"]],
    "pleure": [_GUILD_EMOJIS["Kappapleure"], "😢"],
}

_DEFAULT_EMOJIS = [
    _GUILD_EMOJIS["Kikicoeur"], _GUILD_EMOJIS["Tatalol"],
    _GUILD_EMOJIS["SosoNice"], _GUILD_EMOJIS["KappaHi"],
    "❤️", "😂", "👍", "🔥", "💯", "✨", "👌", "😍",
]


def _pick_emoji_reaction(text: str) -> str:
    """Pick a relevant emoji based on message content, or a random default.
    Returns discord.py-compatible emoji string (unicode or 'name:id' for custom)."""
    lowered = text.lower()
    candidates = []
    for keyword, emojis in _EMOJI_MAP.items():
        if keyword in lowered:
            candidates.extend(emojis)
    if candidates:
        emoji = random.choice(candidates)
    else:
        emoji = random.choice(_DEFAULT_EMOJIS)
    # Convert <:name:id> format to name:id for discord.py add_reaction
    if emoji.startswith("<:") and emoji.endswith(">"):
        # <:Coeur:1335304491003674646> → Coeur:1335304491003674646
        emoji = emoji[2:-1]
    return emoji


class SoCandyBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.target_channel_id = TARGET_CHANNEL_ID

    async def setup_hook(self) -> None:
        # Synchroniser les commandes slash
        try:
            if COMMAND_GUILD_ID:
                guild = discord.Object(id=COMMAND_GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info(f"Commandes slash Discord synchronisées avec le serveur {COMMAND_GUILD_ID}")
            else:
                await self.tree.sync()
                logger.info("Commandes slash Discord synchronisées globalement")
        except Exception as e:
            logger.warning(f"Échec de la synchronisation des commandes Discord : {e}")

    async def on_ready(self) -> None:
        logger.info(f"Bot de commandes Discord prêt : {self.user} (ID : {self.user.id})")

bot = SoCandyBot()
bot.remove_command("help")  # Remove default help to add our custom one

# ── Shared state accessor (injected at runtime) ────────────
# Set by main.py on startup
_get_live_status: Optional[callable] = None
_get_bot_state: Optional[callable] = None
_bot_control: Optional[callable] = None  # (action: str) -> dict

# ── Commandes Slash ─────────────────────────────────────────

@bot.tree.command(name="status", description="État du backend + statut TikTok live")
async def cmd_status(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🍬 État du Backend SoCandyShop",
        color=0xFF0050,
        timestamp=discord.utils.utcnow(),
    )

    # Santé
    embed.add_field(name="Backend", value="🟢 En cours d'exécution", inline=True)

    # Statut live
    live_info = "Inconnu"
    if _get_live_status:
        try:
            info = await _get_live_status()
            is_live = info.get("live", False)
            viewers = info.get("viewer_count", 0)
            live_info = f"🔴 EN LIVE — {viewers} spectateurs" if is_live else f"⚪ Hors ligne (dernier : {viewers} spectateurs)"
        except Exception as e:
            live_info = f"Erreur : {e}"
    embed.add_field(name="TikTok @soetsopains", value=live_info, inline=True)

    # État du bot
    bot_info = "Inconnu"
    if _get_bot_state:
        try:
            st = await _get_bot_state()
            enabled = st.get("enabled", False)
            connected = st.get("connected", False)
            status = "🟢 Actif" if enabled else "🔴 Arrêté"
            if enabled:
                status += " | " + ("📡 Connecté" if connected else "⏳ En attente du live")
            sent = st.get("messages_sent", 0)
            comments = st.get("comment_count", 0)
            bot_info = f"{status}\nMsgs : {sent} | Chat : {comments}"
        except Exception as e:
            bot_info = f"Erreur : {e}"
    embed.add_field(name="Chat Bot", value=bot_info, inline=False)

    # Twitter Bot
    twitter_info = "Inconnu"
    try:
        import twitter_bot as _tb
        tb = _tb.get_twitter_bot()
        if tb and tb._ready:
            stats = tb.get_state().get("stats", {})
            twitter_info = f"🟢 Actif @{_tb.HANDLE}\nTweets: {stats.get('posts', 0)} | Réponses: {stats.get('replies', 0)}"
        else:
            enabled = _tb.ENABLED
            twitter_info = f"🔴 {'Configuré' if enabled else 'Désactivé'} (non connecté)"
    except Exception as e:
        twitter_info = f"Erreur : {e}"
    embed.add_field(name="Twitter Bot", value=twitter_info, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="bot-on", description="Activer le chat bot TikTok live")
async def cmd_bot_on(interaction: discord.Interaction) -> None:
    if not _bot_control:
        await interaction.response.send_message("❌ Contrôle du bot indisponible.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        result = await _bot_control("start")
        ok = result.get("ok", False)
        msg = result.get("status", result.get("error", "inconnu"))
        if ok:
            await interaction.followup.send(f"✅ Chat bot démarré : {msg}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Échec : {msg}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur : {e}", ephemeral=True)


@bot.tree.command(name="bot-off", description="Désactiver le chat bot TikTok live")
async def cmd_bot_off(interaction: discord.Interaction) -> None:
    if not _bot_control:
        await interaction.response.send_message("❌ Contrôle du bot indisponible.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        result = await _bot_control("stop")
        ok = result.get("ok", False)
        msg = result.get("status", result.get("error", "inconnu"))
        if ok:
            await interaction.followup.send(f"🛑 Chat bot arrêté : {msg}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Échec : {msg}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur : {e}", ephemeral=True)


@bot.tree.command(name="bot-mode", description="Afficher la configuration actuelle du bot")
async def cmd_bot_mode(interaction: discord.Interaction) -> None:
    env = {
        "BOT_ENABLED": os.getenv("BOT_ENABLED", "false"),
        "BOT_GREET_NEW_VIEWERS": os.getenv("BOT_GREET_NEW_VIEWERS", "true"),
        "BOT_THANK_GIFTS": os.getenv("BOT_THANK_GIFTS", "true"),
        "BOT_REPLY_MENTIONS": os.getenv("BOT_REPLY_MENTIONS", "true"),
        "BOT_PROMO_INTERVAL": os.getenv("BOT_PROMO_INTERVAL", "300"),
        "BOT_MAX_MSG_RATE": os.getenv("BOT_MAX_MSG_RATE", "3"),
    }

    lines = []
    for k, v in env.items():
        icon = "🟢" if v.lower() in ("true", "1", "yes") else "🔴" if v.lower() in ("false", "0", "no", "") else "⚪"
        lines.append(f"{icon} `{k}` = `{v}`")

    embed = discord.Embed(
        title="🤖 Configuration du Bot",
        description="Mode observateur actuel :\n\n" + "\n".join(lines),
        color=0x00F2EA,
    )

    # État live
    if _get_bot_state:
        try:
            st = await _get_bot_state()
            embed.add_field(
                name="État d'exécution",
                value=f"Activé : {st.get('enabled', False)}\nConnecté : {st.get('connected', False)}\nMsgs envoyés : {st.get('messages_sent', 0)}\nCommentaires : {st.get('comment_count', 0)}",
                inline=False,
            )
        except Exception:
            pass

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="live-status", description="Vérifier si @soetsopains est en live maintenant")
async def cmd_live_status(interaction: discord.Interaction) -> None:
    if not _get_live_status:
        await interaction.response.send_message("❌ Statut live indisponible.", ephemeral=True)
        return

    info = await _get_live_status()
    is_live = info.get("live", False)
    viewers = info.get("viewer_count", 0)
    title = info.get("title", "")

    color = 0xFF0050 if is_live else 0x808080
    status_text = "🔴 EN LIVE" if is_live else "⚪ Hors ligne"

    embed = discord.Embed(
        title=f"TikTok @soetsopains — {status_text}",
        color=color,
    )
    if is_live:
        embed.add_field(name="Spectateurs", value=str(viewers), inline=True)
        if title:
            embed.add_field(name="Titre", value=title, inline=False)
    else:
        embed.add_field(name="Dernière vérification", value=f"{viewers} spectateurs présents (pas de live)", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Commandes Préfixes ──────────────────────────────────────

@bot.command(name="help")
async def prefix_help(ctx: commands.Context) -> None:
    """Liste toutes les commandes disponibles."""
    embed = discord.Embed(
        title="🍬 Liste des Commandes SoCandyShop",
        description="Utilise les **commandes slash** (`/`) ou les préfixes (`!`)",
        color=0xFF0050,
    )
    embed.add_field(
        name="Commandes Slash",
        value=(
            "`/status` — État du backend + statut live\n"
            "`/bot-on` — Activer le chat bot\n"
            "`/bot-off` — Désactiver le chat bot\n"
            "`/bot-mode` — Afficher la configuration\n"
            "`/live-status` — Vérifier si @soetsopains est en live"
        ),
        inline=False,
    )
    embed.add_field(
        name="Commandes Préfixes",
        value="`!help` — Afficher ce message",
        inline=False,
    )
    embed.set_footer(text="Astuce : Les commandes slash sont préférées — elles s'autocomplètent et montrent les descriptions.")
    await ctx.send(embed=embed)


# ── Discord channel auto-replies ─────────────────────────────
_REPLIES_MENTIONED = [
    "Hey {name} ! Tu me cherches ? 😄",
    "Ouais {name} ? 👀",
    "Quoi {name} ? 😂",
    "Grave {name}, dis-moi !",
    "{name} ! Quoi de neuf ?",
]

_REPLIES_KEYWORD = [
    "Hmm ouais je vois 🤔",
    "Trop vrai ça 😂",
    "Je suis d'accord !",
    "Ouais pareil, je valide ✅",
    "Bref, raconte la suite 💬",
    "Haha trop bien !",
    "Nope, moi je dirais que si 😏",
]

# ════════════════════════════════════════════════════════════
#  Discord channel auto-replies — with persistent memory
# ════════════════════════════════════════════════════════════

import re

@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore self
    if message.author.id == bot.user.id:
        return
    # Ignore DMs — only monitor guild channels
    if message.guild is None:
        return
    # Only monitor our guild
    if COMMAND_GUILD_ID and message.guild.id != COMMAND_GUILD_ID:
        return
    # Ignore bot messages
    if message.author.bot:
        return
    # Process commands first (these don't need LLM)
    await bot.process_commands(message)
    # Don't reply to commands
    if message.content.startswith(("!", "/")):
        return

    # ── Anti-duplicate: skip if already processing this message ──
    msg_id = message.id
    if msg_id in _messages_in_progress:
        return
    _messages_in_progress.add(msg_id)
    # Clean up old entries after 2 minutes
    try:
        if len(_messages_in_progress) > 500:
            _messages_in_progress.clear()
    except Exception:
        pass

    ch_id = str(message.channel.id)
    # Mention detection: @bot OR "nana" anywhere in the text
    text_lower = message.content.lower()
    is_mention = (
        (bot.user and (f"<@{bot.user.id}>" in message.content or f"<@!{bot.user.id}>" in message.content))
        or "nana" in text_lower
    )

    # ── Check if this is a reply to one of Nana's messages ──
    is_reply_to_nana = False
    if message.reference and message.reference.message_id:
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
            is_reply_to_nana = (ref.author.id == bot.user.id)
        except Exception:
            pass

    # ── Smart conversation context: only engage if Nana is in the active thread ──
    # BUT: limit consecutive auto-replies so Nana doesn't loop
    is_in_active_thread = False
    try:
        history = mem.get_history(ch_id, limit=12)  # oldest first
        if history:
            # Count how many times Nana replied consecutively at the end
            consecutive_nana = 0
            for m in reversed(history):
                if m.get("role") == "assistant" and m.get("name") == "Nana":
                    consecutive_nana += 1
                else:
                    break

            # If Nana has replied N times in a row, stop auto-engaging
            # (she's talking to herself / dominating the conversation)
            if consecutive_nana >= _CONVERSATION_CHAIN_LIMIT:
                # Only override: direct mentions still get through
                is_in_active_thread = False
            else:
                # Walk backwards from latest message — find if Nana is in the
                # recent "turn" (last 6 messages).
                recent = history[-6:]  # last 6 messages
                nana_in_recent = any(
                    m.get("role") == "assistant" and m.get("name") == "Nana"
                    for m in recent
                )
                # Nana is in the thread only if she participated recently
                # AND her last message was within the last 4 messages (not stale)
                nana_in_last_4 = any(
                    m.get("role") == "assistant" and m.get("name") == "Nana"
                    for m in history[-4:]
                )
                is_in_active_thread = nana_in_recent and nana_in_last_4
    except Exception:
        pass

    # Combined: message is for Nana if mention, reply to her, or she's in the active thread
    is_for_nana = (
        is_mention
        or is_reply_to_nana
        or is_in_active_thread
    )

    # ── Emoji + GIF reaction path: when not directly addressed, sometimes just react ──
    # This makes Nana feel present without always typing a reply
    if not is_for_nana:
        # Only in channels we monitor
        if message.channel.id != TARGET_CHANNEL_ID:
            return
        # Decide: text reply, emoji/gif reaction, or skip entirely
        roll = random.random()
        if roll > _REPLY_CHANCE:
            # Not replying with text — but maybe react?
            gif_chance = _GIF_REACT_CHANCE  # chance of GIF vs emoji
            emoji_and_gif_chance = _EMOJI_REACT_CHANCE + _GIF_REACT_CHANCE
            if roll < _REPLY_CHANCE + _GIF_REACT_CHANCE:
                # Send a GIF reaction
                try:
                    from gif_reactions import get_reaction_gif
                    gif_url = await get_reaction_gif(message.content)
                    if gif_url:
                        # Small natural delay before sending GIF
                        await asyncio.sleep(random.randint(2, 5))
                        await message.channel.send(gif_url)
                        logger.info(f"[Discord] GIF react on msg from {message.author.display_name}: {gif_url[:60]}")
                    else:
                        # Fallback to emoji if no GIF found
                        emoji = _pick_emoji_reaction(message.content)
                        if emoji:
                            await message.add_reaction(emoji)
                            logger.info(f"[Discord] Emoji react {emoji} on msg from {message.author.display_name} (GIF fallback)")
                except Exception as e:
                    logger.debug(f"[Discord] GIF react failed, trying emoji: {e}")
                    try:
                        emoji = _pick_emoji_reaction(message.content)
                        if emoji:
                            await message.add_reaction(emoji)
                    except Exception:
                        pass
            elif roll < _REPLY_CHANCE + emoji_and_gif_chance:
                # React with an emoji
                emoji = _pick_emoji_reaction(message.content)
                if emoji:
                    try:
                        await message.add_reaction(emoji)
                        logger.info(f"[Discord] Emoji react {emoji} on msg from {message.author.display_name}")
                    except Exception as e:
                        logger.debug(f"[Discord] Emoji react failed: {e}")
            # Either we reacted or we're skipping — either way, no text reply
            return

    # ── Check if someone told Nana she's off-topic (cooldown) ──
    global _OFFTOPIC_COOLDOWN
    off_topic_signals = [
        "hors sujet", "hors-sujet", "hs", "nana hs", "nana t'es hs",
        "nana arrête", "nana suffit", "nana chut", "nana tais",
        "pas le sujet", "on te demande rien", "personne te demande",
        "on parle de ça", "c'est pas le sujet",
    ]
    if any(sig in text_lower for sig in off_topic_signals):
        _OFFTOPIC_COOLDOWN = time.time() + 300  # 5 min cooldown
        logger.info(f"[Discord] Off-topic signal from {message.author.display_name}: '{message.content}' — cooldown 5 min")
        # Still save the message to memory, but don't reply
        mem.save_message(
            channel_id=ch_id,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            role="user",
            content=message.content,
        )
        return

    # If Nana is in off-topic cooldown, skip all non-mention messages
    if time.time() < _OFFTOPIC_COOLDOWN and not is_mention and not is_reply_to_nana:
        logger.info(f"[Discord] Off-topic cooldown active, skipping")
        return

    # ── Detect GIFs / Tenor / Giphy ─────────────────────
    gif_info = ""
    # Check embeds (Discord auto-embeds tenor/giphy links)
    for embed in message.embeds:
        if embed.url and ("tenor.com" in embed.url.lower() or "giphy.com" in embed.url.lower()):
            gif_info = f" [GIF: {embed.url}]"
            break
    # Check attachments
    if not gif_info:
        for att in message.attachments:
            if att.filename.endswith((".gif", ".webp", ".png", ".jpg")):
                gif_info = f" [Image: {att.filename}]"
                break

    # ── Détecter réactions sur ce message ────────────────────
    reaction_info = ""
    for reaction in message.reactions:
        emoji_str = str(reaction.emoji)
        reaction_info += f" [Réaction: {emoji_str} x{reaction.count}]"

    # Enrichir le contenu pour contexte LLM
    enriched_content = message.content
    if gif_info:
        enriched_content += gif_info
    if reaction_info:
        enriched_content += reaction_info

    # Persist user message
    mem.save_message(
        channel_id=ch_id,
        user_id=str(message.author.id),
        user_name=message.author.display_name,
        role="user",
        content=enriched_content,
    )

    # ── Store in MemPalace for semantic recall ──
    try:
        from mempalace_bridge import store_conversation_turn
        asyncio.create_task(store_conversation_turn(
            user_name=message.author.display_name,
            user_id=str(message.author.id),
            content=enriched_content,
            role="user",
            channel_id=ch_id,
        ))
    except ImportError:
        pass
    except Exception:
        pass

    # ── Rate limiting — applies to ALL reply types ──
    global _last_reply_ts, _LLM_FAIL_STREAK, _LLM_COOLDOWN_UNTIL, _hourly_counts
    now = time.time()
    if now - _last_reply_ts < _MIN_REPLY_INTERVAL:
        return

    # ── LLM cooldown: if LLM failed recently, go quiet ──
    if now < _LLM_COOLDOWN_UNTIL:
        logger.info(f"[Discord] LLM cooldown active, skipping reply")
        return

    # ── Per-hour cap ──
    hourly = _hourly_counts.get(ch_id, [])
    hourly = [ts for ts in hourly if now - ts < 3600]  # keep last hour
    _hourly_counts[ch_id] = hourly
    if len(hourly) >= _HOURLY_CAP:
        logger.info(f"[Discord] Hourly cap reached ({len(hourly)}/{_HOURLY_CAP}), skipping")
        return

    # ── Délai naturel avant de répondre (30s pour paraître humain) ──
    delay = 8 + random.randint(0, 7)  # 8-15s pour un effet naturel
    logger.info(f"[Discord] Attente {delay}s avant réponse à {message.author.display_name}")
    await asyncio.sleep(delay)

    # Build history from SQLite (last 10 exchanges)
    raw = mem.get_history(ch_id, limit=12)
    history = [{"role": r["role"], "name": r["name"], "content": r["content"]} for r in raw]

    try:
        from llm_engine import reply_to_comment
        # Construire un contexte riche pour le LLM
        ch_name = f"#{message.channel.name}" if message.channel.name else "DM"
        extra_context = f"Discord {ch_name} — tu discutes avec des copains"
        if is_mention:
            extra_context += ", on t'a appelée directement"
        if gif_info:
            extra_context += " — un copain a envoyé un GIF"
        if reaction_info:
            extra_context += f" — Réaction{reaction_info}"

        # Appel LLM avec instruction de réponse courte
        llm_reply = await asyncio.wait_for(
            reply_to_comment(
                user_name=message.author.display_name,
                comment=enriched_content,
                context=extra_context,
                history=history,
                user_id=str(message.author.id),
                channel_id=ch_id,
                platform="discord",
            ),
            timeout=15.0,  # slightly longer for semantic search
        )

        # TRONQUER intelligemment : garder du sens, max 120 caractères
        if llm_reply:
            llm_reply = llm_reply.strip()
            # ── Hard truncate at 120 chars — LLM often ignores the limit ──
            if len(llm_reply) > 120:
                # Try smart cut at last sentence separator
                cut = llm_reply[:120]
                for delim in ['; ', '. ', ', ', '! ', '? ']:
                    last = cut.rfind(delim)
                    if last > 20:
                        llm_reply = llm_reply[:last + len(delim) - 1].strip()
                        break
                else:
                    last_space = cut.rfind(' ')
                    if last_space > 20:
                        llm_reply = cut[:last_space].strip()
                    else:
                        llm_reply = cut[:117] + "..."
                # Hard cap: if smart cut still too long, force truncate
                if len(llm_reply) > 120:
                    llm_reply = llm_reply[:117] + "..."

        if llm_reply and len(llm_reply.strip()) > 2:
            # ── Dedup: check if Nana recently said something very similar ──
            # Expanded: last 8 Nana messages, threshold 0.55 (stricter)
            try:
                recent_nana = [
                    m for m in history
                    if m.get("role") == "assistant" and m.get("name") == "Nana"
                ][-8:]  # last 8 Nana messages (was 3)
                from difflib import SequenceMatcher
                for prev in recent_nana:
                    prev_text = prev.get("content", "").lower().strip()
                    new_text = llm_reply.lower().strip()
                    similarity = SequenceMatcher(None, prev_text, new_text).ratio()
                    if similarity > 0.55:  # stricter threshold (was 0.6)
                        logger.info(f"[Discord] Dedup blocked (similarity={similarity:.2f}): '{llm_reply}' ≈ '{prev_text}'")
                        # Try once more with explicit "don't repeat" hint
                        retry_messages = messages + [
                            {"role": "assistant", "content": prev_text},
                            {"role": "user", "content": f"{message.author.display_name}: {enriched_content}\n(N'exprime pas la même chose que tout à l'heure, dis quelque chose de différent)"},
                        ]
                        retry_reply = await asyncio.wait_for(
                            reply_to_comment(
                                user_name=message.author.display_name,
                                comment=enriched_content,
                                context=extra_context + " — IMPORTANT : dis quelque chose de différent de ta réponse précédente, varie tes phrases",
                                history=history,
                                user_id=str(message.author.id),
                                channel_id=ch_id,
                                platform="discord",
                            ),
                            timeout=15.0,
                        )
                        if retry_reply:
                            llm_reply = retry_reply.strip()
                        break
            except Exception as e:
                logger.debug(f"[Discord] Dedup check failed: {e}")

            # Reply in the SAME channel where the mention happened
            await message.channel.send(llm_reply)
            _nana_replied_messages.add(message.id)  # Track to prevent double-reply from reaction handler
            # Prune old entries to prevent unbounded growth
            if len(_nana_replied_messages) > 200:
                recent = list(_nana_replied_messages)[-100:]
                _nana_replied_messages.clear()
                _nana_replied_messages.update(recent)
            _last_reply_ts = time.time()
            _hourly_counts.setdefault(ch_id, []).append(_last_reply_ts)
            _LLM_FAIL_STREAK = 0  # reset on success

            # ── Also react with an emoji on the original message ──
            try:
                emoji = _pick_emoji_reaction(enriched_content)
                await message.add_reaction(emoji)
                logger.debug(f"[Discord] Emoji react {emoji} (with text reply)")
            except Exception:
                pass

            # ── Sometimes also send a GIF alongside the text reply (30% chance) ──
            if random.random() < 0.30:
                try:
                    from gif_reactions import get_reaction_gif
                    gif_url = await get_reaction_gif(enriched_content)
                    if gif_url:
                        await message.channel.send(gif_url)
                        logger.info(f"[Discord] GIF sent with text reply: {gif_url[:60]}")
                except Exception:
                    pass
            mem.save_message(
                channel_id=ch_id,
                user_id=str(bot.user.id) if bot.user else None,
                user_name="Nana",
                role="assistant",
                content=llm_reply,
            )
            # ── Store Nana's reply in MemPalace + vector store ──
            try:
                from mempalace_bridge import store_conversation_turn
                asyncio.create_task(store_conversation_turn(
                    user_name="Nana",
                    user_id=str(bot.user.id) if bot.user else "bot",
                    content=llm_reply,
                    role="assistant",
                    channel_id=ch_id,
                ))
            except ImportError:
                pass
            except Exception:
                pass
        else:
            # LLM returned None or empty — track failures
            _LLM_FAIL_STREAK += 1
            if _LLM_FAIL_STREAK >= 3:
                _LLM_COOLDOWN_UNTIL = time.time() + 300  # 5 min cooldown
                _LLM_FAIL_STREAK = 0
                logger.warning("[Discord] LLM failed 3x in a row — cooldown 5 min")
    except Exception as e:
        _LLM_FAIL_STREAK += 1
        if _LLM_FAIL_STREAK >= 3:
            _LLM_COOLDOWN_UNTIL = time.time() + 300  # 5 min cooldown
            _LLM_FAIL_STREAK = 0
            logger.warning(f"[Discord auto-reply] LLM error (cooldown triggered): {e}")
        else:
            logger.debug(f"[Discord auto-reply] LLM error: {e}")


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
    """Surveiller les réactions pour réagir aussi."""
    # Ignore self reactions
    if user.id == bot.user.id:
        return
    # Skip if Nana already replied to this message (prevents double text messages)
    if reaction.message.id in _nana_replied_messages:
        logger.info(f"[Discord reaction] Skipping — Nana already replied to this message")
        return
    # Ignore DMs
    if reaction.message.guild is None:
        return
    # Only monitor our guild
    if COMMAND_GUILD_ID and reaction.message.guild.id != COMMAND_GUILD_ID:
        return
    # Ignore bot reactions
    if user.bot:
        return

    ch_id = str(reaction.message.channel.id)
    emoji = str(reaction.emoji)

    # Log la réaction comme un message système
    mem.save_message(
        channel_id=ch_id,
        user_id=str(user.id),
        user_name=user.display_name,
        role="user",
        content=f"[Réaction: {emoji} sur un message]",
    )

    # Réagir seulement si c'est une réaction "forte" (= beaucoup de monde)
    # ET si le cooldown global permet une réponse
    if reaction.count >= 2:
        # ── Global cooldown check (same as on_message) ──
        if time.time() - _last_reply_ts < _MIN_REPLY_INTERVAL:
            return
        # ── Délai naturel avant de répondre ──
        delay = 8 + random.randint(0, 7)
        logger.info(f"[Discord reaction] Attente {delay}s avant réponse")
        await asyncio.sleep(delay)

        try:
            from llm_engine import reply_to_comment
            ch_name = f"#{reaction.message.channel.name}" if reaction.message.channel.name else "DM"
            llm_reply = await asyncio.wait_for(
                reply_to_comment(
                    user_name=user.display_name,
                    comment=f"a réagi avec {emoji}",
                    context=f"Discord {ch_name} — réaction {emoji} sur un message, réagis naturellement comme une copine",
                    history=mem.get_history(ch_id, limit=5),
                    platform="discord",
                ),
                timeout=12.0,
            )
            if llm_reply:
                llm_reply = llm_reply.strip()
                if len(llm_reply) > 120:
                    cut = llm_reply[:120]
                    for delim in ['; ', '. ', ', ', '! ', '? ']:
                        last = cut.rfind(delim)
                        if last > 20:
                            llm_reply = llm_reply[:last + len(delim) - 1].strip()
                            break
                    else:
                        last_space = cut.rfind(' ')
                        if last_space > 20:
                            llm_reply = cut[:last_space].strip()
                        else:
                            llm_reply = cut[:117] + "..."

            if llm_reply and len(llm_reply.strip()) > 2:
                # Reply in the same channel
                await reaction.message.channel.send(llm_reply)
                mem.save_message(
                    channel_id=ch_id,
                    user_id=str(bot.user.id) if bot.user else None,
                    user_name="Nana",
                    role="assistant",
                    content=llm_reply,
                )
        except Exception as e:
            logger.debug(f"[Discord reaction] LLM error: {e}")


# ── Lifecycle ───────────────────────────────────────────────
_bot_task: Optional[asyncio.Task] = None
_last_reply_ts: float = 0.0
_MIN_REPLY_INTERVAL: float = 30.0  # seconds between ANY replies (all types)
_REPLY_CHANCE: float = 0.45        # 45% chance to reply to non-mention messages (was 0.70)
_EMOJI_REACT_CHANCE: float = 0.20  # 20% chance to emoji-react instead of text reply (was 0.15)
_GIF_REACT_CHANCE: float = 0.10    # 10% chance to send a GIF reaction instead of text reply
_HOURLY_CAP: int = 15              # max Nana messages per hour per channel (was 30)
_hourly_counts: dict[str, list] = {}   # channel_id -> list of timestamps
_LLM_FAIL_STREAK: int = 0          # consecutive LLM failures
_LLM_COOLDOWN_UNTIL: float = 0.0   # timestamp: don't reply until this time
_OFFTOPIC_COOLDOWN: float = 0.0    # timestamp: Nana was told she's off-topic
_nana_replied_messages: set[int] = set()  # message IDs Nana already text-replied to
_messages_in_progress: set[int] = set()     # message IDs currently being processed (anti-duplicate)
_CONVERSATION_CHAIN_LIMIT: int = 2  # max consecutive auto-replies before going quiet (was 3)

async def start_bot_command(
    get_live_status: callable,
    get_bot_state: callable,
    bot_control: callable,
    **kwargs,  # Swallow extra callbacks from main.py
) -> Optional[asyncio.Task]:
    """Start the Discord command bot as a background task."""
    global _bot_task, _get_live_status, _get_bot_state, _bot_control

    if not BOT_TOKEN:
        logger.warning("DISCORD_BOT_COMMAND_TOKEN non défini — bot de commandes désactivé")
        return None

    mem.init_db()
    logger.info("Mémoire conversationnelle initialisée (%s)", mem.DB_PATH)

    _get_live_status = get_live_status
    _get_bot_state = get_bot_state
    _bot_control = bot_control

    if _bot_task and not _bot_task.done():
        logger.info("Bot de commandes Discord déjà en cours d'exécution")
        return _bot_task

    _bot_task = asyncio.create_task(bot.start(BOT_TOKEN), name="discord-commands")
    logger.info("Tâche du bot de commandes Discord démarrée")
    return _bot_task


async def stop_bot_command() -> None:
    """Arrêter proprement le bot de commandes Discord."""
    global _bot_task
    if bot.is_ready():
        await bot.close()
    if _bot_task and not _bot_task.done():
        _bot_task.cancel()
        try:
            await _bot_task
        except asyncio.CancelledError:
            pass
    logger.info("Bot de commandes Discord arrêté")
