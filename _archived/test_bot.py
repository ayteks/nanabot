"""Quick standalone test for TikTokLive chat connection."""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot-test")

from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, CommentEvent, GiftEvent, LikeEvent, JoinEvent, FollowEvent, ShareEvent


async def main():
    client = TikTokLiveClient(unique_id="@soetsopains")

    @client.on(ConnectEvent)
    async def on_connect(event):
        logger.info(f"✅ CONNECTED to room_id={client.room_id}")

    @client.on(DisconnectEvent)
    async def on_disconnect(event):
        logger.info("🔴 DISCONNECTED")

    @client.on(CommentEvent)
    async def on_comment(event):
        logger.info(f"💬 COMMENT from {event.user.nickname}: {event.comment}")

    @client.on(GiftEvent)
    async def on_gift(event):
        logger.info(f"🎁 GIFT from {event.user.nickname}: {event.gift.name} x{event.repeat_count}")

    @client.on(LikeEvent)
    async def on_like(event):
        logger.info(f"❤️ LIKE batch count={event.count}")

    @client.on(JoinEvent)
    async def on_join(event):
        logger.info(f"👋 JOIN {event.user.nickname}")

    @client.on(FollowEvent)
    async def on_follow(event):
        logger.info(f"⭐ FOLLOW {event.user.nickname}")

    @client.on(ShareEvent)
    async def on_share(event):
        logger.info(f"🚀 SHARE {event.user.nickname}")

    is_live = await client.is_live()
    logger.info(f"is_live={is_live}")
    if not is_live:
        logger.warning("Streamer not live, skipping connect.")
        return

    logger.info("Connecting to live chat...")
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
    except asyncio.TimeoutError:
        logger.error("Connection timed out after 30s")
    except Exception as e:
        logger.error(f"Connection error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
