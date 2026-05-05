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
    "Coucou {name} ! 🍬 Moi c'est Nana de socandyshop — t'as une question sur les bonbons ?",
    "Hey {name} ! Ca va ? Je suis de la team socandyshop, t'as envie de découvrir un truc sympa ?",
    "Salut {name} 😊 Moi c'est Nana de socandyshop — je connais tout le stock sur le bout des doigts !",
    "Hello {name} 👋 ! Tu cherches un goût en particulier ? Je peux t'aider à choisir 🍬",
]

_REPLIES_KEYWORD = [
    "🍬 Tu cherches un truc en particulier ? On a des goûts pour tous les styles.",
    "✨ Nos nouveautés arrivent vite — tu peux nous suivre sur socandyshop pour être au courant !",
    "🎁 Les bonbons c'est notre dada — t'as un goût préféré ou tu veux découvrir ?",
    "🍭 Nana is here ! Pose-moi ce que tu veux sur les bonbons.",
    "😋 Perso j'adore les nouveautés ! Tu as testé les derniers arrivages ?",
    "🌈 Les rubans acides c'est une dinguerie — si t'aimes les sensations fortes fonce !",
    "💬 Raconte-moi ce que tu cherches, je connais presque tous les goûts par cœur 😄",
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
    # Only READ in #🐰la-maison-des-sopains (modo-chat)
    if message.channel.id != TARGET_CHANNEL_ID:
        return
    # Process commands first (these don't need LLM)
    await bot.process_commands(message)
    # Don't reply to commands
    if message.content.startswith(("!", "/")):
        return

    ch_id = str(TARGET_CHANNEL_ID)
    # Mention detection: @bot OR "nana" anywhere in the text
    text_lower = message.content.lower()
    is_mention = (
        (bot.user and (f"<@{bot.user.id}>" in message.content or f"<@!{bot.user.id}>" in message.content))
        or "nana" in text_lower
    )

    # ── Détecter GIFs / Tenor / Giphy ─────────────────────
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

    # ── Rate limiting ─────────────────────────────────────────
    global _last_reply_ts
    now = time.time()

    if not is_mention:
        if random.random() > _REPLY_CHANCE:
            return
        if now - _last_reply_ts < _MIN_REPLY_INTERVAL:
            return

    # Build history from SQLite (last 10 exchanges)
    raw = mem.get_history(ch_id, limit=10)
    history = [{"role": r["role"], "name": r["name"], "content": r["content"]} for r in raw]

    try:
        from llm_engine import reply_to_comment
        # Construire un contexte riche pour le LLM
        extra_context = "Discord #🐰la-maison-des-sopains SoCandyShop"
        if gif_info:
            extra_context += " — L'utilisateur a envoyé un GIF"
        if reaction_info:
            extra_context += f" — Réaction{reaction_info}"

        # Appel LLM avec instruction de réponse courte
        import random
        llm_reply = await asyncio.wait_for(
            reply_to_comment(
                user_name=message.author.display_name,
                comment=enriched_content,
                context=extra_context,
                history=history,
            ),
            timeout=12.0,
        )

        # TRONQUER : max 1 phrase, 120 caractères
        if llm_reply:
            llm_reply = llm_reply.strip()
            # Couper à la première fin de phrase
            for delim in ['. ', '! ', '? ', '。', '！', '？']:
                if delim in llm_reply:
                    idx = llm_reply.index(delim) + len(delim) - 1
                    llm_reply = llm_reply[:idx+1].strip()
                    break
            # Fallback : couper brut à 120 chars si toujours trop long
            if len(llm_reply) > 120:
                llm_reply = llm_reply[:117] + "..."

        if llm_reply and len(llm_reply.strip()) > 2:
            reply_ch = bot.get_channel(REPLY_CHANNEL_ID)
            if reply_ch:
                await reply_ch.send(f"*Depuis #🐰la-maison-des-sopains*\n{llm_reply}")
            else:
                logger.warning(f"[Discord] Reply channel {REPLY_CHANNEL_ID} not found")
            _last_reply_ts = time.time()
            mem.save_message(
                channel_id=ch_id,
                user_id=str(bot.user.id) if bot.user else None,
                user_name="Nana",
                role="assistant",
                content=llm_reply,
            )
    except Exception as e:
        logger.debug(f"[Discord auto-reply] LLM error: {e}")


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
    """Surveiller les réactions pour réagir aussi."""
    # Ignore self reactions
    if user.id == bot.user.id:
        return
    # Only READ in #🐰la-maison-des-sopains (modo-chat)
    if reaction.message.channel.id != TARGET_CHANNEL_ID:
        return

    ch_id = str(TARGET_CHANNEL_ID)
    emoji = str(reaction.emoji)

    # Log la réaction comme un message système
    mem.save_message(
        channel_id=ch_id,
        user_id=str(user.id),
        user_name=user.display_name,
        role="user",
        content=f"[Réaction: {emoji} sur un message]",
    )

    # Réagir seulement si mentionné ou si c'est une réaction "forte" (= beaucoup de monde)
    if reaction.count >= 2:
        try:
            from llm_engine import reply_to_comment
            llm_reply = await asyncio.wait_for(
                reply_to_comment(
                    user_name=user.display_name,
                    comment=f"a réagi avec {emoji}",
                    context=f"Discord #🐰la-maison-des-sopains SoCandyShop — forte réaction {emoji} sur un message, reste simple et cool",
                    history=mem.get_history(ch_id, limit=5),
                ),
                timeout=12.0,
            )
            if llm_reply:
                # Tronquer 1 phrase
                for delim in ['. ', '! ', '? ', '。', '！', '？']:
                    if delim in llm_reply:
                        idx = llm_reply.index(delim) + len(delim) - 1
                        llm_reply = llm_reply[:idx+1].strip()
                        break
                if len(llm_reply) > 120:
                    llm_reply = llm_reply[:117] + "..."

            if llm_reply and len(llm_reply.strip()) > 2:
                reply_ch = bot.get_channel(REPLY_CHANNEL_ID)
                if reply_ch:
                    await reply_ch.send(f"*[Réaction dans #modo-chat]*\n{llm_reply}")
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
_MIN_REPLY_INTERVAL: float = 45.0  # seconds between auto-replies
_REPLY_CHANCE: float = 0.35        # 35% chance to reply to non-mention messages

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
