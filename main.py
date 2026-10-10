import os
import asyncio
from pyrogram import Client, filters

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")

bot = Client("bot_client", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user = Client("user_client", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text("Bot active hai! Command format:\n`/batch <channel_id> <start_id> <end_id>`")

@bot.on_message(filters.command("batch") & filters.private)
async def batch_handler(client, message):
    args = message.text.split()
    if len(args) < 4:
        return await message.reply_text("Galat format! Use: `/batch -100xxxx 10 20`")

    chat_id = int(args[1])
    start_id = int(args[2])
    end_id = int(args[3])

    status = await message.reply_text("Downloading shuru ho rahi hai...")

    for msg_id in range(start_id, end_id + 1):
        try:
            msg = await user.get_messages(chat_id, msg_id)
            if msg and not msg.empty:
                if msg.media:
                    file_path = await user.download_media(msg)
                    await bot.send_document(message.chat.id, document=file_path, caption=msg.caption or "")
                    if os.path.exists(file_path):
                        os.remove(file_path)
                elif msg.text:
                    await bot.send_message(message.chat.id, text=msg.text)
                await asyncio.sleep(2)
        except Exception:
            continue

    await status.edit_text("Batch process complete ho gaya!")

async def main():
    await user.start()
    await bot.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
        
