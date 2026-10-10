import os
import re
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import FloodWait

# Credentials
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
PORT = int(os.environ.get("PORT", 8080))

bot = Client("bot_client", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user = Client("user_client", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# 1. Koyeb Health Check Web Server
async def web_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# Link se Chat ID aur Message ID nikalne ka function
def parse_tg_link(link: str):
    # Private channel: https://t.me/c/1234567890/55
    match_c = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if match_c:
        chat_id = int("-100" + match_c.group(1))
        msg_id = int(match_c.group(2))
        return chat_id, msg_id

    # Public channel: https://t.me/channel_name/55
    match_pub = re.match(r"https?://t\.me/([^/]+)/(\d+)", link)
    if match_pub:
        return match_pub.group(1), int(match_pub.group(2))

    return None, None

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        "**Bot ready hai!**\n\n"
        "Direct Single link bhejo, ya Batch ke liye command bhejo:\n"
        "`/batch <start_link> <end_link>`\n\n"
        "**Example:**\n"
        "`/batch https://t.me/c/123456/10 https://t.me/c/123456/20`"
    )

# 2. Batch Handler
@bot.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    args = message.text.split()
    if len(args) < 3:
        return await message.reply_text("Format galat hai! Use karein:\n`/batch <start_link> <end_link>`")

    start_chat, start_id = parse_tg_link(args[1])
    end_chat, end_id = parse_tg_link(args[2])

    if not start_chat or not end_chat or start_chat != end_chat:
        return await message.reply_text("Links valid nahi hain ya alag-alag channels ke hain!")

    if start_id > end_id:
        return await message.reply_text("Start ID end ID se chhota hona chahiye!")

    status = await message.reply_text(f"Batch task start hua: Msg {start_id} se {end_id} tak...")

    for msg_id in range(start_id, end_id + 1):
        try:
            msg = await user.get_messages(start_chat, msg_id)
            if not msg or msg.empty:
                continue

            # Media download aur upload
            if msg.media:
                file_path = await user.download_media(msg)
                caption = msg.caption or ""

                if msg.video:
                    await bot.send_video(message.chat.id, video=file_path, caption=caption)
                elif msg.photo:
                    await bot.send_photo(message.chat.id, photo=file_path, caption=caption)
                elif msg.audio:
                    await bot.send_audio(message.chat.id, audio=file_path, caption=caption)
                else:
                    await bot.send_document(message.chat.id, document=file_path, caption=caption)

                if os.path.exists(file_path):
                    os.remove(file_path)
            elif msg.text:
                await bot.send_message(message.chat.id, text=msg.text)

            await asyncio.sleep(2)  # Safe delay

        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
        except Exception:
            continue

    await status.edit_text("Batch process successfully poora ho gaya!")

async def main():
    await user.start()
    await bot.start()
    await web_server()
    print("Bot aur Koyeb Web Server dono start ho chuke hain!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
