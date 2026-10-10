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
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", 8080))

# Pyrogram Clients Setup (in_memory=True taaki Docker session lock na ho)
app = Client(
    "bot_service",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=16,
)

user = None
if SESSION_STRING and len(SESSION_STRING) > 30:
    user = Client(
        "user_service",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
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
    try:
        await message.edit_text("\n".join(card_lines))
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
    if not USERBOT_ONLINE or not user_client:
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
            await client.send_photo(chat_id, photo=file_path, caption=caption)

        return True

    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
        await client.send_message(
            chat_id,
            "❌ **Error:** Userbot account is private channel ka member nahi hai.",
        )
        return "BREAK"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return False
    except Exception as e:
        print(f"Error on {msg_id}: {e}")
        return False
    finally:
        for p in [file_path, thumb_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# --- VILLAN START COMMAND ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    user_status = "🟢 Connected" if USERBOT_ONLINE else "🔴 Disconnected"

    start_lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **ᴹᴿ ᴠɪʟʟᴀɪɴ RESTRICTION BOT** 🦅",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        "┃ ⚡ **Welcome!**",
        "┃ ",
        "┃ Main private channels se restricted",
        "┃ content (Video, PDF, Audio, Photo)",
        "┃ instantly save karne ke liye ready hoon.",
        "┃ ",
        f"┃ ⚙️ **Engine Status:** `{user_status}`",
        "┃ ",
        "┃ 📌 **Commands:**",
        "┃ • `/batch`  - Step-by-step range extraction",
        "┃ • `/cancel` - Active queue stop karne ke liye",
        "┃ • Direct channel post link paste karein",
        "┃ ",
        "┃ ⚡ *Ultra-Fast MTProto Engine by VILLAN*",
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
    ]
    await message.reply_text("\n".join(start_lines))


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    if message.chat.id in USER_STATES:
        del USER_STATES[message.chat.id]
        await message.reply_text("🛑 **VILLAN Engine:** Task cancel kar diya gaya hai.")
    else:
        await message.reply_text("Kuch bhi active nahi hai cancel karne ke liye.")


@app.on_message(filters.command("batch") & filters.private)
async def batch_start_handler(client, message):
    USER_STATES[message.chat.id] = {"step": "WAITING_FIRST_LINK"}
    await message.reply_text(
        "🔗 **VILLAN Batch System - Step 1/2**\n\n"
        "Kripya **start message ka link** bhejiye jahan se files extract shuru karni hain.\n\n"
        "*(Cancel karne ke liye `/cancel` likhein)*"
    )


@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(["start", "batch", "cancel"])
)
async def message_dispatcher(client, message):
    user_id = message.chat.id
    text = message.text.strip()

    if (
        user_id in USER_STATES
        and USER_STATES[user_id].get("step") == "WAITING_FIRST_LINK"
    ):
        chat_id, start_id = parse_tg_link(text)
        if not chat_id or not start_id:
            await message.reply_text(
                "❌ **Invalid link!** Sahi Telegram channel post link bhejiye."
            )
            return

        USER_STATES[user_id] = {
            "step": "WAITING_LAST_LINK",
            "chat_id": chat_id,
            "start_id": start_id,
        }
        await message.reply_text(
            f"✅ **Start Message Locked:** (ID: `{start_id}`)\n\n"
            "🔗 **VILLAN Batch System - Step 2/2**\n\n"
            "Ab **aakhiri (last) message ka link** send karein."
        )
        return

    elif (
        user_id in USER_STATES
        and USER_STATES[user_id].get("step") == "WAITING_LAST_LINK"
    ):
        chat_id, end_id = parse_tg_link(text)
        if not chat_id or not end_id:
            await message.reply_text(
                "❌ **Invalid link!** Sahi Telegram channel post link bhejiye."
            )
            return

        state = USER_STATES.pop(user_id)
        if state["chat_id"] != chat_id:
            await message.reply_text(
                "❌ **Mismatched Channels!** Start aur End links ek hi channel ke hone chahiye."
            )
            return

        start_id = state["start_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total_files = end_id - start_id + 1
        if total_files > 150:
            await message.reply_text(
                "⚠️ Safety ke liye ek baar mein max 150 posts allow hain."
            )
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
                res = await process_single_message(
                    client, user, chat_id, current_id, status_msg, user_id
                )
                if res == "BREAK":
                    break
                await asyncio.sleep(2)

        await status_msg.edit_text("✅ **VILLAN Engine:** Batch extraction complete!")
        return

    range_regex = (
        r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)(?:-(\d+))?"
    )
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

        status_msg = await message.reply_text(
            f"⏳ **VILLAN Engine:** `{total}` item(s) queue mein hain..."
        )

        async with SEMAPHORE:
            for idx, current_id in enumerate(range(start_id, end_id + 1), start=1):
                if total > 1:
                    await status_msg.edit_text(
                        f"🔄 **Processing Item `{idx}/{total}`** (ID: `{current_id}`)..."
                    )
                res = await process_single_message(
                    client, user, chat_id, current_id, status_msg, user_id
                )
                if res == "BREAK":
                    break
                if total > 1:
                    await asyncio.sleep(2)

        await status_msg.delete()


# --- MAIN ASYNC ENGINE ---
async def main():
    global USERBOT_ONLINE
    await start_web_server()

    print("Clearing old webhooks and starting Bot Client...")
    await app.start()
    try:
        await app.delete_webhook(drop_pending_updates=True)
        print("Webhook cleared successfully! Long polling active.")
    except Exception as e:
        print(f"Webhook cleanup warning: {e}")

    print("Bot Client Online! ✅")

    if user:
        print("Connecting Userbot Session...")
        try:
            await user.start()
            USERBOT_ONLINE = True
            print("Userbot Client Connected Successfully! ✅")
        except Exception as e:
            USERBOT_ONLINE = False
            print(f"❌ Userbot connection failed: {e}")
    else:
        print("⚠️ SESSION_STRING provide nahi kiya gaya ya chhota hai.")

    print("🦅 VILLAN Restricted Bot is Fully LIVE!")
    await idle()
    await app.stop()
    if user and USERBOT_ONLINE:
        await user.stop()


if __name__ == "__main__":
    asyncio.run(main())
