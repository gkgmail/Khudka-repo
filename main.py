import os
import re
import time
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired, UserNotParticipant

# --- CONFIGURATION (Koyeb Environment Variables) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
PORT = int(os.environ.get("PORT", 8080))

# Pyrogram Clients Setup
app = Client("bot_service", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)
user = Client("user_service", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, workers=16)

# Concurrency lock
SEMAPHORE = asyncio.Semaphore(2)

# Multi-step batch state storage
USER_STATES = {}

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
    if seconds:
        parts.append(f"{seconds}s")
    return ", ".join(parts) if parts else "0s"

def get_progress_bar(percentage):
    completed = int(percentage // 10)
    remaining = 10 - completed
    return "▰" * completed + "▱" * remaining

# --- STYLISH LIVE PROGRESS TRACKER ---
async def progress_bar(current, total, message, start_time, last_update, action_title):
    now = time.time()
    # 5-second interval Telegram FloodWait se bachne ke liye
    if (now - last_update[0]) < 5.0 and current != total:
        return
    last_update[0] = now

    elapsed_time = max(now - start_time, 0.001)
    speed = current / elapsed_time
    percentage = (current * 100 / total) if total > 0 else 0
    bar = get_progress_bar(percentage)

    remaining_bytes = max(total - current, 0)
    time_left = round(remaining_bytes / speed) if speed > 0 else 0

    eta = time_formatter(time_left * 1000)
    elapsed = time_formatter(elapsed_time * 1000)

    card = (
        f"╭━━━━━━━━━━━━━━━━━━━━╮\n"
        f"┃ ⚡ **{action_title}**\n"
        f"┣━━━━━━━━━━━━━━━━━━━━┫\n"
        f"┃ `[{bar}]` **{percentage:.1f}%**\n"
        f"┃ ⚡ **Speed:** `{humanbytes(speed)}/s`\n"
        f"┃ 💾 **Done:** `{humanbytes(current)} / {humanbytes(total)}`\n"
        f"┃ 📦 **Left:** `{humanbytes(remaining_bytes)}`\n"
        f"┃ ⏳ **ETA:** `{eta}`\n"
        f"┃ ⏱️ **Elapsed:** `{elapsed}`\n"
        f"╰━━━━━━━━━━━━━━━━━━━━╯"
    )
    try:
        await message.edit_text(card)
    except Exception:
        pass

# --- LINK PARSER HELPER ---
def parse_tg_link(link: str):
    regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)"
    match = re.search(regex, link)
    if not match:
        return None, None
    chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
    message_id = int(match.group(4))
    return chat_id, message_id

# --- MESSAGE PROCESSING ENGINE ---
async def process_single_message(client, user_client, target_chat, msg_id, status_msg, chat_id):
    file_path = None
    thumb_path = None
    try:
        target_msg = await user_client.get_messages(target_chat, msg_id)
        if not target_msg or target_msg.empty:
            return False

        # 1. Text Message Cloning
        if not target_msg.media:
            if target_msg.text:
                await client.send_message(chat_id, target_msg.text)
                return True
            return False

        # 2. Check 2GB Limit
        media_obj = getattr(target_msg, target_msg.media.value)
        file_size = getattr(media_obj, "file_size", 0)
        if file_size > 2097152000:
            await client.send_message(chat_id, f"⚠️ **Message {msg_id}:** File 2GB se badi hai (Telegram Bot API limit).")
            return False

        # 3. Media Download
        last_update = [time.time()]
        start_time = time.time()
        file_path = await user_client.download_media(
            target_msg,
            progress=progress_bar,
            progress_args=(status_msg, start_time, last_update, "📥 Downloading...")
        )

        if target_msg.video and target_msg.video.thumbs:
            try:
                thumb_path = await user_client.download_media(target_msg.video.thumbs[0].file_id)
            except Exception:
                thumb_path = None

        # 4. Media Upload
        caption = target_msg.caption or ""
        last_update = [time.time()]
        start_time = time.time()

        if target_msg.video:
            await client.send_video(
                chat_id,
                video=file_path,
                caption=caption,
                thumb=thumb_path,
                duration=target_msg.video.duration,
                width=target_msg.video.width,
                height=target_msg.video.height,
                progress=progress_bar,
                progress_args=(status_msg, start_time, last_update, "🚀 Uploading Video...")
            )
        elif target_msg.document:
            await client.send_document(
                chat_id,
                document=file_path,
                caption=caption,
                thumb=thumb_path,
                progress=progress_bar,
                progress_args=(status_msg, start_time, last_update, "🚀 Uploading Document...")
            )
        elif target_msg.audio:
            await client.send_audio(
                chat_id,
                audio=file_path,
                caption=caption,
                duration=target_msg.audio.duration,
                performer=target_msg.audio.performer,
                title=target_msg.audio.title,
                progress=progress_bar,
                progress_args=(status_msg, start_time, last_update, "🚀 Uploading Audio...")
            )
        elif target_msg.photo:
            await client.send_photo(chat_id, photo=file_path, caption=caption)

        return True

    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
        await client.send_message(chat_id, "❌ **Error:** Userbot account is private channel ka member nahi hai.")
        return "BREAK"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False
    except Exception as e:
        print(f"Error processing {msg_id}: {e}")
        return False
    finally:
        for p in [file_path, thumb_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

# --- BOT COMMANDS & DIALOGUE LOGIC ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    text = (
        "🦅 **Welcome to VILLAN Save Restriction Bot** 🦅\n\n"
        "Main private channels se restricted posts (Videos, PDFs, Audios, Photos, Texts) ko save karne ke liye ready hoon.\n\n"
        "**Features & Commands:**\n"
        "• `/batch` - Step-by-step batch extraction shuru karein\n"
        "• `/cancel` - Active batch request cancel karein\n"
        "• Direct post link ya range link (`link/10-25`) paste karein\n\n"
        "⚡ *Fast MTProto engine with Live Speedometer enabled.*"
    )
    await message.reply_text(text)

@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    if message.chat.id in USER_STATES:
        del USER_STATES[message.chat.id]
        await message.reply_text("🛑 **Batch process cancel kar diya gaya hai.**")
    else:
        await message.reply_text("Kuch bhi active nahi hai cancel karne ke liye.")

@app.on_message(filters.command("batch") & filters.private)
async def batch_start_handler(client, message):
    USER_STATES[message.chat.id] = {"step": "WAITING_FIRST_LINK"}
    await message.reply_text(
        "🔗 **Batch Extraction - Step 1/2**\n\n"
        "Kripya **pehla (start) message ka link** send karein jahan se extraction shuru karni hai.\n\n"
        "*(Process cancel karne ke liye `/cancel` likhein)*"
    )

@app.on_message(filters.text & filters.private)
async def message_dispatcher(client, message):
    user_id = message.chat.id
    text = message.text.strip()

    # 1. Batch Step 1: Receiving First Link
    if user_id in USER_STATES and USER_STATES[user_id].get("step") == "WAITING_FIRST_LINK":
        chat_id, start_id = parse_tg_link(text)
        if not chat_id or not start_id:
            await message.reply_text("❌ **Invalid link!** Kripya sahi Telegram post link bhejiye.")
            return

        USER_STATES[user_id] = {
            "step": "WAITING_LAST_LINK",
            "chat_id": chat_id,
            "start_id": start_id
        }
        await message.reply_text(
            f"✅ **Start Post Locked:** (Message ID: `{start_id}`)\n\n"
            "🔗 **Batch Extraction - Step 2/2**\n\n"
            "Ab **aakhiri (last) message ka link** send karein jahan tak files extract karni hain."
        )
        return

    # 2. Batch Step 2: Receiving Last Link and Executing
    elif user_id in USER_STATES and USER_STATES[user_id].get("step") == "WAITING_LAST_LINK":
        chat_id, end_id = parse_tg_link(text)
        if not chat_id or not end_id:
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
