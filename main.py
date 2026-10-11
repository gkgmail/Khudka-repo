
import os
import re
import time
import asyncio
import tempfile
import shutil
import logging
from pathlib import Path

from aiohttp import web
from pyrogram import Client, filters, idle
from pyrogram.errors import (
    FloodWait,
    ChannelPrivate,
    ChatAdminRequired,
    UserNotParticipant,
)

# ==================== KOYEB CONFIG ====================

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# Example: ALLOWED_USER_IDS=123456789,987654321
_raw_ids = os.environ.get(
    "ALLOWED_USER_IDS",
    os.environ.get("OWNER_ID", ""),
)

try:
    ALLOWED_USER_IDS = {
        int(x.strip())
        for x in _raw_ids.split(",")
        if x.strip()
    }
except ValueError as exc:
    raise RuntimeError(
        "ALLOWED_USER_IDS must contain numeric Telegram user IDs."
    ) from exc

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO")
)
log = logging.getLogger("villan")

app = Client(
    "bot_service",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=16,
)

user = (
    Client(
        "user_service",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
        workers=16,
    )
    if SESSION_STRING and len(SESSION_STRING) > 30
    else None
)

SEMAPHORE = asyncio.Semaphore(2)
USER_STATES = {}
RUNNING_JOBS = {}

USERBOT_ONLINE = False
MAX_BATCH_ITEMS = 150
MAX_FILE_SIZE = 2_000_000_000

TEMP_ROOT = (
    Path(tempfile.gettempdir())
    / "villan_restriction_bot"
)


# ==================== SECURITY ====================

def is_allowed(message):
    return bool(
        message.from_user
        and message.from_user.id in ALLOWED_USER_IDS
    )


async def deny_if_not_allowed(message):
    if is_allowed(message):
        return False

    try:
        await message.reply_text(
            "⛔ Aapko is bot ko use karne ki permission nahi hai."
        )
    except Exception:
        pass

    return True


# ==================== KOYEB HEALTH CHECK ====================

async def health_check(request):
    return web.Response(
        text="VILLAN Restricted Bot is Active!"
    )


async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)

    runner = web.AppRunner(server)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )
    await site.start()

    log.info("Health check running on port %s", PORT)


# ==================== FORMATTING ====================

def humanbytes(size):
    if not size:
        return "0 B"

    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0

    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1

    return f"{value:.2f} {units[unit]}"


def time_formatter(milliseconds):
    seconds = int(milliseconds / 1000)
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
    completed = max(
        0,
        min(10, int(percentage // 10)),
    )
    return "▰" * completed + "▱" * (10 - completed)


async def progress_bar(
    current,
    total,
    message,
    start_time,
    last_update,
    action_title,
):
    now = time.time()

    if now - last_update[0] < 5 and current != total:
        return

    last_update[0] = now
    elapsed = max(now - start_time, 0.001)
    speed = current / elapsed
    percentage = current * 100 / total if total else 0
    left = max(total - current, 0)

    eta = time_formatter(
        (round(left / speed) if speed else 0) * 1000
    )

    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN ENGINE**",
        f"┃ ⚡ **{action_title}**",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ `[{get_progress_bar(percentage)}]` "
        f"**{percentage:.1f}%**",
        f"┃ ⚡ **Speed:** `{humanbytes(speed)}/s`",
        f"┃ 💾 **Done:** `{humanbytes(current)} / "
        f"{humanbytes(total)}`",
        f"┃ 📦 **Left:** `{humanbytes(left)}`",
        f"┃ ⏳ **ETA:** `{eta}`",
        f"┃ ⏱️ **Elapsed:** "
        f"`{time_formatter(elapsed * 1000)}`",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ]

    try:
        await message.edit_text("\n".join(lines))
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


# ==================== TELEGRAM LINKS ====================

def parse_tg_link(link):
    pattern = (
        r"https?://t\.me/"
        r"(?:c/(\d+)|([a-zA-Z0-9_]+)/)"
        r"(?:(\d+)/)?(\d+)(?:-(\d+))?"
    )

    match = re.search(pattern, link.strip())

    if not match:
        return None, None

    if match.group(1):
        chat_id = int("-100" + match.group(1))
    else:
        chat_id = match.group(2)

    message_id = int(match.group(5) or match.group(4))

    return chat_id, message_id


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


# ==================== BATCH SUMMARY ====================

def batch_summary(counts, total, cancelled=False):
    processed = sum(counts.values())

    title = (
        "🛑 **Batch cancelled**"
        if cancelled
        else "✅ **Batch finished**"
    )

    return (
        f"{title}\n\n"
        f"📦 **Total:** `{total}`\n"
        f"✅ **Successful:** `{counts['success']}`\n"
        f"❌ **Failed:** `{counts['failed']}`\n"
        f"⏭️ **Skipped:** `{counts['skipped']}`\n"
        f"📊 **Processed:** `{processed}/{total}`"
    )


# ==================== PROCESS ONE MESSAGE ====================

async def process_single_message(
    client,
    user_client,
    target_chat,
    msg_id,
    status_msg,
    chat_id,
):
    if not USERBOT_ONLINE or not user_client:
        await client.send_message(
            chat_id,
            "❌ Userbot offline hai. "
            "Koyeb mein SESSION_STRING check karein.",
        )
        return "BREAK"

    temp_dir = None

    try:
        msg = await user_client.get_messages(
            target_chat,
            msg_id,
        )

        if not msg or getattr(msg, "empty", False):
            return "SKIPPED"

        # Protected media is skipped, not bypassed.
        if getattr(msg, "has_protected_content", False):
            log.info(
                "Skipping protected message %s",
                msg_id,
            )
            return "SKIPPED"

        # Plain text messages
        if not msg.media:
            if msg.text:
                await client.send_message(
                    chat_id,
                    msg.text,
                )
                return "SUCCESS"

            return "SKIPPED"

        media_type = getattr(msg.media, "value", "")
        media_obj = getattr(msg, media_type, None)

        if media_obj is None:
            return "SKIPPED"

        file_size = (
            getattr(media_obj, "file_size", 0) or 0
        )

        if file_size > MAX_FILE_SIZE:
            log.warning(
                "Oversized file in message %s",
                msg_id,
            )
            return "SKIPPED"

        # Unique directory for each message
        TEMP_ROOT.mkdir(
            parents=True,
            exist_ok=True,
        )

        temp_dir = tempfile.mkdtemp(
            prefix="item_",
            dir=str(TEMP_ROOT),
        )

        download_dir = str(Path(temp_dir)) + os.sep

        started = time.time()
        last_update = [time.time()]

        path = await user_client.download_media(
            msg,
            file_name=download_dir,
            progress=progress_bar,
            progress_args=(
                status_msg,
                started,
                last_update,
                "📥 Downloading...",
            ),
        )

        if not path or not os.path.isfile(path):
            return "FAILED"

        caption = msg.caption or ""

        started = time.time()
        last_update = [time.time()]

        args = (
            status_msg,
            started,
            last_update,
            "🚀 Uploading...",
        )

        # Photo
        if msg.photo:
            await client.send_photo(
                chat_id,
                path,
                caption=caption,
            )

        # Video
        elif msg.video:
            thumb = None

            if msg.video.thumbs:
                try:
                    thumb = await user_client.download_media(
                        msg.video.thumbs[0].file_id,
                        file_name=download_dir,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug(
                        "Thumbnail unavailable for %s",
                        msg_id,
                    )

            await client.send_video(
                chat_id,
                path,
                caption=caption,
                thumb=thumb,
                duration=msg.video.duration,
                width=msg.video.width,
                height=msg.video.height,
                progress=progress_bar,
                progress_args=args,
            )

        # Documents and files
        elif msg.document:
            await client.send_document(
                chat_id,
                path,
                caption=caption,
                progress=progress_bar,
                progress_args=args,
            )

        # Music/audio
        elif msg.audio:
            await client.send_audio(
                chat_id,
                path,
                caption=caption,
                duration=msg.audio.duration,
                performer=msg.audio.performer,
                title=msg.audio.title,
                progress=progress_bar,
                progress_args=args,
            )

        # GIF/animation
        elif msg.animation:
            await client.send_animation(
                chat_id,
                path,
                caption=caption,
                progress=progress_bar,
                progress_args=args,
            )

        # Voice notes
        elif msg.voice:
            await client.send_voice(
                chat_id,
                path,
                caption=caption,
                duration=msg.voice.duration,
                progress=progress_bar,
                progress_args=args,
            )

        # Round video notes
        elif msg.video_note:
            await client.send_video_note(
                chat_id,
                path,
                duration=msg.video_note.duration,
                length=msg.video_note.length,
                progress=progress_bar,
                progress_args=args,
            )

        # Stickers
        elif msg.sticker:
            await client.send_sticker(
                chat_id,
                path,
            )

        # Unsupported media type
        else:
            return "SKIPPED"

        return "SUCCESS"

    except asyncio.CancelledError:
        raise

    except (
        ChannelPrivate,
        ChatAdminRequired,
        UserNotParticipant,
    ):
        log.warning(
            "Source access denied for message %s",
            msg_id,
        )
        return "BREAK"

    except FloodWait as exc:
        await asyncio.sleep(exc.value)
        return "FAILED"

    except Exception:
        log.exception(
            "Failed processing message %s",
            msg_id,
        )
        return "FAILED"

    finally:
        if temp_dir:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )


# ==================== BATCH WORKER ====================

async def run_batch(
    client,
    user_id,
    target_chat,
    start_id,
    end_id,
    status_msg,
):
    total = end_id - start_id + 1

    counts = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    cancelled = False

    try:
        async with SEMAPHORE:
            for idx, msg_id in enumerate(
                range(start_id, end_id + 1),
                1,
            ):
                await safe_edit(
                    status_msg,
                    f"🔄 **Processing `{idx}/{total}`**\n"
                    f"🎯 Message ID: `{msg_id}`\n"
                    f"✅ `{counts['success']}`  "
                    f"❌ `{counts['failed']}`  "
                    f"⏭️ `{counts['skipped']}`",
                )

                result = await process_single_message(
                    client,
                    user,
                    target_chat,
                    msg_id,
                    status_msg,
                    user_id,
                )

                if result == "BREAK":
                    counts["failed"] += 1
                    counts["skipped"] += total - idx
                    break

                counts[result.lower()] += 1

                if idx < total:
                    await asyncio.sleep(1.5)

    except asyncio.CancelledError:
        cancelled = True
        raise

    finally:
        task = asyncio.current_task()

        if RUNNING_JOBS.get(user_id) is task:
            RUNNING_JOBS.pop(user_id, None)

        if cancelled:
            remaining = total - sum(counts.values())
            counts["skipped"] += max(0, remaining)

        await safe_edit(
            status_msg,
            batch_summary(
                counts,
                total,
                cancelled,
            ),
        )


# ==================== COMMANDS ====================

@app.on_message(
    filters.command("start") & filters.private
)
async def start_handler(client, message):
    if await deny_if_not_allowed(message):
        return

    status = (
        "🟢 Connected"
        if USERBOT_ONLINE
        else "🔴 Disconnected"
    )

    await message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "┃ 🦅 **VILLAN RESTRICTION BOT**\n"
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫\n"
        "┃ Private bot ready hai.\n"
        f"┃ ⚙️ **Userbot:** `{status}`\n"
        "┃ 📌 **Commands:**\n"
        "┃ • `/batch` - Start/end link range\n"
        "┃ • `/cancel` - Stop running batch\n"
        "┃ • Single post/range link bhejein\n"
        "┃ 🔒 Protected posts skip honge.\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


@app.on_message(
    filters.command("cancel") & filters.private
)
async def cancel_handler(client, message):
    if await deny_if_not_allowed(message):
        return

    uid = message.from_user.id
    task = RUNNING_JOBS.get(uid)

    if task and not task.done():
        task.cancel()

        await message.reply_text(
            "🛑 Cancellation request bhej di. "
            "Final counts status message par aayenge."
        )

    elif uid in USER_STATES:
        USER_STATES.pop(uid, None)

        await message.reply_text(
            "🛑 Batch setup cancel kar diya."
        )

    else:
        await message.reply_text(
            "Koi active batch nahi hai."
        )


@app.on_message(
    filters.command("batch") & filters.private
)
async def batch_start_handler(client, message):
    if await deny_if_not_allowed(message):
        return

    uid = message.from_user.id

    if (
        uid in RUNNING_JOBS
        and not RUNNING_JOBS[uid].done()
    ):
        await message.reply_text(
            "⚠️ Ek batch already chal raha hai. "
            "Pehle /cancel karein."
        )
        return

    USER_STATES[uid] = {
        "step": "WAITING_FIRST_LINK"
    }

    await message.reply_text(
        "🔗 **Batch Step 1/2**\n"
        "Start message ka Telegram link bhejein.\n"
        "`/cancel` se cancel karein."
    )


# ==================== START BATCH ====================

async def launch_batch(
    client,
    message,
    uid,
    chat_id,
    start_id,
    end_id,
):
    old = RUNNING_JOBS.get(uid)

    if old and not old.done():
        await message.reply_text(
            "⚠️ Ek batch already chal raha hai. "
            "Pehle /cancel karein."
        )
        return

    status = await message.reply_text(
        f"🦅 **VILLAN Batch Started**\n"
        f"Range: `{start_id}-{end_id}`\n"
        f"Total: `{end_id - start_id + 1}`"
    )

    task = asyncio.create_task(
        run_batch(
            client,
            uid,
            chat_id,
            start_id,
            end_id,
            status,
        ),
        name=f"villan-batch-{uid}",
    )

    RUNNING_JOBS[uid] = task


# ==================== LINK HANDLER ====================

@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(["start", "batch", "cancel"])
)
async def message_dispatcher(client, message):
    if await deny_if_not_allowed(message):
        return

    uid = message.from_user.id
    value = message.text.strip()

    state = USER_STATES.get(uid)

    if state and state.get("step") == "WAITING_FIRST_LINK":
        chat, first = parse_tg_link(value)

        if not chat or not first:
            await message.reply_text(
                "❌ Invalid Telegram post link. Dobara try karein."
            )
            return

        USER_STATES[uid] = {
            "step": "WAITING_LAST_LINK",
            "chat_id": chat,
            "start_id": first,
        }

        await message.reply_text(
            f"✅ Start message locked: `{first}`\n"
            "Ab last message ka link bhejein."
        )
        return

    if state and state.get("step") == "WAITING_LAST_LINK":
        chat, last = parse_tg_link(value)

        if not chat or not last:
            await message.reply_text(
                "❌ Invalid Telegram post link. Dobara try karein."
            )
            return

        USER_STATES.pop(uid, None)

        if state["chat_id"] != chat:
            await message.reply_text(
                "❌ Start aur end links ek hi channel ke hone chahiye."
            )
            return

        first = state["start_id"]
        first, last = min(first, last), max(first, last)

        if last - first + 1 > MAX_BATCH_ITEMS:
            await message.reply_text(
                f"⚠️ Ek batch mein max {MAX_BATCH_ITEMS} posts allowed hain."
            )
            return

        await launch_batch(
            client, message, uid, chat, first, last
        )
        return

    pattern = (
        r"https?://t\.me/"
        r"(?:c/(\d+)|([a-zA-Z0-9_]+)/)"
        r"(?:(\d+)/)?(\d+)(?:-(\d+))?"
    )

    match = re.search(pattern, value)

    if not match:
        return

    chat = (
        int("-100" + match.group(1))
        if match.group(1)
        else match.group(2)
    )

    first = int(match.group(4))
    last = int(match.group(5) or match.group(4))
    first, last = min(first, last), max(first, last)

    if last - first + 1 > MAX_BATCH_ITEMS:
        await message.reply_text(
            f"⚠️ Ek batch mein max {MAX_BATCH_ITEMS} posts allowed hain."
        )
        return

    await launch_batch(
        client, message, uid, chat, first, last
    )


# ==================== USERBOT CONNECTION ====================

async def start_userbot_background():
    global USERBOT_ONLINE

    if not user:
        log.warning(
            "SESSION_STRING missing or too short; userbot disabled."
        )
        return

    try:
        await user.start()
        USERBOT_ONLINE = True
        log.info("Userbot connected.")

    except Exception:
        USERBOT_ONLINE = False
        log.exception("Userbot connection failed")


# ==================== MAIN ====================

async def main():
    # Old temporary files are removed at startup.
    shutil.rmtree(
        TEMP_ROOT,
        ignore_errors=True,
    )
    TEMP_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    await start_web_server()

    await app.start()
    me = await app.get_me()

    log.info(
        "Bot online as @%s",
        me.username,
    )

    user_task = asyncio.create_task(
        start_userbot_background()
    )

    try:
        await idle()

    finally:
        jobs = list(RUNNING_JOBS.values())

        for task in jobs:
            if not task.done():
                task.cancel()

        if jobs:
            await asyncio.gather(
                *jobs,
                return_exceptions=True,
            )

        user_task.cancel()

        await asyncio.gather(
            user_task,
            return_exceptions=True,
        )

        if user and USERBOT_ONLINE:
            await user.stop()

        await app.stop()

        shutil.rmtree(
            TEMP_ROOT,
            ignore_errors=True,
        )


if __name__ == "__main__":
    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise RuntimeError(
            "Set API_ID, API_HASH and BOT_TOKEN in Koyeb environment variables."
        )

    if not ALLOWED_USER_IDS:
        log.warning(
            "No ALLOWED_USER_IDS / OWNER_ID set: "
            "all users will be denied."
        )

    asyncio.run(main())
