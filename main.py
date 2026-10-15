import os
import re
import time
import json
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
    PhoneNumberInvalid,
    PhoneNumberBanned,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PasswordHashInvalid,
    PeerIdInvalid,
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
# /login command sirf is Telegram user ID ke liye (0 = sab ke liye khula).
# Apni ID @userinfobot se le sakte ho. Bot public hai to set karna behtar hai.
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
# Per-user sessions, allowlist aur paid subscriptions ki files
# (container lifetime tak rehti hain - redeploy par reset)
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "sessions.json")
ALLOWED_FILE = os.environ.get("ALLOWED_FILE", "allowed_users.json")
SUBS_FILE = os.environ.get("SUBS_FILE", "subscriptions.json")

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
LOGIN_STATES = {}  # user_id -> {"step", "phone", "phone_code_hash", "tmp"}
USER_CLIENTS = {}  # user_id -> running pyrogram Client (per-user userbot)
_OWNER_UB_STARTED = False


# --- JSON STORAGE + PERMISSION HELPERS ---
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"save {path} failed: {e}", flush=True)


def _load_allowed():
    data = _load_json(ALLOWED_FILE, [])
    return {int(x) for x in data if str(x).isdigit()}


def _save_allowed(allowed):
    _save_json(ALLOWED_FILE, sorted(allowed))


def _get_allowed():
    allowed = _load_allowed()
    for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(","):
        if x.isdigit():
            allowed.add(int(x))
    if OWNER_ID:
        allowed.add(OWNER_ID)
    return allowed


def _is_owner(uid: int) -> bool:
    return OWNER_ID != 0 and uid == OWNER_ID


def _load_subs():
    return _load_json(SUBS_FILE, {})


def _save_subs(subs):
    _save_json(SUBS_FILE, subs)


def _sub_remaining(uid: int) -> int:
    """Paid subscription me kitne seconds bache hain (0 = koi active plan nahi)."""
    sub = _load_subs().get(str(uid))
    if not sub:
        return 0
    return max(0, int(sub.get("expiry", 0) - time.time()))


def _is_allowed(uid: int) -> bool:
    if OWNER_ID == 0:
        return True  # open mode: sab allowed
    if uid in _get_allowed():
        return True  # free (lifetime) access
    return _sub_remaining(uid) > 0  # paid access (expiry tak)


async def _logout_user(uid):
    c = USER_CLIENTS.pop(uid, None)
    if c is not None:
        try:
            await c.disconnect()
        except Exception:
            pass
    sessions = _load_json(SESSIONS_FILE, {})
    if str(uid) in sessions:
        del sessions[str(uid)]
        _save_json(SESSIONS_FILE, sessions)


async def get_userbot(uid):
    """Is user ka userbot client lao (per-user session, owner fallback)."""
    global _OWNER_UB_STARTED
    if uid in USER_CLIENTS:
        return USER_CLIENTS[uid]
    sessions = _load_json(SESSIONS_FILE, {})
    sess = sessions.get(str(uid))
    if sess:
        c = Client(
            f"user_{uid}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=sess,
            workers=16,
        )
        try:
            await asyncio.wait_for(c.start(), timeout=20.0)
        except Exception as e:
            print(f"userbot start failed for {uid}: {e}", flush=True)
            return None
        USER_CLIENTS[uid] = c
        return c
    # Owner fallback: purana global SESSION_STRING
    if user is not None and (OWNER_ID == 0 or uid == OWNER_ID):
        if not _OWNER_UB_STARTED:
            try:
                await asyncio.wait_for(user.start(), timeout=20.0)
                _OWNER_UB_STARTED = True
            except Exception as e:
                print(f"owner userbot start failed: {e}", flush=True)
                return None
        return user
    return None


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


# --- BATCH STATS (poore batch ka live hisaab) ---
class BatchStats:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.start = time.time()
        self.lock = asyncio.Lock()


# --- LIVE PROGRESS TRACKER (file + batch dashboard) ---
async def progress_bar(
    current, total, message, start_time, last_update, action_title, stats=None
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

    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN CLONER** 🦅",
    ]
    if stats is not None:
        async with stats.lock:
            done, ok, fail, total_b, bstart = (
                stats.done, stats.ok, stats.fail, stats.total, stats.start,
            )
        b_pct = (done * 100 / total_b) if total_b else 0
        avg = max(now - bstart, 0.001) / max(done, 1)
        eta_b = avg * max(total_b - done, 0)
        lines += [
            f"┃ 📦 **Batch:** `{done}/{total_b}` (`{b_pct:.0f}%`)",
            f"┃ ✅ `{ok}` ok • ❌ `{fail}` fail",
            f"┃ ⏳ **Batch ETA:** `~{time_formatter(eta_b * 1000)}`",
            "┣━━━━━━━━━━━━━━━━━━━━┫",
        ]
    lines += [
        f"┃ ⚡ **{action_title}**",
        f"┃ `[{bar}]` **{percentage:.1f}%**",
        f"┃ 🚀 **Speed:** `{humanbytes(speed)}/s`",
        f"┃ 💾 **Downloaded:** `{humanbytes(current)} / {humanbytes(total)}`",
        f"┃ ⏳ **File ETA:** `{time_formatter(time_left * 1000)}`",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
    ]
    await safe_edit(message, "\n".join(lines))


def parse_tg_link(link: str):
    regex = r"https://t\.me/(?:c/(\d+)|([a-zA-Z0-9_]+))/(?:(\d+)/)?(\d+)"
    match = re.search(regex, link)
    if not match:
        return None, None
    chat_id = int(f"-100{match.group(1)}") if match.group(1) else match.group(2)
    message_id = int(match.group(4))
    return chat_id, message_id


# --- PHASE 1: DOWNLOAD (parallel, tez) ---
async def _fetch_with_warmup(user_client, src_chat, msg_id):
    """get_messages karo. 'Peer id invalid' aaye to dialogs sync karke
    peer cache warm karo aur ek baar retry karo (fresh login me hota hai)."""
    try:
        return await user_client.get_messages(src_chat, msg_id)
    except PeerIdInvalid:
        try:
            await user_client.get_dialogs(limit=500)
        except Exception as e:
            print(f"dialog warmup failed: {e}", flush=True)
        return await user_client.get_messages(src_chat, msg_id)


async def download_one(user_client, src_chat, msg_id, status_msg, stats=None):
    """Ek message userbot se download karo.
    Returns: info dict | None (skip) | 'BREAK' (fatal) | 'OFFLINE' | 'PEER_INVALID'."""
    if not user_client:
        return "OFFLINE"

    tmpdir = tempfile.mkdtemp(prefix=f"vln_{msg_id}_")
    try:
        target_msg = await _fetch_with_warmup(user_client, src_chat, msg_id)
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
                status_msg, start_time, last_update,
                f"📥 Downloading • ID `{msg_id}`", stats,
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

    except PeerIdInvalid:
        # Retry ke baad bhi peer nahi mila = account channel ka member nahi
        return "PEER_INVALID"
    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
        return "BREAK"
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return None
    except Exception as e:
        print(f"Download error on {msg_id}: {e}", flush=True)
        return None


# --- PHASE 2: UPLOAD (hamesha ORDER me, ek-ek karke) ---
async def upload_one(client, dest_chat, status_msg, info, stats=None):
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
        title = f"🚀 Uploading • ID `{info.get('msg_id')}`"

        if kind == "video":
            await client.send_video(
                dest_chat, video=fp, caption=cap, thumb=th,
                duration=info.get("duration"),
                width=info.get("width"), height=info.get("height"),
                progress=progress_bar,
                progress_args=pa + (title, stats),
            )
        elif kind == "document":
            await client.send_document(
                dest_chat, document=fp, caption=cap, thumb=th,
                progress=progress_bar,
                progress_args=pa + (title, stats),
            )
        elif kind == "audio":
            await client.send_audio(
                dest_chat, audio=fp, caption=cap,
                duration=info.get("duration"),
                performer=info.get("performer"), title=info.get("title"),
                progress=progress_bar,
                progress_args=pa + (title, stats),
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
    # Har user ka apna userbot session use hoga
    userbot = await get_userbot(owner_id)
    if not userbot:
        await client.send_message(
            owner_id,
            "❌ **Pehle /login karke apne number se login karo.**",
        )
        await safe_edit(status_msg, "❌ **Login required. Pehle /login karo.**")
        return

    ids = list(range(start_id, end_id + 1))
    total = len(ids)
    cancel_event = asyncio.Event()
    ACTIVE_BATCHES[owner_id] = cancel_event
    stats = BatchStats(total)
    # Ek window me itni files: download parallel, phir order me upload,
    # taaki disk par zyada load na pade aur sequence bhi na bigde
    WINDOW = PARALLEL_TASKS * 2

    async def dl(mid):
        async with SEMAPHORE:
            if cancel_event.is_set():
                return None
            return await download_one(userbot, src_chat, mid, status_msg, stats)

    try:
        for w in range(0, total, WINDOW):
            if cancel_event.is_set():
                break
            chunk = ids[w:w + WINDOW]
            await safe_edit(
                status_msg,
                f"📦 **Batch:** `{w + 1}`-`{w + len(chunk)}` / `{total}` start ho raha hai...\n"
                f"(`{PARALLEL_TASKS}x` parallel, order me delivery)\n⏹ Cancel: `/cancel`",
            )
            # gather result hamesha input ke ORDER me deta hai
            infos = await asyncio.gather(*(dl(mid) for mid in chunk))

            # Upload hamesha original order me, ek-ek karke
            for info in infos:
                if cancel_event.is_set():
                    break
                if info == "PEER_INVALID":
                    await client.send_message(
                        owner_id,
                        "❌ **Channel/group nahi mila.**\n"
                        "Tumhara logged-in account is channel ka **member nahi hai**, "
                        "ya link galat hai.\n"
                        "• Usi account se channel **join** karo jisse `/login` kiya tha\n"
                        "• Link bilkul usi channel ka hona chahiye\n"
                        "Phir dobara try karo.",
                    )
                    cancel_event.set()
                    break
                if info == "BREAK":
                    await client.send_message(
                        owner_id,
                        "❌ **Error:** Userbot us private channel ka member nahi hai.",
                    )
                    cancel_event.set()
                    break
                if info == "OFFLINE" or info is None:
                    async with stats.lock:
                        stats.done += 1
                        stats.fail += 1
                    continue
                ok_one = await upload_one(client, owner_id, status_msg, info, stats)
                async with stats.lock:
                    stats.done += 1
                    if ok_one:
                        stats.ok += 1
                    else:
                        stats.fail += 1
    finally:
        ACTIVE_BATCHES.pop(owner_id, None)

    async with stats.lock:
        ok, fail, done = stats.ok, stats.fail, stats.done
    if cancel_event.is_set() and (done < total):
        await safe_edit(status_msg, "🛑 **Batch stopped.**")
    else:
        await safe_edit(
            status_msg,
            f"✅ **Batch complete!**\n✔️ Success: `{ok}` | ❌ Failed: `{fail}`",
        )


# --- BOT HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    uid = message.chat.id
    print(f"⚡ Processing /start for user {uid}", flush=True)
    has_session = uid in USER_CLIENTS or str(uid) in _load_json(SESSIONS_FILE, {})
    user_status = "🟢 Logged in" if has_session else "🔴 Not logged in (`/login` karo)"
    access = "✅ Allowed" if _is_allowed(uid) else "⛔ Not allowed"

    start_lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮",
        "┃ 🦅 **VILLAN RESTRICTION BOT** 🦅",
        "┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫",
        "┃ ⚡ **Welcome!**",
        "┃ ",
        "┃ Main private channels se restricted",
        "┃ content instantly save karne ke liye ready hoon.",
        "┃ ",
        f"┃ 🔑 **Login:** `{user_status}`",
        f"┃ 🛡️ **Access:** `{access}`",
        f"┃ 🚀 **Parallel Download:** `{PARALLEL_TASKS}x`",
        "┃ 📑 **Delivery:** `Order me`",
        "┃ ",
        "┃ 📌 **Commands:**",
        "┃ • `/login`  - Apne number se login",
        "┃ • `/logout` - Logout",
        "┃ • `/plan`   - Apna plan dekho",
        "┃ • `/batch`  - Range extraction",
        "┃ • `/cancel` - Active queue stop",
        "┃ • Direct link paste karein",
    ]
    if _is_owner(uid):
        start_lines += [
            "┃ ",
            "┃ 👑 **Owner:** `/allow` `/paid` `/unallow` `/users`",
        ]
    start_lines += [
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
    if uid in LOGIN_STATES:
        await _cleanup_login(uid)
        hit = True
    ev = ACTIVE_BATCHES.get(uid)
    if ev is not None:
        ev.set()
        hit = True
    if hit:
        await message.reply_text("🛑 **VILLAN Engine:** Task cancel kar diya gaya hai.")
    else:
        await message.reply_text("Kuch bhi active nahi hai cancel karne ke liye.")


# --- OWNER ADMIN COMMANDS ---
@app.on_message(filters.command("allow") & filters.private)
async def allow_handler(client, message):
    uid = message.chat.id
    if not _is_owner(uid):
        await message.reply_text("⛔ Sirf owner ye command chala sakta hai.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply_text(
            "Usage: `/allow <user_id>`\n(User ID @userinfobot se milegi)"
        )
        return
    target = int(parts[1])
    allowed = _load_allowed()
    allowed.add(target)
    _save_allowed(allowed)
    await message.reply_text(
        f"✅ `{target}` ko permission mil gayi.\nAb wo `/login` karke bot use kar sakta hai."
    )


@app.on_message(filters.command("unallow") & filters.private)
async def unallow_handler(client, message):
    uid = message.chat.id
    if not _is_owner(uid):
        await message.reply_text("⛔ Sirf owner ye command chala sakta hai.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/unallow <user_id>`")
        return
    target = int(parts[1])
    allowed = _load_allowed()
    allowed.discard(target)
    _save_allowed(allowed)
    subs = _load_subs()
    if str(target) in subs:
        del subs[str(target)]
        _save_subs(subs)
    ev = ACTIVE_BATCHES.get(target)
    if ev is not None:
        ev.set()
    await _logout_user(target)
    await message.reply_text(f"🚫 `{target}` ki permission hata di gayi.")


@app.on_message(filters.command("users") & filters.private)
async def users_handler(client, message):
    uid = message.chat.id
    if not _is_owner(uid):
        await message.reply_text("⛔ Sirf owner ye command chala sakta hai.")
        return
    free = sorted(u for u in _get_allowed() if u != OWNER_ID)
    free_lines = "\n".join(f"• `{x}`" for x in free) or "—"
    subs = _load_subs()
    now = time.time()
    paid_lines = []
    for k in sorted((kk for kk in subs if kk.isdigit()), key=int):
        rem_days = (subs[k].get("expiry", 0) - now) / 86400
        if rem_days > 0:
            paid_lines.append(f"• `{k}` — `{rem_days:.1f}` din bache")
    paid_text = "\n".join(paid_lines) or "—"
    await message.reply_text(
        f"👥 **Free users:**\n{free_lines}\n\n💎 **Paid users:**\n{paid_text}"
    )


@app.on_message(filters.command("paid") & filters.private)
async def paid_handler(client, message):
    """Owner kisi ko time-based paid access de: /paid <user_id> <days>"""
    uid = message.chat.id
    if not _is_owner(uid):
        await message.reply_text("⛔ Sirf owner ye command chala sakta hai.")
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.reply_text(
            "Usage: `/paid <user_id> <days>`\nJaise: `/paid 123456789 30`"
        )
        return
    target, days = int(parts[1]), int(parts[2])
    if days <= 0 or days > 3650:
        await message.reply_text("❌ Days 1 se 3650 ke beech hone chahiye.")
        return
    subs = _load_subs()
    now = time.time()
    cur = subs.get(str(target), {}).get("expiry", 0)
    subs[str(target)] = {"type": "paid", "expiry": max(now, cur) + days * 86400}
    _save_subs(subs)
    total_days = (subs[str(target)]["expiry"] - now) / 86400
    await message.reply_text(
        f"✅ `{target}` ko paid access mil gaya.\n"
        f"💎 Total: `{total_days:.1f}` din (expiry tak valid)."
    )
    try:
        await client.send_message(
            target,
            f"🎉 Tumhe **{days} din** ka premium access mil gaya hai!\n"
            "`/login` karke shuru karo.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
    except Exception:
        pass


@app.on_message(filters.command("plan") & filters.private)
async def plan_handler(client, message):
    """User apna plan aur bacha hua time dekhe."""
    uid = message.chat.id
    if not _is_allowed(uid):
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    if _is_owner(uid):
        plan = "👑 Owner (lifetime)"
    elif uid in _get_allowed():
        plan = "✅ Free (lifetime)"
    else:
        days = _sub_remaining(uid) / 86400
        plan = f"💎 Paid — `{days:.1f}` din bache"
    has_session = uid in USER_CLIENTS or str(uid) in _load_json(SESSIONS_FILE, {})
    login = "🟢 Logged in" if has_session else "🔴 Not logged in (`/login` karo)"
    await message.reply_text(
        f"📋 **Tumhara plan:** {plan}\n🔑 **Login:** {login}",
        parse_mode=enums.ParseMode.MARKDOWN,
    )


@app.on_message(filters.command("logout") & filters.private)
async def logout_handler(client, message):
    uid = message.chat.id
    await _cleanup_login(uid)
    await _logout_user(uid)
    await message.reply_text(
        "👋 Logout ho gaya. Dobara use karne ke liye `/login` karo."
    )


@app.on_message(filters.command("batch") & filters.private)
async def batch_start_handler(client, message):
    if not _is_allowed(message.chat.id):
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    print(f"⚡ Processing /batch for user {message.chat.id}", flush=True)
    USER_STATES[message.chat.id] = {"step": "WAITING_FIRST_LINK"}
    await message.reply_text(
        "🔗 **VILLAN Batch System - Step 1/2**\n\n"
        "Kripya **start message ka link** bhejiye jahan se files extract shuru karni hain.\n\n"
        "*(Cancel karne ke liye `/cancel` likhein)*"
    )


# --- LOGIN FLOW (har user apne number se login karega) ---
async def _cleanup_login(uid):
    st = LOGIN_STATES.pop(uid, None)
    if st and st.get("tmp"):
        try:
            await st["tmp"].disconnect()
        except Exception:
            pass


@app.on_message(filters.command("login") & filters.private)
async def login_start_handler(client, message):
    uid = message.chat.id
    if not _is_allowed(uid):
        await message.reply_text(
            "⛔ Tumhe is bot ki permission nahi hai. Owner se contact karo."
        )
        return
    await _cleanup_login(uid)
    LOGIN_STATES[uid] = {"step": "PHONE"}
    await message.reply_text(
        "📱 **Login - Step 1/3**\n\nApna phone number bhejo country code ke saath\n"
        "(jaise `+919876543210`)\n\nRokne ke liye `/cancel` likho.",
        parse_mode=enums.ParseMode.MARKDOWN,
    )


async def _finish_login(client, message, st):
    uid = message.chat.id
    tmp = st["tmp"]
    try:
        me = await tmp.get_me()
        sess = await tmp.export_session_string()
    except Exception as e:
        await message.reply_text(f"❌ Session banane me error: `{e}`")
        await _cleanup_login(uid)
        return
    # Purana session ho to hatao, naya save karo, aur isi connected
    # client ko is user ka persistent userbot banao (disconnect nahi!)
    old = USER_CLIENTS.get(uid)
    if old is not None and old is not tmp:
        try:
            await old.disconnect()
        except Exception:
            pass
    sessions = _load_json(SESSIONS_FILE, {})
    sessions[str(uid)] = sess
    _save_json(SESSIONS_FILE, sessions)
    USER_CLIENTS[uid] = tmp
    LOGIN_STATES.pop(uid, None)

    name = f"@{me.username}" if me.username else me.first_name
    await message.reply_text(
        f"✅ **Login successful!** ({name})\n\n"
        "Tumhara account jud gaya hai. Ab `/batch` bhejo ya channel ka link bhejo, "
        "files tumhare account se niklegi.\n\n"
        "⚠️ OTP/password wale messages yahan se delete kar dena.\n"
        "Logout ke liye `/logout` likho.",
        parse_mode=enums.ParseMode.MARKDOWN,
    )
    try:
        await message.delete()
    except Exception:
        pass


async def handle_login_step(client, message):
    uid = message.chat.id
    st = LOGIN_STATES.get(uid)
    if not st:
        return
    text = message.text.strip()

    if st["step"] == "PHONE":
        phone = re.sub(r"[\s\-()]", "", text)
        if not re.fullmatch(r"\+\d{7,15}", phone):
            await message.reply_text(
                "❌ Number sahi format me bhejo, jaise `+919876543210`"
            )
            return
        tmp = Client(
            f"login_{uid}", api_id=API_ID, api_hash=API_HASH, in_memory=True
        )
        try:
            await tmp.connect()
            sent = await tmp.send_code(phone)
        except FloodWait as e:
            try:
                await tmp.disconnect()
            except Exception:
                pass
            await message.reply_text(
                f"⏳ Telegram ne rok diya hai. `{e.value}` sec baad `/login` dobara try karo."
            )
            await _cleanup_login(uid)
            return
        except (PhoneNumberInvalid, PhoneNumberBanned) as e:
            try:
                await tmp.disconnect()
            except Exception:
                pass
            await message.reply_text(f"❌ Number me problem hai: `{e}`")
            await _cleanup_login(uid)
            return
        except Exception as e:
            try:
                await tmp.disconnect()
            except Exception:
                pass
            await message.reply_text(f"❌ Code bhejne me error: `{e}`")
            await _cleanup_login(uid)
            return
        st.update({
            "step": "OTP",
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "tmp": tmp,
        })
        await message.reply_text(
            "📩 **Login - Step 2/3**\n\nTelegram app me aaya hua **OTP** bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        return

    if st["step"] == "OTP":
        otp = re.sub(r"\D", "", text)
        if len(otp) < 4:
            await message.reply_text("❌ Sahi OTP bhejo.")
            return
        tmp = st["tmp"]
        try:
            await tmp.sign_in(st["phone"], st["phone_code_hash"], otp)
        except PhoneCodeInvalid:
            await message.reply_text("❌ OTP galat hai, dobara bhejo.")
            return
        except PhoneCodeExpired:
            await message.reply_text(
                "❌ OTP expire ho gaya. `/login` se dobara shuru karo."
            )
            await _cleanup_login(uid)
            return
        except SessionPasswordNeeded:
            st["step"] = "PASSWORD"
            await message.reply_text(
                "🔐 **Login - Step 3/3**\n\nIs account par 2-step verification laga hai. "
                "Apna **password** bhejo.\n\n⚠️ Ye message baad me delete kar dena.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return
        except FloodWait as e:
            await message.reply_text(
                f"⏳ `{e.value}` sec wait karke OTP dobara bhejo."
            )
            return
        except Exception as e:
            await message.reply_text(f"❌ Login fail: `{e}`")
            await _cleanup_login(uid)
            return
        await _finish_login(client, message, st)
        return

    if st["step"] == "PASSWORD":
        tmp = st["tmp"]
        try:
            await tmp.check_password(text)
        except PasswordHashInvalid:
            await message.reply_text("❌ Password galat hai, dobara bhejo.")
            return
        except Exception as e:
            await message.reply_text(f"❌ Login fail: `{e}`")
            await _cleanup_login(uid)
            return
        await _finish_login(client, message, st)
        return


@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(
        ["start", "batch", "cancel", "login", "logout",
         "allow", "unallow", "users", "paid", "plan"]
    )
)
async def message_dispatcher(client, message):
    user_id = message.chat.id
    text = message.text.strip()

    # Login flow chal raha ho to wahi handle karega
    if user_id in LOGIN_STATES:
        if not _is_allowed(user_id):
            await _cleanup_login(user_id)
            await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
            return
        await handle_login_step(client, message)
        return

    # Permission gate: sirf allowed users
    if not _is_allowed(user_id):
        await message.reply_text(
            "⛔ Tumhe is bot ko use karne ki permission nahi hai.\nOwner se contact karo."
        )
        return

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


# --- MAIN ENGINE ---
async def main():
    await start_web_server()

    print("🚀 Starting Bot Client...", flush=True)
    await app.start()

    me = await app.get_me()
    print("========================================", flush=True)
    print(f"👉 BOT LISTENING AS: @{me.username}", flush=True)
    print(f"👉 PARALLEL DOWNLOADS: {PARALLEL_TASKS}x (upload order me)", flush=True)
    print(f"👉 OWNER_ID: {OWNER_ID if OWNER_ID else 'not set (open mode)'}", flush=True)
    print("========================================", flush=True)

    await idle()
    await app.stop()
    for c in list(USER_CLIENTS.values()):
        try:
            await c.stop()
        except Exception:
            pass
    if user is not None:
        try:
            await user.stop()
        except Exception:
            pass


if __name__ == "__main__":
    # NOTE: asyncio.run() NAYA loop banata hai, jabki Client ne upar wala
    # loop capture kar rakha hai. app.run() current (wahi) loop use karta
    # hai, tabhi handlers aur updates sahi chalenge.
    app.run(main())
