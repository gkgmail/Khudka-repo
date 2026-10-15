import os
import re
import time
import shutil
import logging
import asyncio
import tempfile
from aiohttp import web
from pyrogram import Client, filters, idle, enums
from pyrogram.errors import (
    FloodWait,
    ChannelPrivate,
    ChatAdminRequired,
    UserNotParticipant,
)

# Pyrogram ka Client module-level par event loop capture kar leta hai,
# isliye Client banane SE PEHLE ek current loop set karna zaroori hai.
# (uvloop yahan crash karta hai, isliye default asyncio loop hi use karo)
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("VILLAN_BOT")

# --- CONFIGURATION (Koyeb Environment Variables) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", 8080))

# SPEED: ek saath kitni files DOWNLOAD hongi (2-5 best rehta hai)
# NOTE: upload hamesha ORDER me ek-ek karke hoga, taaki sequence na bigde
PARALLEL_TASKS = max(1, int(os.environ.get("PARALLEL_TASKS", "3")))
# Progress card kitne second me update ho
PROGRESS_EVERY = float(os.environ.get("PROGRESS_EVERY", "5"))

# Pyrogram Clients Setup
app = Client(
    "bot_service",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16,
)

user = None
if SESSION_STRING and len(SESSION_STRING) > 30:
    user = Client(
        "user_service",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        workers=16,
    )

SEMAPHORE = asyncio.Semaphore(PARALLEL_TASKS)
USER_STATES = {}
ACTIVE_BATCHES = {}  # user_id -> asyncio.Event (cancel flag)
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
    print(f"🌐 Health server running on port {PORT}", flush=True)


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


async def safe_edit(msg, text):
    """Parallel tasks me edit conflict/FloodWait aaye to silently skip."""
    try:
        await msg.edit_text(text, parse_mode=enums.ParseMode.MARKDOWN)
    except FloodWait:
        pass
    except Exception:
        pass


# --- LIVE PROGRESS TRACKER ---
async def progress_bar(
    current, total, message, start_time, last_update, action_title
):
    now = time.time()
    if (now - last_update[0]) < PROGRESS_EVERY and current != total:
        return
    last_update[0] = now

    elapsed_time = max(now - start_time, 0.001)
    speed = current / elapsed_time
    percentage = (current * 100 / total) if total > 0 else 0
    bar = get_progress_bar(percentage)

    remaining_bytes = max(total - current, 0)
    time_left = round(remaining_bytes / speed) if speed > 0 else 0

    card_lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN CLONER** 🦅",
        f"┃ ⚡ **{action_title}**",
        "┣━━━━━━━━━━━━━━━━━━━━┫",
        f"┃ `[{bar}]` **{percentage:.1f}%**",
        f"┃ ⚡ **Speed:** `{humanbytes(speed)}/s`",
        f"┃ 💾 **Done:** `{humanbytes(current)} / {humanbytes(total)}`",
        f"┃ 📦 **Left:** `{humanbytes(remaining_bytes)}`",
        f"┃ ⏳ **ETA:** `{time_formatter(time_left * 1000)}`",
        f"┃ ⏱️ **Elapsed:** `{time_formatter(elapsed_time * 1000)}`",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ]
    await safe_edit(message, "\n".join(card_lines))


def parse_tg_link(link: str):
    regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)"
    match = re.search(regex, link)
    if not match:
        return None, None
    chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
    message_id = int(match.group(4))
    return chat_id, message_id


# --- PHASE 1: DOWNLOAD (parallel, tez) ---
async def download_one(user_client, src_chat, msg_id, status_msg):
    """Ek message userbot se download karo.
    Returns: info dict | None (skip) | 'BREAK' (fatal) | 'OFFLINE'."""
    if not USERBOT_ONLINE or not user_client:
        return "OFFLINE"

    tmpdir = tempfile.mkdtemp(prefix=f"vln_{msg_id}_")
    try:
        target_msg = await user_client.get_messages(src_chat, msg_id)
        if not target_msg or target_msg.empty:
            return None

        if not target_msg.media:
            if target_msg.text:
                return {"kind": "text", "text": target_msg.text,
                        "tmpdir": tmpdir, "msg_id": msg_id}
            return None

        media_obj = getattr(target_msg, target_msg.media.value)
        file_size = getattr(media_obj, "file_size", 0)
        if file_size > 2097152000:
            return {"kind": "too_big", "msg_id": msg_id, "tmpdir": tmpdir}

        orig_name = os.path.basename(
            getattr(media_obj, "file_name", "") or f"media_{msg_id}"
        )
        dl_path = os.path.join(tmpdir, orig_name)

        last_update = [time.time()]
        start_time = time.time()
        file_path = await user_client.download_media(
            target_msg,
            file_name=dl_path,
            progress=progress_bar,
            progress_args=(
                status_msg, start_time, last_update, "📥 Downloading...",
            ),
        )
        if not file_path or not os.path.exists(file_path):
            return None

        thumb_path = None
        if target_msg.video and target_msg.video.thumbs:
            try:
                thumb_path = await user_client.download_media(
                    target_msg.video.thumbs[0].file_id,
                    file_name=os.path.join(tmpdir, "thumb.jpg"),
                )
            except Exception:
                thumb_path = None

        info = {
            "kind": target_msg.media.value,
            "tmpdir": tmpdir,
            "file_path": file_path,
            "thumb_path": thumb_path,
            "caption": target_msg.caption or "",
            "msg_id": msg_id,
        }
        if target_msg.video:
            info.update({
                "duration": target_msg.video.duration,
                "width": target_msg.video.width,
                "height": target_msg.video.height,
            })
        if target_msg.audio:
            info.update({
                "duration": target_msg.audio.duration,
                "performer": target_msg.audio.performer,
                "title": target_msg.audio.title,
            })
        if target_msg.voice:
            info.update({"duration": target_msg.voice.duration})
        return info

    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
        return "BREAK"
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return None
    except Exception as e:
        print(f"Download error on {msg_id}: {e}", flush=True)
        return None


# --- PHASE 2: UPLOAD (hamesha ORDER me, ek-ek karke) ---
async def upload_one(client, dest_chat, status_msg, info):
    kind = info["kind"]
    try:
        if kind == "text":
            await client.send_message(dest_chat, info["text"])
            return True
        if kind == "too_big":
            await client.send_message(
                dest_chat,
                f"⚠️ **Message {info['msg_id']}:** File 2GB se badi hai.",
            )
            return False

        fp, cap, th = info["file_path"], info["caption"], info.get("thumb_path")
        last_update = [time.time()]
        start_time = time.time()
        pa = (status_msg, start_time, last_update)

        if kind == "video":
            await client.send_video(
                dest_chat, video=fp, caption=cap, thumb=th,
                duration=info.get("duration"),
                width=info.get("width"), height=info.get("height"),
                progress=progress_bar,
                progress_args=pa + ("🚀 Uploading Video...",),
            )
        elif kind == "document":
            await client.send_document(
                dest_chat, document=fp, caption=cap, thumb=th,
                progress=progress_bar,
                progress_args=pa + ("🚀 Uploading Document...",),
            )
        elif kind == "audio":
            await client.send_audio(
                dest_chat, audio=fp, caption=cap,
                duration=info.get("duration"),
                performer=info.get("performer"), title=info.get("title"),
                progress=progress_bar,
                progress_args=pa + ("🚀 Uploading Audio...",),
            )
        elif kind == "photo":
            await client.send_photo(dest_chat, photo=fp, caption=cap)
        elif kind == "voice":
            await client.send_voice(
                dest_chat, voice=fp, caption=cap,
                duration=info.get("duration"),
            )
        elif kind == "video_note":
            await client.send_video_note(dest_chat, video_note=fp)
        else:
            await client.send_document(dest_chat, document=fp, caption=cap)
        return True

    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return False
    except Exception as e:
        print(f"Upload error on {info.get('msg_id')}: {e}", flush=True)
        return False
    finally:
        shutil.rmtree(info["tmpdir"], ignore_errors=True)


# --- BATCH RUNNER: download parallel + upload order me ---
async def run_batch(client, owner_id, src_chat, start_id, end_id, status_msg):
    if not USERBOT_ONLINE or not user:
        await client.send_message(
            owner_id,
            "❌ **Userbot session offline hai!** Koyeb environment me `SESSION_STRING` check karein.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        await safe_edit(status_msg, "❌ **Userbot offline.**")
        return

    ids = list(range(start_id, end_id + 1))
    total = len(ids)
    cancel_event = asyncio.Event()
    ACTIVE_BATCHES[owner_id] = cancel_event
    ok = 0
    fail = 0
    # Ek window me itni files: download parallel, phir order me upload,
    # taaki disk par zyada load na pade aur sequence bhi na bigde
    WINDOW = PARALLEL_TASKS * 2

    async def dl(mid):
        async with SEMAPHORE:
            if cancel_event.is_set():
                return None
            return await download_one(user, src_chat, mid, status_msg)

    try:
        for w in range(0, total, WINDOW):
            if cancel_event.is_set():
                break
            chunk = ids[w:w + WINDOW]
            await safe_edit(
                status_msg,
                f"📥 **Downloading** `{w + 1}`-`{w + len(chunk)}` / `{total}` "
                f"(`{PARALLEL_TASKS}x` parallel)...\n⏹ Cancel: `/cancel`",
            )
            # gather result hamesha input ke ORDER me deta hai
            infos = await asyncio.gather(*(dl(mid) for mid in chunk))

            # Upload hamesha original order me, ek-ek karke
            for info in infos:
                if cancel_event.is_set():
                    break
                if info == "BREAK":
                    await client.send_message(
                        owner_id,
                        "❌ **Error:** Userbot us private channel ka member nahi hai.",
                    )
                    cancel_event.set()
                    break
                if info == "OFFLINE" or info is None:
                    fail += 1
                    continue
                done = ok + fail + 1
                await safe_edit(
                    status_msg,
                    f"🚀 **Uploading** `{done}/{total}` (ID: `{info.get('msg_id')}`)...",
                )
                if await upload_one(client, owner_id, status_msg, info):
                    ok += 1
                else:
                    fail += 1
    finally:
        ACTIVE_BATCHES.pop(owner_id, None)

    if cancel_event.is_set() and (ok + fail < total):
        await safe_edit(status_msg, "🛑 **Batch stopped.**")
    else:
        await safe_edit(
            status_msg,
            f"✅ **Batch complete!**\n✔️ Success: `{ok}` | ❌ Failed: `{fail}`",
        )


# --- BOT HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    print(f"⚡ Processing /start for user {message.chat.id}", flush=True)
    user_status = "🟢 Connected" if USERBOT_ONLINE else "🔴 Disconnected"

    start_lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN RESTRICTION BOT** 🦅",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        "┃ ⚡ **Welcome!**",
        "┃ ",
        "┃ Main private channels se restricted",
        "┃ content instantly save karne ke liye ready hoon.",
        "┃ ",
        f"┃ ⚙️ **Engine Status:** `{user_status}`",
        f"┃ 🚀 **Parallel Download:** `{PARALLEL_TASKS}x`",
        "┃ 📑 **Delivery:** `Order me`",
        "┃ ",
        "┃ 📌 **Commands:**",
        "┃ • `/batch`  - Range extraction",
        "┃ • `/cancel` - Active queue stop",
        "┃ • Direct link paste karein",
        "┃ ",
        "┃ ⚡ *Ultra-Fast Engine by VILLAN*",
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯",
    ]

    try:
        await message.reply_text(
            "\n".join(start_lines), parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        print(f"Card error: {e}, sending plain fallback...", flush=True)
        await message.reply_text(
            f"🦅 VILLAN Bot Active!\nStatus: {user_status}\nSend /batch to start."
        )


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
    uid = message.chat.id
    hit = False
    if uid in USER_STATES:
        USER_STATES.pop(uid, None)
        hit = True
    ev = ACTIVE_BATCHES.get(uid)
    if ev is not None:
        ev.set()
        hit = True
    if hit:
        await message.reply_text("🛑 **VILLAN Engine:** Task cancel kar diya gaya hai.")
    else:
        await message.reply_text("Kuch bhi active nahi hai cancel karne ke liye.")


@app.on_message(filters.command("batch") & filters.private)
async def batch_start_handler(client, message):
    print(f"⚡ Processing /batch for user {message.chat.id}", flush=True)
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
            await message.reply_text("❌ **Invalid link!** Sahi channel link bhejiye.")
            return

        USER_STATES[user_id] = {
            "step": "WAITING_LAST_LINK",
            "chat_id": chat_id,
            "start_id": start_id,
        }
        await message.reply_text(
            f"✅ **Start Locked:** `{start_id}`\n\nAb **aakhiri (last) link** send karein."
        )
        return

    elif (
        user_id in USER_STATES
        and USER_STATES[user_id].get("step") == "WAITING_LAST_LINK"
    ):
        chat_id, end_id = parse_tg_link(text)
        if not chat_id or not end_id:
            await message.reply_text("❌ **Invalid link!** Sahi channel link bhejiye.")
            return

        state = USER_STATES.pop(user_id)
        if state["chat_id"] != chat_id:
            await message.reply_text("❌ Links ek hi channel ke hone chahiye.")
            return

        start_id = state["start_id"]
        if start_id > end_id:
            start_id, end_id = end_id, start_id

        total_files = end_id - start_id + 1
        if total_files > 150:
            await message.reply_text("⚠️ Max 150 posts allow hain.")
            return

        status_msg = await message.reply_text(
            f"🦅 **Batch Started!** `{total_files}` items queued "
            f"(`{PARALLEL_TASKS}x` parallel download, order me delivery)."
        )
        await run_batch(client, user_id, chat_id, start_id, end_id, status_msg)
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
            await message.reply_text("⚠️ Max 150 posts allow hain.")
            return

        status_msg = await message.reply_text(
            f"⏳ `{total}` item(s) processing (`{PARALLEL_TASKS}x` parallel download, order me delivery)..."
        )
        await run_batch(client, user_id, chat_id, start_id, end_id, status_msg)


# --- BACKGROUND USERBOT CONNECT ---
async def start_userbot_background():
    global USERBOT_ONLINE
    if not user:
        print("⚠️ SESSION_STRING missing ya invalid hai.", flush=True)
        return
    print("🔄 Connecting Userbot Session...", flush=True)
    try:
        await asyncio.wait_for(user.start(), timeout=15.0)
        USERBOT_ONLINE = True
        print("✅ Userbot Online & Ready!", flush=True)
    except Exception as e:
        USERBOT_ONLINE = False
        print(f"❌ Userbot connection failed: {e}", flush=True)


# --- MAIN ENGINE ---
async def main():
    await start_web_server()

    print("🚀 Starting Bot Client...", flush=True)
    await app.start()

    me = await app.get_me()
    print("========================================", flush=True)
    print(f"👉 BOT LISTENING AS: @{me.username}", flush=True)
    print(f"👉 PARALLEL DOWNLOADS: {PARALLEL_TASKS}x (upload order me)", flush=True)
    print("========================================", flush=True)

    asyncio.create_task(start_userbot_background())

    await idle()
    await app.stop()
    if user and USERBOT_ONLINE:
        await user.stop()


if __name__ == "__main__":
    # NOTE: asyncio.run() NAYA loop banata hai, jabki Client ne upar wala
    # loop capture kar rakha hai. app.run() current (wahi) loop use karta
    # hai, tabhi handlers aur updates sahi chalenge.
    app.run(main())
