import os
import re
import time
import asyncio
from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.errors import (
    FloodWait,
    ChannelPrivate,
    ChatAdminRequired,
    UserNotParticipant,
)

# --- CONFIGURATION (Koyeb Environment Variables) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
PORT = int(os.environ.get("PORT", 8080))

# Pyrogram Clients Setup
app = Client(
    "bot_service",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16,
)
user = Client(
    "user_service",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    workers=16,
)

# Concurrency lock
SEMAPHORE = asyncio.Semaphore(2)

# Multi-step batch state storage
USER_STATES = {}
USERBOT_ONLINE = False


# --- KOYEB HEALTH CHECK WEB SERVER ---
async def health_check(request):
    return web.Response(text="⚡ VILLAN Restricted Bot is Active!")


async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health check running on port {PORT}")


# --- FORMATTING UTILITIES ---
def humanbytes(size):
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"


def time_formatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    
    **\n\n"
            "Ab **aakhiri (last) message ka link** send karein."
        )
        return

    # Step 2: Receiving End Link and Running Batch
    elif user_id in USER_STATES and USER_STATES[user_id].get("step") == "WAITING_LAST_LINK":
        chat_id, end_id = parse_tg_link(text)
        if not chat_id or not end_id:
            await message.reply_text("❌ **Invalid link!** Sahi Telegram channel post link bhejiye.")
            return

        state = USER_STATES.pop(user_id)
        if state["chat_id"] != chat_id:
            await message.reply_text("❌ **Mismatched Channels!** Start aur End links ek hi channel ke hone chahiye.")
            return

        start_id = state["start_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total_files = end_id - start_id + 1
        if total_files > 150:
            await message.reply_text("⚠️ Safety ke liye ek baar mein max 150 posts allow hain.")
            return

        status_msg = await message.reply_text(
            f"🦅 **VILLAN Extraction Engine Started!**\n\n"
            f"📊 **Range:** `{start_id}` se `{end_id}`\n"
            f"📦 **Total Items:** `{total_files}`"
        )

        async with SEMAPHORE:
            for idx, current_id in enumerate(range(start_id, end_id + 1), start=1):
                await status_msg.edit_text(
                    f"🔄 **Processing Item `{idx}/{total_files}`**\n"
                    f"🎯 Message ID: `{current_id}`\n"
                    f"⏳ Queue fetching..."
                )
                res = await process_single_message(client, user, chat_id, current_id, status_msg, user_id)
                if res == "BREAK":
                    break
                await asyncio.sleep(2)

        await status_msg.edit_text("✅ **VILLAN Engine:** Batch extraction complete!")
        return

    # Direct Single or Dash-Range Link
    range_regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)(?:-(\d+))?"
    match = re.search(range_regex, text)
    if match:
        chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
        start_id = int(match.group(4))
        end_id = int(match.group(5)) if match.group(5) else start_id

        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total = end_id - start_id + 1
        if total > 150:
            await message.reply_text("⚠️ Max 150 messages allow hain ek baar mein.")
            return

        status_msg = await message.reply_text(f"⏳ **VILLAN Engine:** `{total}` item(s) queue mein hain...")

        async with SEMAPHORE:
            for idx, current_id in enumerate(range(start_id, end_id + 1), start=1):
                if total > 1:
                    await status_msg.edit_text(f"🔄 **Processing Item `{idx}/{total}`** (ID: `{current_id}`)...")
                res = await process_single_message(client, user, chat_id, current_id, status_msg, user_id)
                if res == "BREAK":
                    break
                if total > 1:
                    await asyncio.sleep(2)

        await status_msg.delete()

# --- MAIN ASYNC ENGINE ---
async def main():
    global USERBOT_ONLINE
    await start_web_server()

    print("Starting Bot Client...")
    await app.start()
    print("Bot Client Online! ✅")

    print("Connecting Userbot Session...")
    try:
        await user.start()
        USERBOT_ONLINE = True
        print("Userbot Client Connected Successfully! ✅")
    except Exception as e:
        USERBOT_ONLINE = False
        print(f"❌ Userbot connection failed: {e}")

    print("🦅 VILLAN Restricted Bot is Fully LIVE!")
    await idle()
    await app.stop()
    if USERBOT_ONLINE:
        await user.stop()

if __name__ == "__main__":
    asyncio.run(main())
                await message.reply_text("❌ **Invalid link!** Kripya sahi Telegram post link bhejiye.")
            return

        state = USER_STATES.pop(user_id)
        if state["chat_id"] != chat_id:
            await message.reply_text("❌ **Mismatched Channels!** Start link aur Last link ek hi channel ke hone chahiye.")
            return

        start_id = state["start_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total_files = end_id - start_id + 1
        if total_files > 150:
            await message.reply_text("⚠️ Safety ke liye ek baar mein maximum 150 messages nikal sakte hain.")
            return

        status_msg = await message.reply_text(
            f"🚀 **VILLAN Batch Engine Started!**\n\n"
            f"📊 **Total Range:** `{start_id}` se `{end_id}`\n"
            f"📦 **Total Items:** `{total_files}`"
        )

        async with SEMAPHORE:
            for idx, current_id in enumerate(range(start_id, end_id + 1), start=1):
                await status_msg.edit_text(
                    f"🔄 **Processing Item `{idx}/{total_files}`**\n"
                    f"🎯 Message ID: `{current_id}`\n"
                    f"⏳ Queue fetching..."
                )
                res = await process_single_message(client, user, chat_id, current_id, status_msg, user_id)
                if res == "BREAK":
                    break
                await asyncio.sleep(2)

        await status_msg.edit_text("✅ **Batch Extraction Successfully Complete!**")
        return

    # 3. Direct Link Handling (Single Post ya Range Link)
    range_regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)(?:-(\d+))?"
    match = re.search(range_regex, text)
    if match:
        chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
        start_id = int(match.group(4))
        end_id = int(match.group(5)) if match.group(5) else start_id

        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total = end_id - start_id + 1
        if total > 150:
            await message.reply_text("⚠️ Ek baar mein max 150 messages allow hain.")
            return

        status_msg = await message.reply_text(f"⏳ **Processing `{total}` item(s)...**")

        async with SEMAPHORE:
            for idx, current_id in enumerate(range(start_id, end_id + 1), start=1):
                if total > 1:
                    await status_msg.edit_text(f"🔄 **Processing Item `{idx}/{total}`** (ID: `{current_id}`)...")
                res = await process_single_message(client, user, chat_id, current_id, status_msg, user_id)
                if res == "BREAK":
                    break
                if total > 1:
                    await asyncio.sleep(2)

        await status_msg.delete()

# --- MAIN ASYNC ENGINE ---
async def main():
    await start_web_server()
    await user.start()
    await app.start()
    print("🦅 VILLAN Restricted Bot is LIVE on Koyeb!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
