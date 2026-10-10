import os
import time
import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
SESSION_STRING = os.environ.get("SESSION_STRING", "your_userbot_session_string")

# Max 16 parallel workers for high concurrency
app = Client("bot_service", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)
user = Client("user_service", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, workers=16)

# Concurrency limiter: ek waqt par max 2 active downloads taaki network & RAM choke na ho
SEMAPHORE = asyncio.Semaphore(2)

# --- THROTTLED PROGRESS BAR (Prevents FloodWait) ---
async def progress_bar(current, total, message, start_time, last_update):
    now = time.time()
    # Kam se kam 6 seconds ke interval par edit karo
    if now - last_update[0] < 6.0 and current != total:
        return
    last_update[0] = now
    
    percentage = current * 100 / total
    speed = current / (now - start_time) if (now - start_time) > 0 else 0
    speed_mb = speed / (1024 * 1024)
    completed_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    
    text = (
        f"**Speed:** `{speed_mb:.2f} MB/s`\n"
        f"**Progress:** `{percentage:.1f}%` (`{completed_mb:.1f}/{total_mb:.1f} MB`)"
    )
    try:
        await message.edit_text(text)
    except Exception:
        pass

# --- CORE HANDLER ---
@app.on_message(filters.command("start"))
async def start_handler(client, message):
    await message.reply_text("⚡ **High-Speed Restricted Content Cloner Active.**\n\nLink bhejo copy karne ke liye.")

@app.on_message(filters.regex(r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(\d+)"))
async def clone_handler(client, message):
    match = message.matches[0]
    chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
    message_id = int(match.group(3))
    
    status_msg = await message.reply_text("⏳ `Queue mein add ho gaya hai...`")
    
    async with SEMAPHORE:
        file_path = None
        try:
            await status_msg.edit_text("📥 `Restricted file fetch ho rahi hai...`")
            
            # Userbot se original message read karo
            target_msg = await user.get_messages(chat_id, message_id)
            if not target_msg or not target_msg.media:
                await status_msg.edit_text("❌ Is message mein koi valid media nahi mila.")
                return

            last_update = [time.time()]
            start_time = time.time()
            
            # Download file using Userbot MTProto
            file_path = await user.download_media(
                target_msg,
                progress=progress_bar,
                progress_args=(status_msg, start_time, last_update)
            )

            await status_msg.edit_text("🚀 `Uploading to you...`")
            caption = target_msg.caption or ""
            last_update = [time.time()]
            start_time = time.time()

            # Direct Bot Upload
            if target_msg.video:
                await client.send_video(
                    message.chat.id,
                    video=file_path,
                    caption=caption,
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
                    progress=progress_bar,
                    progress_args=(status_msg, start_time, last_update)
                )
            elif target_msg.audio:
                await client.send_audio(
                    message.chat.id,
                    audio=file_path,
                    caption=caption,
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
            # Memory leak aur disk storage full hone se rokne ke liye strict cleanup
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

async def main():
    await user.start()
    await app.start()
    print("Bot aur Userbot dono online hain!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
    
