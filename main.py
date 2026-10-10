import os
import time
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import FloodWait

# --- CONFIGURATION (Koyeb Environment Variables se aayega) ---
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
PORT = int(os.environ.get("PORT", 8080))

# Clients setup (workers=16 MTProto speed boost ke liye)
app = Client("bot_service", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)
user = Client("user_service", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, workers=16)

# Concurrency lock: Ek waqt par VPS RAM ke hisaab se max 2 heavy downloads
SEMAPHORE = asyncio.Semaphore(2)

# --- 1. KOYEB HEALTH CHECK WEB SERVER ---
async def health_check(request):
    return web.Response(text="Restricted Content Bot is Running ⚡")

async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

# --- 2. THROTTLED PROGRESS BAR (FloodWait Protection) ---
async def progress_bar(current, total, message, start_time, last_update):
    now = time.time()
    # Har 6 seconds se pehle edit request nahi jayegi
    if now - last_update[0] < 6.0 and current != total:
        return
    last_update[0] = now
    
    percentage = (current * 100 / total) if total > 0 else 0
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    speed_mb = speed / (1024 * 1024)
    completed_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    
    text = (
        f"⚡ **Speed:** `{speed_mb:.2f} MB/s`\n"
        f"📊 **Progress:** `{percentage:.1f}%` (`{completed_mb:.1f}/{total_mb:.1f} MB`)"
    )
    try:
        await message.edit_text(text)
    except Exception:
        pass

# --- 3. BOT EVENT HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        "⚡ **High-Speed Restricted Content Bot Active**\n\n"
        "Kisi bhi private channel post ka link yahan send karein."
    )

@app.on_message(filters.regex(r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)") & filters.private)
async def clone_handler(client, message):
    match = message.matches[0]
    
    # Private channel (-100 prefix) ya Public username detect karna
    if match.group(1):
        chat_id = int(f"-100{match.group(1)}")
    else:
        chat_id = match.group(2)
        
    message_id = int(match.group(4))  # Target message ID
    
    status_msg = await message.reply_text("⏳ `Queue mein check ho raha hai...`")
    
    async with SEMAPHORE:
        file_path = None
        thumb_path = None
        try:
            await status_msg.edit_text("📥 `Restricted file fetch ho rahi hai...`")
            
            # Userbot session se restricted content read karna
            target_msg = await user.get_messages(chat_id, message_id)
            if not target_msg or not target_msg.media:
                await status_msg.edit_text("❌ Is message mein koi valid media nahi mila.")
                return

            last_update = [time.time()]
            start_time = time.time()
            
            # Media download
            file_path = await user.download_media(
                target_msg,
                progress=progress_bar,
                progress_args=(status_msg, start_time, last_update)
            )

            # Thumbnail preserve karna (agar video me present ho)
            if target_msg.video and target_msg.video.thumbs:
                try:
                    thumb_path = await user.download_media(target_msg.video.thumbs[0].file_id)
                except Exception:
                    thumb_path = None

            await status_msg.edit_text("🚀 `Uploading to you...`")
            caption = target_msg.caption or ""
            last_update = [time.time()]
            start_time = time.time()

            # Media type detect karke user ko send karna
            if target_msg.video:
                await client.send_video(
                    message.chat.id,
                    video=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    duration=target_msg.video.duration,
                    width=target_msg.video.width,
                    height=target_msg.video.height,
                    progress=progress_bar,
                    progress_args=(status_msg, start_time, last_update)
                )
            elif target_msg.document:
                await client.send_document(
                    message.chat.id,
                    document=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    progress=progress_bar,
                    progress_args=(status_msg, start_time, last_update)
                )
            elif target_msg.audio:
                await client.send_audio(
                    message.chat.id,
                    audio=file_path,
                    caption=caption,
                    duration=target_msg.audio.duration,
                    performer=target_msg.audio.performer,
                    title=target_msg.audio.title,
                    progress=progress_bar,
                    progress_args=(status_msg, start_time, last_update)
                )
            elif target_msg.photo:
                await client.send_photo(
                    message.chat.id,
                    photo=file_path,
                    caption=caption
                )

            await status_msg.delete()

        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            # Koyeb ki storage leak se bachne ke liye file delete karna
            for path in [file_path, thumb_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

# --- 4. STARTUP ENGINE ---
async def main():
    # Pehle Koyeb health check port open karega
    await start_web_server()
    # Uske baad Userbot aur Bot login honge
    await user.start()
    await app.start()
    print("⚡ System live: Koyeb Health Check OK + Bot/Userbot Connected.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
