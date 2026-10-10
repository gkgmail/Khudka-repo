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
    if seconds:
        parts.append(f"{seconds}s")
    return ", ".join(parts) if parts else "0s"


def get_progress_bar(percentage):
    completed = int(percentage // 10)
    remaining = 10 - completed
    return "▰" * completed + "▱" * remaining


# --- LIVE PROGRESS TRACKER ---
async def progress_bar(
    current, total, message, start_time, last_update, action_title
):
    now = time.time()
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

    card_lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN CLONER** 🦅",
        f"┃ ⚡ **{action_title}**",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ `[{bar}]` **{percentage:.1f}%**",
        f"┃ ⚡ **Speed:** `{humanbytes(speed)}/s`",
        f"┃ 💾 **Done:** `{humanbytes(current)} / {humanbytes(total)}`",
        f"┃ 📦 **Left:** `{humanbytes(remaining_bytes)}`",
        f"┃ ⏳ **ETA:** `{eta}`",
        f"┃ ⏱️ **Elapsed:** `{elapsed}`",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ]
    card = "\n".join(card_lines)

    try:
        await message.edit_text(card)
    except Exception:
        pass


def parse_tg_link(link: str):
    regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)"
    match = re.search(regex, link)
    if not match:
        return None, None
    chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
    message_id = int(match.group(4))
    return chat_id, message_id


# --- MESSAGE PROCESSING ENGINE ---
async def process_single_message(
    client, user_client, target_chat, msg_id, status_msg, chat_id
):
    if not USERBOT_ONLINE:
        await client.send_message(
            chat_id,
            "❌ **Userbot session offline hai!** Koyeb environment me `SESSION_STRING` check karein.",
        )
        return "BREAK"

    file_path = None
    thumb_path = None
    try:
        target_msg = await user_client.get_messages(target_chat, msg_id)
        if not target_msg or target_msg.empty:
            return False

        if not target_msg.media:
            if target_msg.text:
                await client.send_message(chat_id, target_msg.text)
                return True
            return False

        media_obj = getattr(target_msg, target_msg.media.value)
        file_size = getattr(media_obj, "file_size", 0)
        if file_size > 2097152000:
            await client.send_message(
                chat_id, f"⚠️ **Message {msg_id}:** File 2GB se badi hai."
            )
            return False

        last_update = [time.time()]
        start_time = time.time()
        file_path = await user_client.download_media(
            target_msg,
            progress=progress_bar,
            progress_args=(
                status_msg,
                start_time,
                last_update,
                "📥 Downloading...",
            ),
        )

        if target_msg.video and target_msg.video.thumbs:
            try:
                thumb_path = await user_client.download_media(
                    target_msg.video.thumbs[0].file_id
                )
            except Exception:
                thumb_path = None

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
                progress_args=(
                    status_msg,
                    start_time,
                    last_update,
                    "🚀 Uploading Video...",
                ),
            )
        elif target_msg.document:
            await client.send_document(
                chat_id,
                document=file_path,
                caption=caption,
                thumb=thumb_path,
                progress=progress_bar,
                progress_args=(
                    status_msg,
                    start_time,
                    last_update,
                    "🚀 Uploading Document...",
                ),
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
                progress_args=(
                    status_msg,
                    start_time,
                    last_update,
                    "🚀 Uploading Audio...",
                ),
            )
        elif target_msg.photo:
            await client.
            
