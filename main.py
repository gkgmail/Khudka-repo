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
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
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

# --- PYROGRAM PATCH: naye bade channel/group IDs ---
# Telegram ab 2^31 se bade channel/group IDs de raha hai (jaise 3329634673).
# Pyrogram 2.0.106 ke utils me purana MIN_CHANNEL_ID (-1002147483647) inhe
# "Peer id invalid" (ValueError) bolke reject kar deta hai. Isliye range fix:
import pyrogram.utils as _pyro_utils
_pyro_utils.MIN_CHANNEL_ID = -100999999999999

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

# Code version - startup logs me dikhega taaki pata chale kaunsa code deploy hai
VERSION = "v38"

# --- CONFIGURATION (Koyeb Environment Variables) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", 8080))

# (Sequential mode me istemal nahi hota; rakha hai taaki purana env var tootey nahi)
PARALLEL_TASKS = max(1, int(os.environ.get("PARALLEL_TASKS", "3")))
# Progress card kitne second me update ho
PROGRESS_EVERY = float(os.environ.get("PROGRESS_EVERY", "5"))
# /login command sirf is Telegram user ID ke liye (0 = sab ke liye khula).
# Apni ID @userinfobot se le sakte ho. Bot public hai to set karna behtar hai.
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
# Owner ka username (contact/buy ke liye dikhega)
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "@THE_OG_VILLAN").strip()
# Per-user sessions, allowlist aur paid subscriptions ki files
# DATA_DIR par rakho taaki Koyeb Volume lage to redeploy par na udein.
# (Volume: Koyeb dashboard -> Volumes -> Create, mount path /data,
#  phir env var DATA_DIR=/data set karo. Bina volume ke "." = ephemeral.)
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", os.path.join(DATA_DIR, "sessions.json"))
ALLOWED_FILE = os.environ.get("ALLOWED_FILE", os.path.join(DATA_DIR, "allowed_users.json"))
SUBS_FILE = os.environ.get("SUBS_FILE", os.path.join(DATA_DIR, "subscriptions.json"))
# Log group: har user ke naam ka folder (forum topic) yahi banega,
# aur nikali hui files yahan bhi jayengi (personal ke saath-saath)
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "-1004328020590") or 0) or None
# v35: Force-join updates channel (@username ya -100... ID). Khali = force join OFF.
UPDATES_CHANNEL = os.environ.get("UPDATES_CHANNEL", "").strip()
# v35: /start welcome photo ka URL ya Telegram file_id. Khali = bina photo ke welcome.
START_IMAGE = os.environ.get("START_IMAGE", "").strip()
TOPICS_FILE = os.environ.get("TOPICS_FILE", os.path.join(DATA_DIR, "user_topics.json"))
# Ek batch me max kitne posts (default 1000; Koyeb env me BATCH_LIMIT se badlo)
BATCH_LIMIT = max(1, int(os.environ.get("BATCH_LIMIT", "1000")))

# --- BACKUP: redeploy par data bachao (FREE tareeka, Koyeb Eco par bhi) ---
# Koyeb Volume sirf paid Standard instance par lagta hai. Agar tum free Eco
# par ho to ye karo: ek PRIVATE channel banao, bot ko usme admin banao,
# uska ID Koyeb env me BACKUP_CHAT_ID me daalo. Har JSON save ke baad bot
# uski copy channel me bhej dega; redeploy ke baad jo file local nahi
# milegi, wo channel se wapas aa jayegi. DATA_DIR/volume wala tareeka bhi
# saath me chalta rahega — dono me se jo mile, wahi use hoga.
# NOTE: sessions.json me users ke login session strings hote hain, isliye
# backup channel HAMESHA private rakho — sirf tum (owner) usme raho.
try:
    _raw_backup = os.environ.get("BACKUP_CHAT_ID", "").strip()
    BACKUP_CHAT_ID = int(_raw_backup) if _raw_backup else None
except ValueError:
    BACKUP_CHAT_ID = None
_BACKUP_KNOWN = ("sessions.json", "allowed_users.json", "subscriptions.json",
                  "user_settings.json", "user_topics.json")
_BACKUP_MSG_IDS = {}  # filename -> aakhri backup message id (delete-and-replace)


def _backup_caption(fname):
    return f"backup:{fname}"


async def _backup_upload(fname, path):
    """Local save ke baad backup channel me copy bhejo (best-effort)."""
    if not BACKUP_CHAT_ID:
        return
    try:
        if not os.path.exists(path):
            return
        msg = await app.send_document(BACKUP_CHAT_ID, path,
                                      caption=_backup_caption(fname))
        old = _BACKUP_MSG_IDS.get(fname)
        _BACKUP_MSG_IDS[fname] = msg.id
        if old and old != msg.id:
            try:
                await app.delete_messages(BACKUP_CHAT_ID, old)
            except Exception:
                pass
    except Exception as e:
        print(f"backup upload {fname} failed: {e}", flush=True)


async def _backup_restore_all():
    """Startup par: jo JSON store local nahi hai, use backup channel se lao.
    Bot MTProto se channel history NAHI padh sakta (messages.GetHistory par
    BOT_METHOD_INVALID aata hai — v36 ke live logs me confirm hua), isliye
    history scan OWNER ke userbot se hota hai. Upload bot se hi hota hai
    (channel me post karna bot ke liye allowed hai)."""
    if not BACKUP_CHAT_ID:
        return
    global _OWNER_UB_STARTED
    if user is None:
        print("backup restore SKIP: SESSION_STRING wala owner userbot nahi "
              "mila — bot akele history nahi padh sakta.", flush=True)
        return
    # Deploy overlap me purana instance kuch der zinda rehta hai — usi
    # beech userbot start karo to 406 AUTH_KEY_DUPLICATED aata hai.
    # Isliye 406 par ruko aur dobara try karo (purana instance marne do).
    for _att in range(4):
        try:
            if not _OWNER_UB_STARTED:
                await user.start()
                _OWNER_UB_STARTED = True
            break
        except Exception as e:
            es = str(e)
            if "already" in es.lower():
                _OWNER_UB_STARTED = True
                break
            if "AUTH_KEY_DUPLICATED" in es and _att < 3:
                print(f"backup restore: 406 duplicate (attempt {_att + 1}/4)"
                      " — 20s ruk ke dobara try...", flush=True)
                await asyncio.sleep(20)
                continue
            print(f"backup restore: userbot start fail: {e}", flush=True)
            return
    stores = {
        "sessions.json": SESSIONS_FILE,
        "allowed_users.json": ALLOWED_FILE,
        "subscriptions.json": SUBS_FILE,
        "user_settings.json": SETTINGS_FILE,
        "user_topics.json": TOPICS_FILE,
    }
    for fname, path in stores.items():
        try:
            if os.path.exists(path):
                continue
            cap = _backup_caption(fname)
            latest = None
            async for m in user.get_chat_history(BACKUP_CHAT_ID, limit=100):
                if m.document and (m.caption or "") == cap:
                    latest = m  # history newest-first: pehla match = latest
                    break
            if latest is None:
                continue
            await user.download_media(latest, file_name=path)
            _BACKUP_MSG_IDS[fname] = latest.id
            print(f"backup restore: {fname} OK", flush=True)
        except Exception as e:
            print(f"backup restore {fname} failed: {e}", flush=True)

# Pyrogram Clients Setup
app = Client(
    "bot_service",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
)

user = None
if SESSION_STRING and len(SESSION_STRING) > 30:
    user = Client(
        "user_service",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        workers=100,
    )

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
        return
    # Backup channel set hai to copy wahan bhi bhejo (best-effort, non-blocking)
    fname = os.path.basename(path)
    if BACKUP_CHAT_ID and fname in _BACKUP_KNOWN:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_backup_upload(fname, path))
        except Exception:
            pass


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
            workers=100,
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


def _bold_text(text):
    """Extracted caption/text ko BOLD me convert karo (HTML mode).
    HTML me sirf <, >, & escape karne padte hain — koi visible
    backslash nahi dikhega. Content same rahega, sirf bold dikhega."""
    if not text:
        return text
    # HTML special chars escape karo
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return f"<b>{text}</b>"


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
        self._last_status_update = 0


async def _update_batch_status(status_msg, stats, force=False):
    """Pinned batch status message ko live update karo (har 3 sec me max 1 baar)."""
    now = time.time()
    async with stats.lock:
        if not force and (now - stats._last_status_update) < 3:
            return
        stats._last_status_update = now
        done, ok, fail, total = stats.done, stats.ok, stats.fail, stats.total
        elapsed = max(now - stats.start, 1)
    pct = (done * 100 / total) if total > 0 else 0
    filled = int(pct // 10)
    bar = ("🟩" * filled + "⬜" * (10 - filled))
    # ETA
    remaining = total - done
    per_file = elapsed / max(done, 1)
    eta_s = int(remaining * per_file)
    eta_str = time_formatter(eta_s * 1000) if done > 0 else "—"
    text = (
        "📦 **BATCH STATUS**\n"
        f"{bar} `{pct:.0f}%`\n"
        "━━━━━━━━━━━━━━━\n"
        f"📁 Total: `{total}`\n"
        f"✅ Done: `{ok}`  ❌ Failed: `{fail}`\n"
        f"⏱️ ETA: `{eta_str}`\n"
        "⏹ Cancel: `/cancel`"
    )
    await safe_edit(status_msg, text)


# --- LIVE PROGRESS TRACKER (file + batch dashboard) ---
async def _premium_card(uid, bot_client=None):
    """⚜️ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ card — plan/premium check ke liye."""
    try:
        _u = await bot_client.get_users(uid) if bot_client else None
        _name = (_u.first_name if _u and _u.first_name else "User")
    except Exception:
        _name = "User"
    if _is_owner(uid):
        _tl, _ed, _et = "Lifetime 👑", "—", "—"
    elif uid in _get_allowed():
        _tl, _ed, _et = "Lifetime ✅", "—", "—"
    else:
        _rem = _sub_remaining(uid)
        if _rem > 0:
            _d, _h = int(_rem // 86400), int((_rem % 86400) // 3600)
            _m = int((_rem % 3600) // 60)
            _tl = f"{_d} ᴅᴀʏꜱ, {_h} ʜᴏᴜʀꜱ, {_m} ᴍɪɴᴜᴛᴇꜱ"
            _exp = time.time() + _rem
            _ed = time.strftime("%d-%m-%Y", time.localtime(_exp))
            _et = time.strftime("%I:%M:%S %p", time.localtime(_exp))
        else:
            _tl, _ed, _et = "Expired ❌", "—", "—"
    return "\n".join([
        "⚜️ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ᴅᴀᴛᴀ :",
        "",
        f"👤 ᴜꜱᴇʀ : {_name}",
        f"⚡ ᴜꜱᴇʀ ɪᴅ : {uid}",
        f"⏰ ᴛɪᴍᴇ ʟᴇꜰᴛ : {_tl}",
        f"⌛️ ᴇxᴘɪʀʏ ᴅᴀᴛᴇ : {_ed}",
        f"⏱️ ᴇxᴘɪʀʏ ᴛɪᴍᴇ : {_et}",
    ])


async def progress_bar(
    current, total, message, start_time, last_update, action_title,
    stats=None, uid=None, bot_client=None
):
    """Premium style progress bar (screenshot wala design)."""
    now = time.time()
    if (now - last_update[0]) < PROGRESS_EVERY and current != total:
        return
    last_update[0] = now

    elapsed_time = max(now - start_time, 0.001)
    speed = current / elapsed_time
    percentage = (current * 100 / total) if total > 0 else 0

    # 🟥 Squares wala progress bar (10 blocks)
    filled = int(percentage // 10)
    sq_bar = ("🟥 " * filled + "⬜ " * (10 - filled)).strip()

    remaining_bytes = max(total - current, 0)
    time_left = round(remaining_bytes / speed) if speed > 0 else 0

    # Message ID nikalo (action_title me "ID `123`" format me hai)
    _mid = re.search(r"ID `(\d+)`", action_title or "")
    mid_str = _mid.group(1) if _mid else "—"

    # UPLOADING ya DOWNLOADING
    op = "UPLOADING" if "Upload" in (action_title or "") else "DOWNLOADING"

    # ━━━ SHORT PROGRESS BAR (sirf download/upload ke liye) ━━━
    # Batch line aur premium card YAHAN nahi — wo alag pinned status me hai
    lines = [
        op,
        "",
        sq_bar,
        "╭━━━━❰ᴘʀᴏɢʀᴇss ʙᴀʀ❱━➣",
        f"┣⪼ 🗂️ : {humanbytes(current)} | {humanbytes(total)}",
        f"┣⪼ ⏳️ : {percentage:.1f}%",
        f"┣⪼ 🚀 : {humanbytes(speed)}/s",
        f"┣⪼ ⏱️ : {time_formatter(time_left * 1000)}",
        f"┣⪼Message ID: {mid_str}",
        "╰━━━━━━━━━━━━━━━➣",
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
    """get_messages karo. Peer resolve na ho ('Peer id invalid') to dialogs
    sync karke peer cache warm karo aur ek baar retry karo."""
    try:
        return await user_client.get_messages(src_chat, msg_id)
    except PeerIdInvalid:
        pass
    except ValueError as e:
        # Purana Pyrogram naye bade channel IDs par ValueError deta hai
        if "Peer id invalid" not in str(e):
            raise
    try:
        await user_client.get_dialogs(limit=500)
    except Exception as e:
        print(f"dialog warmup failed: {e}", flush=True)
    return await user_client.get_messages(src_chat, msg_id)


async def fetch_one(user_client, src_chat, msg_id):
    """Message ka metadata + file_id lao. Download MAT karo (fast path ke liye).
    Returns: info dict | None (skip) | 'BREAK' (fatal) | 'OFFLINE' | 'PEER_INVALID'."""
    if not user_client:
        return "OFFLINE"

    # Retry transient failures (max 3 attempts)
    last_err = None
    for attempt in range(3):
        try:
            target_msg = await _fetch_with_warmup(user_client, src_chat, msg_id)
            if not target_msg or target_msg.empty:
                return None
            break  # success, exit retry loop
        except FloodWait as e:
            wait = e.value + 1
            print(f"FloodWait on {msg_id}: waiting {wait}s (attempt {attempt+1}/3)", flush=True)
            await asyncio.sleep(wait)
            last_err = e
            continue
        except (TimeoutError, asyncio.TimeoutError, ConnectionError) as e:
            last_err = e
            print(f"Transient error on {msg_id} (attempt {attempt+1}/3): {e}", flush=True)
            await asyncio.sleep(2 * (attempt + 1))
            continue
        except PeerIdInvalid:
            return "PEER_INVALID"
        except ValueError as e:
            if "Peer id invalid" in str(e):
                return "PEER_INVALID"
            raise
        except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
            return "BREAK"
        except Exception as e:
            last_err = e
            print(f"Fetch error on {msg_id} (attempt {attempt+1}/3): {e}", flush=True)
            await asyncio.sleep(1)
            continue
    else:
        # All 3 attempts failed
        print(f"Fetch failed after 3 attempts on {msg_id}: {last_err}", flush=True)
        return None

    # Metadata nikalo (fetch already successful)
    if not target_msg.media:
        if target_msg.text:
            return {"kind": "text", "text": target_msg.text, "msg_id": msg_id}
        return None

    kind = target_msg.media.value
    media_obj = getattr(target_msg, kind, None)
    file_id = getattr(media_obj, "file_id", None) if media_obj else None
    file_size = getattr(media_obj, "file_size", 0) or 0
    if file_size > 2097152000:
        return {"kind": "too_big", "msg_id": msg_id}

    fname = getattr(media_obj, "file_name", "") or ""
    if not fname:
        # Photo/voice/sticker jaisi media me file_name hota hi nahi.
        # Bina extension ke download hui photo ko upload karne par
        # Telegram PHOTO_EXT_INVALID deta hai (v35 ke live logs me
        # yahi photo-fail ki asli wajah nikli) — sahi extension lagao.
        _ext = {"photo": ".jpg", "voice": ".ogg", "video_note": ".mp4",
                "sticker": ".webp", "animation": ".mp4"}.get(kind, "")
        fname = f"media_{msg_id}{_ext}"
    info = {
        "kind": kind,
        "msg_id": msg_id,
        "file_id": file_id,
        "file_size": file_size,
        "file_name": os.path.basename(fname),
        "caption": target_msg.caption or "",
        "target_msg": target_msg,  # slow path download ke liye
        "tmpdir": None,
        "file_path": None,
        "thumb_path": None,
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


async def download_file(user_client, info, prog_msg, stats=None, uid=None, bot_client=None):
    """Slow path: file ko disk par download karo (settings ya file_id fail par).
    prog_msg = alag progress message (pinned status_msg se alag)."""
    target_msg = info.get("target_msg")
    msg_id = info["msg_id"]
    if not target_msg or not user_client:
        return None
    tmpdir = tempfile.mkdtemp(prefix=f"vln_{msg_id}_")
    info["tmpdir"] = tmpdir
    try:
        dl_path = os.path.join(tmpdir, info["file_name"])
        last_update = [time.time()]
        start_time = time.time()
        file_path = await user_client.download_media(
            target_msg,
            file_name=dl_path,
            progress=progress_bar,
            progress_args=(
                prog_msg, start_time, last_update,
                f"📥 Downloading • ID `{msg_id}`", stats, uid, bot_client,
            ),
        )
        if not file_path or not os.path.exists(file_path):
            return None
        if info.get("kind") == "photo" and not file_path.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp")):
            # Photo bina sahi extension ke upload nahi hoti (PHOTO_EXT_INVALID)
            try:
                os.rename(file_path, file_path + ".jpg")
                file_path = file_path + ".jpg"
            except Exception:
                pass
        info["file_path"] = file_path
        if target_msg.video and target_msg.video.thumbs:
            try:
                info["thumb_path"] = await user_client.download_media(
                    target_msg.video.thumbs[0].file_id,
                    file_name=os.path.join(tmpdir, "thumb.jpg"),
                )
            except Exception:
                info["thumb_path"] = None
        return file_path
    except Exception as e:
        print(f"Download error on {msg_id}: {e}", flush=True)
        return None


def _needs_file(s, info):
    """Kya is file ko disk par download karna zaroori hai (settings ke liye)?"""
    if s["rename_tag"] or s["remove_words"] or s["replace_words"]:
        return True
    if s["thumb_file_id"]:
        return True
    kind = info["kind"]
    if kind == "video" and (s["video_wm"] or s["upload_as_doc"]):
        return True
    if kind == "document" and s["pdf_wm"]:
        if info.get("file_name", "").lower().endswith(".pdf"):
            return True
    return False


async def _send_by_file_id(client, dest, kind, file_id, caption, info, topic_id=None):
    """file_id se bhejo — INSTANT, server-side (no download/upload)."""
    kw = {"reply_to_message_id": topic_id} if topic_id else {}
    # Caption _bold_text() se HTML (<b>...</b>) me aata hai — bina parse_mode
    # ke Telegram tags ko literal text dikhayega, isliye HTML mode zaroori hai.
    cap_kw = {"parse_mode": enums.ParseMode.HTML} if caption else {}
    if kind == "video":
        return await client.send_video(
            dest, video=file_id, caption=caption,
            duration=info.get("duration"),
            width=info.get("width"), height=info.get("height"),
            **cap_kw, **kw)
    elif kind == "audio":
        return await client.send_audio(
            dest, audio=file_id, caption=caption,
            duration=info.get("duration"),
            performer=info.get("performer"), title=info.get("title"),
            **cap_kw, **kw)
    elif kind == "photo":
        return await client.send_photo(
            dest, photo=file_id, caption=caption, **cap_kw, **kw)
    elif kind == "voice":
        return await client.send_voice(
            dest, voice=file_id, caption=caption,
            duration=info.get("duration"), **cap_kw, **kw)
    elif kind == "video_note":
        return await client.send_video_note(dest, video_note=file_id, **kw)
    else:
        return await client.send_document(
            dest, document=file_id, caption=caption, **cap_kw, **kw)


def _sent_file_id(sent_msg, kind):
    """Bot ne jo message bheja, uske media ka file_id nikalo.
    Ye file_id BOT ka apna hai — isko dobara use kar sakte hain
    (userbot wala file_id bot use NAHI kar sakta -> MEDIA_EMPTY)."""
    if not sent_msg:
        return None
    attr = {"video": "video", "audio": "audio", "photo": "photo",
            "voice": "voice", "video_note": "video_note"}.get(kind, "document")
    media = getattr(sent_msg, attr, None)
    return getattr(media, "file_id", None) if media else None


# --- GROUP TOPIC (har user ke naam ka folder) ---
# --- GROUP TOPIC: Bot API se bhejo (message_thread_id) ---
# MTProto raw API me top_msg_id kaam nahi kar raha, isliye Bot API ka
# message_thread_id use karenge — ye forum topics ke liye GUARANTEED kaam karta hai.
import aiohttp as _aiohttp

async def _botapi_send_to_topic(kind, file_id=None, file_path=None,
                                caption="", topic_id=None, info=None):
    """Bot API HTTP se topic me bhejo. file_id ho to instant, warna upload.
    Returns True/False."""
    if not BOT_TOKEN or not LOG_GROUP_ID or not topic_id:
        return False
    info = info or {}
    try:
        method_map = {
            "video": "sendVideo", "audio": "sendAudio", "photo": "sendPhoto",
            "voice": "sendVoice", "video_note": "sendVideoNote",
            "document": "sendDocument", "text": "sendMessage",
        }
        method = method_map.get(kind, "sendDocument")
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
        data = {
            "chat_id": str(LOG_GROUP_ID),
            "message_thread_id": str(topic_id),
            "caption": caption or "",
        }
        # Caption _bold_text() se HTML (<b>) me hai — bina parse_mode ke
        # Bot API tags ko literal dikhayega, isliye HTML mode do.
        if caption:
            data["parse_mode"] = "HTML"
        files = None
        if kind == "text":
            data = {
                "chat_id": str(LOG_GROUP_ID),
                "message_thread_id": str(topic_id),
                "text": caption or "",
            }
            if caption:
                data["parse_mode"] = "HTML"
        elif file_id:
            # file_id se instant bhejo
            param = {"video": "video", "audio": "audio", "photo": "photo",
                     "voice": "voice", "video_note": "video_note"}.get(kind, "document")
            data[param] = file_id
            if kind == "video":
                if info.get("duration"):
                    data["duration"] = str(info["duration"])
                if info.get("width"):
                    data["width"] = str(info["width"])
                if info.get("height"):
                    data["height"] = str(info["height"])
            elif kind in ("audio", "voice"):
                if info.get("duration"):
                    data["duration"] = str(info["duration"])
        elif file_path and os.path.exists(file_path):
            # file upload karo (50MB tak Bot API limit)
            size = os.path.getsize(file_path)
            if size > 50 * 1024 * 1024:
                print(f"Bot API: file too big ({size}), skipping", flush=True)
                return False
            param = {"video": "video", "audio": "audio", "photo": "photo",
                     "voice": "voice", "video_note": "video_note"}.get(kind, "document")
            files = {param: open(file_path, "rb")}
        else:
            return False
        async with _aiohttp.ClientSession() as sess:
            if files:
                form = _aiohttp.FormData()
                for k, v in data.items():
                    form.add_field(k, v)
                for k, f in files.items():
                    form.add_field(k, f, filename=os.path.basename(file_path))
                    f.close()
                async with sess.post(url, data=form, timeout=300) as resp:
                    result = await resp.json()
            else:
                async with sess.post(url, data=data, timeout=60) as resp:
                    result = await resp.json()
        if result.get("ok"):
            return True
        print(f"Bot API failed: {result}", flush=True)
        return False
    except Exception as e:
        print(f"Bot API send failed: {e}", flush=True)
        return False


# --- GROUP TOPIC: raw API se bhejo (top_msg_id) [FALLBACK] ---
# Pyrogram 2.0.106 ke high-level send_* me top_msg_id nahi hai, isliye
# forum topic me bhejne ke liye raw messages.SendMedia use karna padta hai.
async def _raw_send_to_topic(client, peer, topic_id, kind, file_id=None,
                             file_path=None, caption="", info=None, thumb=None):
    """Topic me media bhejo (raw API, top_msg_id se).
    file_id diya to server-side instant, file_path diya to upload karke.
    Returns True/False."""
    from pyrogram import raw
    from pyrogram.file_id import FileId, FileType
    import random as _rand
    import mimetypes
    info = info or {}
    try:
        if file_id:
            # file_id se InputMedia banao
            fid = FileId.decode(file_id)
            if fid.file_type in (FileType.PHOTO, FileType.PROFILE_PHOTO):
                media = raw.types.InputMediaPhoto(
                    id=raw.types.InputPhoto(
                        id=fid.media_id,
                        access_hash=fid.access_hash,
                        file_reference=fid.file_reference or b""))
            else:
                media = raw.types.InputMediaDocument(
                    id=raw.types.InputDocument(
                        id=fid.media_id,
                        access_hash=fid.access_hash,
                        file_reference=fid.file_reference or b""))
        elif file_path and os.path.exists(file_path):
            # file upload karo
            uploaded = await client.save_file(file_path)
            mime, _ = mimetypes.guess_type(file_path)
            mime = mime or "application/octet-stream"
            fname = os.path.basename(file_path)
            attrs = [raw.types.DocumentAttributeFilename(file_name=fname)]
            if kind == "video":
                attrs.append(raw.types.DocumentAttributeVideo(
                    duration=info.get("duration") or 0,
                    w=info.get("width") or 0,
                    h=info.get("height") or 0,
                    supports_streaming=True))
            elif kind == "audio":
                attrs.append(raw.types.DocumentAttributeAudio(
                    duration=info.get("duration") or 0,
                    performer=info.get("performer") or "",
                    title=info.get("title") or ""))
            elif kind == "voice":
                attrs.append(raw.types.DocumentAttributeAudio(
                    duration=info.get("duration") or 0, voice=True))
            elif kind == "video_note":
                attrs.append(raw.types.DocumentAttributeVideo(
                    duration=info.get("duration") or 0,
                    w=0, h=0, round_message=True))
            media = raw.types.InputMediaUploadedDocument(
                file=uploaded, mime_type=mime, attributes=attrs)
            if thumb and isinstance(thumb, str) and os.path.exists(thumb):
                try:
                    media.thumb = await client.save_file(thumb)
                except Exception:
                    pass
        else:
            return False
        # Caption _bold_text() se HTML (<b>) me hai — raw API ko tags
        # hatake bold entity deni padti hai, warna tags literal dikhenge.
        cap_text = caption or ""
        cap_entities = None
        if (cap_text.startswith("<b>") and cap_text.endswith("</b>")
                and len(cap_text) > 7):
            import html as _html
            cap_text = _html.unescape(cap_text[3:-4])
            cap_entities = [raw.types.MessageEntityBold(
                offset=0, length=len(cap_text))]
        await client.invoke(raw.functions.messages.SendMedia(
            peer=peer,
            media=media,
            message=cap_text,
            entities=cap_entities,
            random_id=_rand.randint(1, 2**63 - 1),
            top_msg_id=topic_id,
        ))
        return True
    except Exception as e:
        print(f"Raw topic send failed: {e}", flush=True)
        return False


async def _find_topic_id(client, channel, title):
    """Group me title se topic dhoondo. Returns topic ID ya None.
    Partial match bhi karta hai (uid search ke liye).
    Do pass: pehle server-side q search, phir bina q ke poori list
    scan karke title me uid match (q kabhi-kabhi miss kar deta hai)."""
    from pyrogram import raw
    for query in (title, ""):
        try:
            res = await client.invoke(raw.functions.channels.GetForumTopics(
                channel=channel, offset_date=0, offset_id=0,
                offset_topic=0, limit=100, q=query))
            for t in res.topics:
                # Exact match ya uid/title ka partial match
                if t.title == title or title in t.title:
                    return t.id
        except Exception as e:
            print(f"Find topic failed: {e}", flush=True)
            return None
    return None


async def _botapi_create_topic(title):
    """Bot API se forum topic banao. Returns topic_id ya None.
    (Bots MTProto se nahi, lekin Bot API se topic bana SAKTE hain!)"""
    if not BOT_TOKEN or not LOG_GROUP_ID:
        return None
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/createForumTopic"
        data = {"chat_id": str(LOG_GROUP_ID), "name": title}
        async with _aiohttp.ClientSession() as sess:
            async with sess.post(url, data=data, timeout=30) as resp:
                result = await resp.json()
        if result.get("ok"):
            tid = result["result"].get("message_thread_id")
            print(f"[TOPIC] Bot API se topic banaya: {tid}", flush=True)
            return tid
        print(f"[TOPIC] createForumTopic failed: {result}", flush=True)
    except Exception as e:
        print(f"[TOPIC] createForumTopic exception: {e}", flush=True)
    return None


async def get_or_create_topic(userbot, uid):
    """LOG_GROUP me user ke naam ka forum topic (folder). Ek baar banega,
    phir wahi reuse hoga.
    NOTE: userbot use hota hai kyunki BOTS channels.GetForumTopics/
    CreateForumTopic use NAHI kar sakte (BOT_METHOD_INVALID)!
    Returns: topic ID. Fail par None."""
    print(f"[TOPIC] Called for uid={uid}, LOG_GROUP_ID={LOG_GROUP_ID}", flush=True)
    if not LOG_GROUP_ID:
        return None
    # STEP 1: Pehle stored topic ID check karo (fast path)
    topics = _load_json(TOPICS_FILE, {})
    stored = topics.get(str(uid))
    if stored:
        print(f"[TOPIC] Stored topic_id mil gaya: {stored}", flush=True)
        return stored
    # STEP 2: Stored nahi hai (redeploy par wipe ho gaya?) —
    # to group me EXISTING topic search karo (uid se), naya mat banao!
    # Title format: "{Name} • {uid}" — uid se search karo.
    # PEHLE owner ke apne userbot (global `user`) se search karo — wahi
    # account log group ka member/admin hai. Per-user userbot group me
    # hota hi nahi, isliye usse search hamesha fail hota tha aur har
    # redeploy ke baad NAYA folder ban jata tha (duplicate folders bug)!
    search_clients = []
    if user is not None:
        search_clients.append(user)
    if userbot is not None and userbot is not user:
        search_clients.append(userbot)
    for sc in search_clients:
        try:
            peer = await sc.resolve_peer(LOG_GROUP_ID)
            found_id = await _find_topic_id(sc, peer, str(uid))
            if found_id:
                print(f"[TOPIC] ✅ EXISTING topic mil gaya: {found_id} for uid={uid}", flush=True)
                topics[str(uid)] = found_id
                _save_json(TOPICS_FILE, topics)
                return found_id
        except Exception as e:
            print(f"[TOPIC] Search failed: {e}", flush=True)
    if search_clients:
        print(f"[TOPIC] Existing topic nahi mila, naya banayenge", flush=True)
    # STEP 3: Naya topic Bot API se banao (bot MTProto se nahi bana sakta!)
    # Title ke liye user ka naam lao (bot client se, userbot se nahi)
    title = f"User • {uid}"
    try:
        # Bot client global 'app' se user info lao
        u = await app.get_users(uid)
        title = f"{(u.first_name or 'User')[:25]} • {uid}"
    except Exception:
        pass
    print(f"[TOPIC] Naya topic bana raha hun: '{title}'", flush=True)
    topic_id = await _botapi_create_topic(title)
    if topic_id:
        topics[str(uid)] = topic_id
        _save_json(TOPICS_FILE, topics)
        print(f"[TOPIC] ✅ SUCCESS: topic={topic_id} for uid={uid}", flush=True)
        return topic_id
    print(f"[TOPIC] ❌ FAILED: topic nahi ban saka", flush=True)
    return None


# --- PHASE 2: UPLOAD (hamesha ORDER me, ek-ek karke) ---
async def upload_one(client, userbot, uid, dest_chat, topic_id, status_msg, info, stats=None, prog_msg=None):
    """Ek file deliver karo: personal (dest_chat) + group topic (LOG_GROUP_ID).
    FAST PATH: file_id se instant bhejo (no download/upload).
    SLOW PATH: download karo, settings lagao, phir upload karo.
    status_msg = pinned batch status (sirf _update_batch_status).
    prog_msg = alag per-file progress message (sirf progress_bar)."""
    kind = info["kind"]
    s = get_settings(uid)
    group_id = LOG_GROUP_ID

    def _clean():
        if info.get("tmpdir"):
            shutil.rmtree(info["tmpdir"], ignore_errors=True)

    async def _to_group_text(text):
        if not group_id:
            return
        try:
            if topic_id:
                peer = await client.resolve_peer(group_id)
                from pyrogram import raw
                import random as _rand
                # Bold entity lagao (text me <b> tags hain, unko hatake entity banao)
                entities = None
                plain = text
                if text.startswith("<b>") and text.endswith("</b>") and len(text) > 7:
                    plain = text[3:-4]
                    # HTML entities decode karo (length sahi ho)
                    import html as _html
                    plain_decoded = _html.unescape(plain)
                    entities = [raw.types.MessageEntityBold(
                        offset=0, length=len(plain_decoded))]
                    plain = plain_decoded
                await client.invoke(raw.functions.messages.SendMessage(
                    peer=peer, message=plain,
                    random_id=_rand.randint(1, 2**63 - 1),
                    top_msg_id=topic_id, entities=entities))
            else:
                await client.send_message(
                    group_id, text, parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            print(f"Group text send failed: {e}", flush=True)

    try:
        # caption (settings template)
        cap = info.get("caption") or ""
        if s["caption"]:
            try:
                cap = s["caption"].format(filename=info.get("file_name", "file"))
            except Exception:
                cap = s["caption"]
        # Extracted caption ko BOLD me convert karo (content same, sirf bold)
        if cap:
            cap = _bold_text(cap)

        if kind == "text":
            bold_text = _bold_text(info["text"])
            await client.send_message(
                dest_chat, bold_text, parse_mode=enums.ParseMode.HTML)
            await _to_group_text(bold_text)
            return True
        if kind == "too_big":
            await client.send_message(
                dest_chat,
                f"⚠️ **Message {info['msg_id']}:** File 2GB se badi hai.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return False

        # === FAST PATH: file_id se INSTANT bhejo ===
        file_id = info.get("file_id")
        if file_id and not _needs_file(s, info):
            try:
                await _send_by_file_id(client, dest_chat, kind, file_id, cap, info)
                if group_id and topic_id:
                    # PEHLE Bot API try karo (message_thread_id guaranteed kaam karta hai)
                    ok = await _botapi_send_to_topic(
                        kind, file_id=file_id, caption=cap,
                        topic_id=topic_id, info=info)
                    if not ok:
                        # Fallback: raw MTProto
                        try:
                            peer = await client.resolve_peer(group_id)
                            ok = await _raw_send_to_topic(
                                client, peer, topic_id, kind,
                                file_id=file_id, caption=cap, info=info)
                            if not ok:
                                print(f"Group topic send failed, skipping", flush=True)
                        except Exception as e:
                            print(f"Group file_id send failed: {e}", flush=True)
                elif group_id:
                    # topic nahi bana to General me bhejo (fallback)
                    try:
                        await _send_by_file_id(
                            client, group_id, kind, file_id, cap, info)
                    except Exception as e:
                        print(f"Group file_id send failed: {e}", flush=True)
                return True
            except Exception as e:
                print(f"Fast path failed ({info['msg_id']}): {e} — downloading...",
                      flush=True)

        # === SLOW PATH: download + settings + upload ===
        fp = await download_file(userbot, info, prog_msg, stats, uid, client)
        if not fp:
            _clean()
            return False

        # settings: filename (remove/replace/tag)
        base = os.path.basename(fp)
        new_base = apply_filename_rules(base, s)
        if new_base != base:
            new_fp = os.path.join(os.path.dirname(fp), new_base)
            try:
                os.rename(fp, new_fp)
                fp = new_fp
                info["file_path"] = fp
            except Exception:
                pass
        if s["caption"]:
            try:
                cap = s["caption"].format(filename=new_base)
            except Exception:
                cap = s["caption"]

        # settings: custom thumbnail
        th = info.get("thumb_path")
        if s["thumb_file_id"]:
            th = s["thumb_file_id"]

        # settings: watermarks
        if kind == "document" and new_base.lower().endswith(".pdf") and s["pdf_wm"]:
            fp = apply_pdf_watermark(fp, s["pdf_wm"], info["tmpdir"])
            info["file_path"] = fp
        if kind == "video" and s["video_wm"]:
            fp = apply_video_watermark(fp, s["video_wm"], info["tmpdir"])
            info["file_path"] = fp

        # settings: upload method
        send_kind = kind
        if kind == "video" and s["upload_as_doc"]:
            send_kind = "document"

        async def _do_send(dchat, dtopic, show_progress):
            kw = {"reply_to_message_id": dtopic} if dtopic else {}
            # Caption bold hai to parse_mode chahiye
            cap_kw = {"parse_mode": enums.ParseMode.HTML} if cap else {}
            if show_progress:
                last_update = [time.time()]
                start_time = time.time()
                pa = (prog_msg, start_time, last_update)
                title = f"🚀 Uploading • ID `{info.get('msg_id')}`"
                prog = {"progress": progress_bar,
                        "progress_args": pa + (title, stats, uid, client)}
            else:
                prog = {}
            if send_kind == "video":
                return await client.send_video(
                    dchat, video=fp, caption=cap, thumb=th,
                    duration=info.get("duration"),
                    width=info.get("width"), height=info.get("height"),
                    **prog, **kw, **cap_kw)
            elif send_kind == "document":
                return await client.send_document(
                    dchat, document=fp, caption=cap, thumb=th,
                    **prog, **kw, **cap_kw)
            elif send_kind == "audio":
                return await client.send_audio(
                    dchat, audio=fp, caption=cap,
                    duration=info.get("duration"),
                    performer=info.get("performer"), title=info.get("title"),
                    **prog, **kw, **cap_kw)
            elif send_kind == "photo":
                try:
                    return await client.send_photo(
                        dchat, photo=fp, caption=cap, **kw, **cap_kw)
                except Exception as e:
                    if "PHOTO_EXT_INVALID" in str(e):
                        # Photo ke roop me reject ho jaye to file/document
                        # bana kar bhej do — image user ko mil hi jayegi
                        return await client.send_document(
                            dchat, document=fp, caption=cap, **kw, **cap_kw)
                    raise
            elif send_kind == "voice":
                return await client.send_voice(
                    dchat, voice=fp, caption=cap,
                    duration=info.get("duration"), **kw, **cap_kw)
            elif send_kind == "video_note":
                return await client.send_video_note(dchat, video_note=fp, **kw)
            else:
                return await client.send_document(
                    dchat, document=fp, caption=cap, **prog, **kw)

        # personal delivery (progress ke saath)
        try:
            sent_msg = await _do_send(dest_chat, None, True)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            _clean()
            return False
        except Exception as e:
            print(f"Personal upload failed ({info.get('msg_id')}): {e}", flush=True)
            _clean()
            return False

        # group topic delivery (personal ke baad)
        # v24: personal upload se mila BOT ka file_id use karo -> group copy INSTANT
        # (userbot wala file_id bot ke kaam ka nahi -> MEDIA_EMPTY, isliye dobara upload nahi)
        if group_id and topic_id:
            ok = False
            bot_fid = _sent_file_id(sent_msg, send_kind)
            if bot_fid:
                ok = await _botapi_send_to_topic(
                    send_kind, file_id=bot_fid, caption=cap,
                    topic_id=topic_id, info=info)
                if ok:
                    print(f"Group copy instant (file_id) for {info.get('msg_id')}", flush=True)
            if not ok:
                ok = await _botapi_send_to_topic(
                    send_kind, file_path=fp, caption=cap,
                    topic_id=topic_id, info=info)
            if not ok:
                try:
                    peer = await client.resolve_peer(group_id)
                    ok = await _raw_send_to_topic(
                        client, peer, topic_id, send_kind,
                        file_path=fp, caption=cap, info=info, thumb=th)
                    if not ok:
                        print(f"Group topic upload failed, trying fallback", flush=True)
                        await _do_send(group_id, None, False)
                except Exception as e:
                    print(f"Group upload failed: {e}", flush=True)
        elif group_id:
            try:
                await _do_send(group_id, None, False)
            except Exception as e:
                print(f"Group upload failed: {e}", flush=True)

        _clean()
        return True

    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return False
    except Exception as e:
        print(f"Upload error on {info.get('msg_id')}: {e}", flush=True)
        return False
    finally:
        if info.get("tmpdir"):
            shutil.rmtree(info["tmpdir"], ignore_errors=True)


async def _pin_status(client, chat_id, msg_id):
    """Batch status message ko pin karo. Pehle Pyrogram se, fail ho to
    Bot API se (dono me se ek zaroor kaam karta hai). Returns True/False."""
    try:
        await client.pin_chat_message(
            chat_id, msg_id, disable_notification=True)
        print("[PIN] Pyrogram pin OK", flush=True)
        return True
    except Exception as e:
        print(f"[PIN] Pyrogram pin fail: {e}", flush=True)
    if BOT_TOKEN:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage"
            data = {"chat_id": str(chat_id), "message_id": str(msg_id),
                    "disable_notification": "true"}
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(url, data=data, timeout=20) as resp:
                    result = await resp.json()
            if result.get("ok"):
                print("[PIN] Bot API pin OK", flush=True)
                return True
            print(f"[PIN] Bot API pin fail: {result}", flush=True)
        except Exception as e:
            print(f"[PIN] Bot API pin exception: {e}", flush=True)
    return False


# --- BATCH RUNNER: SEQUENTIAL (ek download -> upload -> next) ---
# Har file: pehle download karo, turant upload karo, complete.
# Phir agli file. Koi parallel nahi - har file ko poori bandwidth milti hai
# aur files ek-ek karke order me milti hain. Disk par 1 se zyada file nahi.
async def run_batch(client, owner_id, src_chat, start_id, end_id, status_msg):
    # PUBLIC source (username wala link, t.me/name/...): login ki zaroorat
    # NAHI — public channel ko bot khud padh sakta hai, aur bot se fetch
    # kiya gaya file_id BOT ka apna hota hai, isliye fast path (file_id
    # instant) bhi kaam karega. Private source (t.me/c/...) ke liye
    # pehle ki tarah user ka apna userbot session lagega.
    is_public = isinstance(src_chat, str)
    userbot = await get_userbot(owner_id)
    if is_public:
        fetch_client = client  # bot khud fetch/download karega
    elif not userbot:
        await client.send_message(
            owner_id,
            "❌ **Pehle /login karke apne number se login karo.**",
        )
        await safe_edit(status_msg, "❌ **Login required. Pehle /login karo.**")
        return
    else:
        fetch_client = userbot

    ids = list(range(start_id, end_id + 1))
    total = len(ids)
    cancel_event = asyncio.Event()
    ACTIVE_BATCHES[owner_id] = cancel_event
    stats = BatchStats(total)
    # Settings: agar user ne chat ID set kiya hai to files wahi jayengi
    dest_chat = get_settings(owner_id)["chat_id"] or owner_id
    # Log group me user ke naam ka folder (topic) banao
    topic_id = await get_or_create_topic(userbot, owner_id)
    peer_msg = (
        "❌ **Channel/group nahi mila.**\n"
        "Tumhara logged-in account is channel ka **member nahi hai**, "
        "ya link galat hai.\n"
        "• Usi account se channel **join** karo jisse `/login` kiya tha\n"
        "• Link bilkul usi channel ka hona chahiye\n"
        "Phir dobara try karo."
    )

    # Status message ko PIN karo taaki upar dikhe (2 tareeke try honge)
    pinned = await _pin_status(client, owner_id, status_msg.id)
    try:
        # Pinned batch status — live update hoga (sirf batch overview)
        await _update_batch_status(status_msg, stats, force=True)
        if not pinned:
            # Pehli koshish fail hui ho to ek baar aur try karo
            pinned = await _pin_status(client, owner_id, status_msg.id)
        prog_msg = None
        for mid in ids:
            if cancel_event.is_set():
                break
            # STEP 1: message ka metadata + file_id lao (download NAHI)
            info = await fetch_one(fetch_client, src_chat, mid)
            if cancel_event.is_set():
                break
            if info == "PEER_INVALID":
                await client.send_message(owner_id, peer_msg)
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
                await _update_batch_status(status_msg, stats)
                continue
            # HAR FILE ke liye NAYA progress message (neeche se aayega)
            # Purana delete karo, naya banao — taaki sent file ke neeche dikhe
            if prog_msg:
                try:
                    await prog_msg.delete()
                except Exception:
                    pass
            try:
                prog_msg = await client.send_message(
                    owner_id, "⏳ **Taiyaar...**",
                    parse_mode=enums.ParseMode.MARKDOWN)
            except Exception:
                prog_msg = None
            # STEP 2: deliver karo (fast file_id ya download+upload), phir agli file
            # prog_msg = per-file progress (alag message), status_msg = pinned batch
            ok_one = await upload_one(
                client, fetch_client, owner_id, dest_chat, topic_id,
                status_msg, info, stats, prog_msg)
            async with stats.lock:
                stats.done += 1
                if ok_one:
                    stats.ok += 1
                else:
                    stats.fail += 1
            # Pinned status live update (max 3 sec me 1 baar)
            await _update_batch_status(status_msg, stats)
    finally:
        ACTIVE_BATCHES.pop(owner_id, None)
        # Aakhri progress message saaf karo (pinned status rehne do)
        if 'prog_msg' in locals() and prog_msg:
            try:
                await prog_msg.delete()
            except Exception:
                pass

    async with stats.lock:
        ok, fail, done = stats.ok, stats.fail, stats.done
    if cancel_event.is_set() and (done < total):
        await safe_edit(status_msg, "🛑 **Batch stopped.**")
    else:
        await safe_edit(
            status_msg,
            f"✅ **Batch complete!**\n✔️ Success: `{ok}` | ❌ Failed: `{fail}`",
        )



# --- v35: FORCE-JOIN + WELCOME PANELS (video wale reference bot jaisa) ---
async def _has_joined(uid):
    """Force-join check. Channel set na ho ya user owner ho to hamesha True.
    Check khud fail ho jaye (bot channel me admin nahi, API error) to bhi
    True — bot is wajah se kabhi band nahi hona chahiye."""
    if not UPDATES_CHANNEL or _is_owner(uid):
        return True
    try:
        m = await app.get_chat_member(UPDATES_CHANNEL, uid)
        return m.status in (
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.RESTRICTED,
        )
    except Exception as e:
        print(f"[JOIN] check fail uid={uid}: {e}", flush=True)
        return True


def _join_kb():
    rows = []
    ch = UPDATES_CHANNEL.lstrip("@")
    if ch and not ch.lstrip("-").isdigit():
        rows.append([InlineKeyboardButton(
            "🤖 JOIN UPDATES CHANNEL 🤖", url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton(
        "✅ JOIN KAR LIYA — CHECK KARO", callback_data="home_joined")])
    return InlineKeyboardMarkup(rows)


JOIN_WALL_TEXT = (
    "**⚠️ Attention ⚠️**\n\n"
    "**📢 Is bot ko use karne ke liye pehle hamara**\n"
    "**updates channel join karna hoga!**\n\n"
    "**✅ Required Action:**\n"
    "**1. Neeche wale button se channel JOIN karo**\n"
    "**2. Wapas aakar `/start` dabao ya ✅ button dabao**\n\n"
    "**🚫 Access restricted until you join!**"
)


async def _send_join_wall(message):
    if START_IMAGE:
        try:
            await message.reply_photo(
                START_IMAGE, caption=JOIN_WALL_TEXT,
                parse_mode=enums.ParseMode.MARKDOWN, reply_markup=_join_kb())
            return
        except Exception:
            pass
    await message.reply_text(
        JOIN_WALL_TEXT, parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=_join_kb())


def _welcome_text(uid):
    has_session = uid in USER_CLIENTS or str(uid) in _load_json(SESSIONS_FILE, {})
    login = "🟢 Logged in" if has_session else "🔴 Not logged in (/login karo)"
    access = "✅ Allowed" if _is_allowed(uid) else "⛔ Not allowed"
    return (
        "**🚀 Welcome to VILLAN Restriction Bot 🦅**\n\n"
        "**🌟 *Private/Restricted content save karne ka sabse fast tareeka***\n\n"
        "**📌 Key Features:**\n"
        "**- 🛡️ Restriction Bypass — save-restricted videos/files nikalo**\n"
        "**- ⚡ File-ID Fast Path — supported files bilkul instant**\n"
        "**- 📦 Bulk Batch — ek saath 1000 tak links**\n"
        "**- 🔄 Public + Private — dono tarah ke channels**\n"
        "**- 🎛️ Full Customize — rename, caption, watermark, thumbnail**\n\n"
        "**🎯 Perfect For:**\n"
        "**- Course / content collectors 📚**\n"
        "**- Channel admins 🔄**\n"
        "**- Limited data wale users 📶**\n\n"
        f"**🔑 Login:** {login}\n"
        f"**🛡️ Access:** {access}\n"
        f"**👑 Owner:** {OWNER_USERNAME}"
    )


def _welcome_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 ADMIN PANEL", callback_data="home_admin")],
        [InlineKeyboardButton("📋 COMMANDS", callback_data="home_commands")],
        [InlineKeyboardButton("📜 DISCLAIMER", callback_data="home_disclaimer"),
         InlineKeyboardButton("⚠️ WARNINGS", callback_data="home_warnings")],
        [InlineKeyboardButton("❌ CLOSE", callback_data="home_close")],
    ])


def _page_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ BACK", callback_data="home_start"),
         InlineKeyboardButton("❌ CLOSE", callback_data="home_close")],
    ])


COMMANDS_TEXT = (
    "**📋 COMMANDS**\n\n"
    "**👤 User Commands:**\n"
    "**• `/login` — Apne number se login**\n"
    "**• `/logout` — Session khatam karo**\n"
    "**• `/batch` — Bulk extraction (range)**\n"
    "**• `/settings` — Customize panel**\n"
    "**• `/plan` — Apna plan dekho**\n"
    "**• `/buy` — Premium kharido**\n"
    "**• `/help` — Usage guide**\n"
    "**• `/cancel` — Chal raha task roko**\n"
    "**• Direct link bhejo — single file extract**\n\n"
    "**👑 Owner Commands:**\n"
    "**• `/paid <id> <days>` • `/allow` • `/unallow` • `/users`**"
)

DISCLAIMER_TEXT = (
    "**⚖️ Legal Notice**\n\n"
    "**Ye bot:**\n"
    "**• Koi file store nahi karta**\n"
    "**• Kisi illegal activity ko endorse nahi karta**\n"
    "**• Sirf tools provide karta hai**\n"
    "**• Users apne actions ke khud zimmedar hain**\n\n"
    "**Bot use karke tum agree karte ho:**\n"
    "**• Local laws follow karoge**\n"
    "**• Copyright material ka abuse nahi karoge**\n"
    "**• Saare risks tumhare khud ke honge**"
)

WARNINGS_TEXT = (
    "**🔞 Safety First!**\n\n"
    "**🚫 Apna session string kabhi kisi ke saath share mat karo**\n"
    "**🛡️ Testing ke liye secondary accounts use karo**\n"
    "**⚖️ Copyright laws ka respect karo**\n"
    "**💡 Security ke liye saari activities log hoti hain**\n\n"
    "**❌ Strictly Prohibited:**\n"
    "**• Child exploitation material**\n"
    "**• Copyright infringement**\n"
    "**• Scam / Phishing attempts**\n"
    "**• Illegal transactions**"
)

ADMIN_TEXT = (
    "**👑 ADMIN PANEL**\n\n"
    "**• `/paid <user_id> <days>` — Premium plan do**\n"
    "**• `/allow <user_id>` — Lifetime free access**\n"
    "**• `/unallow <user_id>` — Access hatao**\n"
    "**• `/users` — Paid + allowed users ki list**\n\n"
    "**Payment manual hai (UPI) — plan yahi se activate hota hai.**"
)


async def _send_welcome(message, uid):
    if START_IMAGE:
        try:
            await message.reply_photo(
                START_IMAGE, caption=_welcome_text(uid),
                parse_mode=enums.ParseMode.MARKDOWN, reply_markup=_welcome_kb())
            return
        except Exception:
            pass
    await message.reply_text(
        _welcome_text(uid), parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=_welcome_kb())


async def _edit_panel(query, text, kb):
    msg = query.message
    try:
        if msg.photo:
            await msg.edit_caption(
                text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await msg.edit_text(
                text, parse_mode=enums.ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass


async def _handle_home_callbacks(client, query, uid, data):
    try:
        await query.answer()
    except Exception:
        pass
    if data == "home_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if data == "home_joined":
        if await _has_joined(uid):
            await _edit_panel(query, _welcome_text(uid), _welcome_kb())
        else:
            try:
                await query.answer(
                    "❌ Abhi bhi join nahi dikh raha. Pehle channel join karo!",
                    show_alert=True)
            except Exception:
                pass
        return
    if data == "home_start":
        await _edit_panel(query, _welcome_text(uid), _welcome_kb())
        return
    if data == "home_commands":
        await _edit_panel(query, COMMANDS_TEXT, _page_kb())
        return
    if data == "home_disclaimer":
        await _edit_panel(query, DISCLAIMER_TEXT, _page_kb())
        return
    if data == "home_warnings":
        await _edit_panel(query, WARNINGS_TEXT, _page_kb())
        return
    if data == "home_admin":
        if _is_owner(uid):
            await _edit_panel(query, ADMIN_TEXT, _page_kb())
        else:
            try:
                await query.answer("⛔ Ye sirf owner ke liye hai.", show_alert=True)
            except Exception:
                pass
        return


# --- BOT HANDLERS ---
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    uid = message.chat.id
    print(f"⚡ Processing /start for user {uid}", flush=True)
    # v35: Force-join gate (UPDATES_CHANNEL set ho tabhi)
    if not await _has_joined(uid):
        await _send_join_wall(message)
        return
    await _send_welcome(message, uid)
    return



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
    if uid in SETTING_STATES:
        SETTING_STATES.pop(uid, None)
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
    card = await _premium_card(target, client)
    await message.reply_text(
        f"✅ Premium activate ho gaya!\n\n{card}"
    )
    try:
        await client.send_message(
            target,
            f"🎉 Tumhe **{days} din** ka premium access mil gaya hai!\n\n{card}\n\n"
            "`/login` karke shuru karo.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
    except Exception:
        pass


@app.on_message(filters.command("plan") & filters.private)
async def plan_handler(client, message):
    """User apna plan premium card style me dekhe."""
    uid = message.chat.id
    if not _is_allowed(uid):
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    card = await _premium_card(uid, client)
    has_session = uid in USER_CLIENTS or str(uid) in _load_json(SESSIONS_FILE, {})
    login = "🟢 Logged in" if has_session else "🔴 Not logged in (`/login` karo)"
    await message.reply_text(f"{card}\n\n🔑 {login}")


@app.on_message(filters.command("help") & filters.private)
async def help_handler(client, message):
    """Bot kaise use karein — poori guide."""
    help_text = (
        "🦅 **VILLAN BOT — HELP**\n\n"
        "**Bot kaise use karein:**\n\n"
        "1️⃣ `/login` — Apne Telegram number se login karo\n"
        "   (OTP aayega, 2-step password ho to wo bhi)\n\n"
        "2️⃣ `/batch` — Phir channel ka **start link** bhejo,\n"
        "   phir **end link** bhejo\n"
        "   _Ya seedha ek message me range bhejo:_\n"
        "   `https://t.me/c/123456789/100-110`\n\n"
        "3️⃣ Files tumhe mil jayengi ⚡\n\n"
        "**Commands:**\n"
        "• `/start` — Bot shuru karo\n"
        "• `/login` — Number se login\n"
        "• `/logout` — Logout\n"
        "• `/batch` — Batch download\n"
        "• `/settings` — Rename, caption, watermark\n"
        "• `/plan` — Apna plan dekho\n"
        "• `/buy` — Premium kharido\n"
        "• `/cancel` — Chal raha kaam roko\n\n"
        f"👑 **Owner:** `{OWNER_USERNAME}`\n"
        "_Access ya premium ke liye owner se contact karo._"
    )
    await message.reply_text(help_text, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("buy") & filters.private)
async def buy_handler(client, message):
    """Premium kharidne ke liye owner ka username dikhao."""
    await message.reply_text(
        "💎 **VILLAN PREMIUM** 💎\n\n"
        "Premium lene ke liye owner se contact karo:\n"
        f"👑 `{OWNER_USERNAME}`\n\n"
        "Unhe message karo:\n"
        "`Hi, mujhe premium chahiye`\n\n"
        "Payment ke baad tumhara plan activate ho jayega! 🚀",
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
    if not await _has_joined(message.chat.id):
        await _send_join_wall(message)
        return
    if not _is_allowed(message.chat.id):
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    print(f"⚡ Processing /batch for user {message.chat.id}", flush=True)
    # Purani adhuri setting-input state ho to hatao, warna batch ka link
    # galti se setting handler me chala jayega
    SETTING_STATES.pop(message.chat.id, None)
    USER_STATES[message.chat.id] = {"step": "WAITING_FIRST_LINK"}
    await message.reply_text(
        "**🔗 VILLAN BATCH SYSTEM**\n"
        "**━━━━━━━━━━━━━━━**\n"
        "**STEP 1/2 — Source Selection**\n\n"
        "**📎 Start message ka link bhejein**\n"
        "**jahan se extraction shuru karna hai.**\n\n"
        "**❌ Cancel:** `/cancel`",
        parse_mode=enums.ParseMode.MARKDOWN,
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
    if not await _has_joined(uid):
        await _send_join_wall(message)
        return
    if not _is_allowed(uid):
        await message.reply_text(
            "⛔ Tumhe is bot ki permission nahi hai. Owner se contact karo."
        )
        return
    await _cleanup_login(uid)
    LOGIN_STATES[uid] = {"step": "PHONE"}
    await message.reply_text(
        "**📱 SECURE LOGIN**\n"
        "**━━━━━━━━━━━━━━━**\n"
        "**STEP 1/3 — Phone Number**\n\n"
        "**📞 Apna phone number bhejein**\n"
        "**country code ke saath**\n"
        "**Example:** `+919876543210`\n\n"
        "**❌ Cancel:** `/cancel`",
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
        "**✅ LOGIN SUCCESSFUL**\n"
        "**━━━━━━━━━━━━━━━**\n"
        f"**🎉 Welcome, {name}!**\n\n"
        "**🔗 Account successfully linked!**\n\n"
        "**📌 Next Steps:**\n"
        "**• /batch — Bulk extraction**\n"
        "**• Channel link — Direct extract**\n\n"
        "**⚠️ Security: OTP/password wale**\n"
        "**messages delete kar dein.**\n\n"
        "**🚪 Logout:** `/logout`",
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
        code_type = str(getattr(sent, "type", "") or "")
        print(f"[LOGIN] code sent -> type={code_type}", flush=True)
        if "SMS" in code_type:
            hint = ("**📱 Code SMS se bheja gaya hai —**\n"
                    "**apne SMS inbox me check karo.**")
        elif "CALL" in code_type:
            hint = "**📞 Code CALL par aayega — call ka wait karo.**"
        else:
            hint = ("**📱 Code SMS par NAHI aayega!**\n"
                    "**Telegram app ke andar 'Telegram' naam wali**\n"
                    "**official chat me aayega.**\n"
                    "**Jis device me ye number login hai,**\n"
                    "**wahan chat list me sabse upar dekho.**")
        await message.reply_text(
            "**📩 VERIFICATION**\n"
            "**━━━━━━━━━━━━━━━**\n"
            "**STEP 2/3 — OTP Code**\n\n"
            f"{hint}\n\n"
            "**🔑 Wo OTP code yahan bhejein.**\n\n"
            "**❌ Cancel:** `/cancel`",
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
                "**🔐 2-STEP VERIFICATION**\n"
                "**━━━━━━━━━━━━━━━**\n"
                "**STEP 3/3 — Password**\n\n"
                "**🛡️ Is account par 2-step verification**\n"
                "**enabled hai.**\n\n"
                "**🔑 Apna password bhejein.**\n\n"
                "**⚠️ Ye message baad me delete kar dein.**\n\n"
                "**❌ Cancel:** `/cancel`",
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


# --- USER SETTINGS (customize panel) ---
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", os.path.join(DATA_DIR, "user_settings.json"))
SETTING_STATES = {}  # uid -> {"action": ...}

DEFAULT_SETTINGS = {
    "chat_id": None,        # files kahan bheji jayein (None = khud ko)
    "rename_tag": None,     # filename me [Tag] prefix
    "caption": None,        # caption template ({filename} supported)
    "replace_words": [],    # [[old, new], ...]
    "remove_words": [],     # [w1, w2, ...]
    "thumb_file_id": None,  # custom thumbnail
    "pdf_wm": None,         # PDF watermark text
    "video_wm": None,       # video watermark text
    "upload_as_doc": False, # video ko document ki tarah bhejo
}


def get_settings(uid):
    all_s = _load_json(SETTINGS_FILE, {})
    s = dict(DEFAULT_SETTINGS)
    s.update(all_s.get(str(uid), {}))
    return s


def save_settings(uid, s):
    all_s = _load_json(SETTINGS_FILE, {})
    all_s[str(uid)] = s
    _save_json(SETTINGS_FILE, all_s)


def update_setting(uid, key, value):
    s = get_settings(uid)
    s[key] = value
    save_settings(uid, s)


def reset_settings(uid):
    all_s = _load_json(SETTINGS_FILE, {})
    all_s.pop(str(uid), None)
    _save_json(SETTINGS_FILE, all_s)


def settings_keyboard(uid):
    s = get_settings(uid)
    on = lambda v: "✅" if v else ""
    um = "Document" if s["upload_as_doc"] else "Auto"
    kb = [
        [InlineKeyboardButton("Set Chat ID", callback_data="st_chat"),
         InlineKeyboardButton("Set Rename Tag", callback_data="st_rename")],
        [InlineKeyboardButton("Caption", callback_data="st_caption"),
         InlineKeyboardButton("Replace Words", callback_data="st_replace")],
        [InlineKeyboardButton("Remove Words", callback_data="st_remove"),
         InlineKeyboardButton("Reset", callback_data="st_reset")],
        [InlineKeyboardButton("Session Login", callback_data="st_login"),
         InlineKeyboardButton("Logout", callback_data="st_logout")],
        [InlineKeyboardButton(f"{on(s['thumb_file_id'])} Set Thumbnail".strip(),
                              callback_data="st_thumb"),
         InlineKeyboardButton("Remove Thumbnail", callback_data="st_unthumb")],
        [InlineKeyboardButton(f"{on(s['pdf_wm'])} PDF Wtmrk".strip(),
                              callback_data="st_pdfwm"),
         InlineKeyboardButton(f"{on(s['video_wm'])} Video Wtmrk".strip(),
                              callback_data="st_videowm")],
        [InlineKeyboardButton(f"Upload Method: {um}",
                              callback_data="st_uploadmethod")],
        [InlineKeyboardButton("Report Errors ↗", callback_data="st_report")],
    ]
    return InlineKeyboardMarkup(kb)


def apply_filename_rules(filename, s):
    """remove_words -> replace_words -> rename_tag lagao."""
    name, ext = os.path.splitext(filename)
    for w in s.get("remove_words", []):
        if w:
            name = name.replace(w, "")
    for pair in s.get("replace_words", []):
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0]:
            name = name.replace(pair[0], pair[1])
    name = re.sub(r"\.{2,}", ".", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    tag = s.get("rename_tag")
    if tag and not name.startswith(f"[{tag}]"):
        name = f"[{tag}] {name}"
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    if not name:
        name = "file"
    return name + ext


def apply_pdf_watermark(pdf_path, text, tmpdir):
    """Har page par tircha text watermark lagao (pypdf + reportlab)."""
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        import io
        reader = PdfReader(pdf_path)
        if not reader.pages:
            return pdf_path
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=A4)
        c.setFont("Helvetica-Bold", 36)
        c.setFillGray(0.55)
        c.saveState()
        c.translate(297, 420)
        c.rotate(45)
        c.drawCentredString(0, 0, text[:60])
        c.restoreState()
        c.save()
        packet.seek(0)
        wm_page = PdfReader(packet).pages[0]
        writer = PdfWriter()
        for page in reader.pages:
            page.merge_page(wm_page)
            writer.add_page(page)
        out = os.path.join(tmpdir, "wm_" + os.path.basename(pdf_path))
        with open(out, "wb") as f:
            writer.write(f)
        return out
    except Exception as e:
        print(f"PDF watermark failed: {e}", flush=True)
        return pdf_path


def apply_video_watermark(video_path, text, tmpdir):
    """Video par text watermark jalao (ffmpeg). Slow ho sakta hai."""
    try:
        import subprocess
        if not shutil.which("ffmpeg"):
            print("ffmpeg nahi mila, video watermark skip", flush=True)
            return video_path
        out = os.path.join(tmpdir, "wm_" + os.path.basename(video_path))
        safe = re.sub(r"[^a-zA-Z0-9 _-]", "", text)[:40] or "VILLAN"
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vf",
               f"drawtext=text='{safe}':fontsize=28:fontcolor=white@0.85:x=20:y=20",
               "-c:a", "copy", "-preset", "ultrafast", out]
        subprocess.run(cmd, capture_output=True, timeout=7200, check=True)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        return video_path
    except Exception as e:
        print(f"Video watermark failed: {e}", flush=True)
        return video_path


@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client, message):
    uid = message.chat.id
    if not _is_allowed(uid):
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    await message.reply_text(
        "⚙️ **Customize by your end and Configure your settings...**",
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=settings_keyboard(uid),
    )


@app.on_callback_query()
async def settings_callbacks(client, query):
    uid = query.from_user.id
    data = query.data or ""
    if data.startswith("home_"):
        await _handle_home_callbacks(client, query, uid, data)
        return
    if not data.startswith("st_"):
        return
    if not _is_allowed(uid):
        try:
            await query.answer("⛔ Permission nahi hai.", show_alert=True)
        except Exception:
            pass
        return
    try:
        await query.answer()
    except Exception:
        pass

    if data == "st_chat":
        SETTING_STATES[uid] = {"action": "set_chat_id"}
        await query.message.reply_text(
            "📨 **Set Chat ID**\n\nFiles kis chat me bheji jayein, wo ID bhejo\n"
            "(jaise `-1001234567890`).\nWapas apne paas bhejne ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_rename":
        SETTING_STATES[uid] = {"action": "set_rename"}
        await query.message.reply_text(
            "🏷️ **Set Rename Tag**\n\nEk tag bhejo, file ka naam `[Tag] naam.ext` hoga.\n"
            "Hatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_caption":
        SETTING_STATES[uid] = {"action": "set_caption"}
        await query.message.reply_text(
            "💬 **Caption**\n\nCaption template bhejo. `{filename}` likhne par file ka naam aayega.\n"
            "Jaise: `🔥 {filename}`\nHatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_replace":
        SETTING_STATES[uid] = {"action": "set_replace"}
        await query.message.reply_text(
            "🔄 **Replace Words**\n\nHar line par `purana | naya` bhejo.\n"
            "Jaise:\n`x265 | x264`\n`1080p | 720p`\nHatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_remove":
        SETTING_STATES[uid] = {"action": "set_remove"}
        await query.message.reply_text(
            "🧹 **Remove Words**\n\nComma se alag karke words bhejo, jo filename se hatane hain.\n"
            "Jaise: `x265, PSA, 1080p`\nHatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_reset":
        reset_settings(uid)
        await query.message.reply_text("♻️ Sab settings reset ho gayi!",
                                       reply_markup=settings_keyboard(uid))
    elif data == "st_login":
        await _cleanup_login(uid)
        LOGIN_STATES[uid] = {"step": "PHONE"}
        await query.message.reply_text(
            "**📱 SECURE LOGIN**\n"
            "**━━━━━━━━━━━━━━━**\n"
            "**STEP 1/3 — Phone Number**\n\n"
            "**📞 Apna phone number bhejein**\n"
            "**country code ke saath**\n"
            "**Example:** `+919876543210`",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_logout":
        await _cleanup_login(uid)
        await _logout_user(uid)
        await query.message.reply_text("👋 Logout ho gaya. `/login` se dobara login karo.")
    elif data == "st_thumb":
        SETTING_STATES[uid] = {"action": "set_thumb"}
        await query.message.reply_text(
            "🖼️ **Set Thumbnail**\n\nThumbnail ke liye ek **photo** bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_unthumb":
        update_setting(uid, "thumb_file_id", None)
        await query.message.reply_text("✅ Thumbnail hata diya.",
                                       reply_markup=settings_keyboard(uid))
    elif data == "st_pdfwm":
        SETTING_STATES[uid] = {"action": "set_pdfwm"}
        await query.message.reply_text(
            "📄 **PDF Watermark**\n\nWatermark text bhejo (PDF ke har page par tircha lagega).\n"
            "Hatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_videowm":
        SETTING_STATES[uid] = {"action": "set_videowm"}
        await query.message.reply_text(
            "🎬 **Video Watermark**\n\nWatermark text bhejo (video par likha aayega).\n"
            "⚠️ Note: video dobara encode hogi, isliye **slow** hoga.\n"
            "Hatane ke liye `off` bhejo.",
            parse_mode=enums.ParseMode.MARKDOWN)
    elif data == "st_uploadmethod":
        s = get_settings(uid)
        update_setting(uid, "upload_as_doc", not s["upload_as_doc"])
        mode = "Document" if not s["upload_as_doc"] else "Auto"
        await query.message.reply_text(f"✅ Upload method: **{mode}**",
                                       parse_mode=enums.ParseMode.MARKDOWN,
                                       reply_markup=settings_keyboard(uid))
    elif data == "st_report":
        SETTING_STATES[uid] = {"action": "report"}
        await query.message.reply_text(
            "🐞 **Report Errors**\n\nKya problem aayi? Detail me likh ke bhejo, owner tak pahuncha dunga.",
            parse_mode=enums.ParseMode.MARKDOWN)


async def handle_setting_input(client, message):
    uid = message.chat.id
    st = SETTING_STATES.get(uid)
    if not st:
        return
    action = st["action"]
    text = (message.text or "").strip()

    async def done(msg):
        SETTING_STATES.pop(uid, None)
        await message.reply_text(msg, parse_mode=enums.ParseMode.MARKDOWN,
                                 reply_markup=settings_keyboard(uid))

    if action == "set_chat_id":
        if text.lower() == "off":
            update_setting(uid, "chat_id", None)
            await done("✅ **Chat ID hata diya. Files ab tumhe milengi.**")
            return
        try:
            cid = int(text)
        except ValueError:
            await message.reply_text("❌ Sahi ID bhejo, jaise `-1001234567890`.")
            return
        update_setting(uid, "chat_id", cid)
        await done(f"✅ **Chat ID set: `{cid}`**\n**Ab files wahi bheji jayengi.**")
    elif action == "set_rename":
        if text.lower() == "off":
            update_setting(uid, "rename_tag", None)
            await done("✅ **Rename tag hata diya.**")
        else:
            update_setting(uid, "rename_tag", text[:40])
            await done(f"✅ **Tag set: `[{text[:40]}]`**")
    elif action == "set_caption":
        if text.lower() == "off":
            update_setting(uid, "caption", None)
            await done("✅ **Caption hata diya.**")
        else:
            update_setting(uid, "caption", text[:500])
            await done("✅ **Caption set ho gaya.**")
    elif action == "set_replace":
        if text.lower() == "off":
            update_setting(uid, "replace_words", [])
            await done("✅ **Replace rules hata diye.**")
        else:
            pairs = []
            for line in text.split("\n"):
                if "|" in line:
                    old, new = line.split("|", 1)
                    old, new = old.strip(), new.strip()
                    if old:
                        pairs.append([old, new])
            update_setting(uid, "replace_words", pairs)
            await done(f"✅ **`{len(pairs)}` replace rules set.**")
    elif action == "set_remove":
        if text.lower() == "off":
            update_setting(uid, "remove_words", [])
            await done("✅ **Remove words hata diye.**")
        else:
            words = [w.strip() for w in text.split(",") if w.strip()]
            update_setting(uid, "remove_words", words)
            await done(f"✅ **`{len(words)}` words hatane ke liye set.**")
    elif action == "set_pdfwm":
        if text.lower() == "off":
            update_setting(uid, "pdf_wm", None)
            await done("✅ **PDF watermark hata diya.**")
        else:
            update_setting(uid, "pdf_wm", text[:60])
            await done(f"✅ **PDF watermark set: `{text[:60]}`**")
    elif action == "set_videowm":
        if text.lower() == "off":
            update_setting(uid, "video_wm", None)
            await done("✅ **Video watermark hata diya.**")
        else:
            update_setting(uid, "video_wm", text[:40])
            await done(f"✅ **Video watermark set: `{text[:40]}`**\n⚠️ **Video slow encode hogi.**")
    elif action == "report":
        if OWNER_ID:
            try:
                await client.send_message(
                    OWNER_ID,
                    f"🐞 **Error report** from `{uid}`:\n\n{text[:1000]}",
                    parse_mode=enums.ParseMode.MARKDOWN)
                await done("✅ **Report owner ko bhej diya.**")
            except Exception:
                await done("❌ **Report nahi bheja ja saka.**")
        else:
            await done("❌ **Owner set nahi hai.**")


@app.on_message(filters.photo & filters.private)
async def thumb_photo_handler(client, message):
    uid = message.chat.id
    st = SETTING_STATES.get(uid)
    if not st or st.get("action") != "set_thumb":
        return
    if not _is_allowed(uid):
        SETTING_STATES.pop(uid, None)
        await message.reply_text("⛔ Tumhe is bot ki permission nahi hai.")
        return
    SETTING_STATES.pop(uid, None)
    file_id = message.photo[-1].file_id
    update_setting(uid, "thumb_file_id", file_id)
    await message.reply_text("✅ Thumbnail set ho gaya!",
                             reply_markup=settings_keyboard(uid))


@app.on_message(
    filters.text
    & filters.private
    & ~filters.command(
        ["start", "batch", "cancel", "login", "logout", "settings",
         "allow", "unallow", "users", "paid", "plan", "help", "buy"]
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

    # Settings input flow (koi setting ka jawab aa raha ho)
    if user_id in SETTING_STATES:
        await handle_setting_input(client, message)
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
            "**🔗 VILLAN BATCH SYSTEM**\n"
            "**━━━━━━━━━━━━━━━**\n"
            "**STEP 2/2 — Range Complete**\n\n"
            f"**✅ Start Locked:** `{start_id}`\n\n"
            "**📎 Ab aakhiri (last) message ka**\n"
            "**link bhejein jahan tak**\n"
            "**extraction karna hai.**\n\n"
            "**❌ Cancel:** `/cancel`",
            parse_mode=enums.ParseMode.MARKDOWN,
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
        if total_files > BATCH_LIMIT:
            await message.reply_text(f"⚠️ Max {BATCH_LIMIT} posts allow hain.")
            return

        status_msg = await message.reply_text(
            f"🦅 **Batch Started!** `{total_files}` items queued (ek-ek karke)."
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
        if total > BATCH_LIMIT:
            await message.reply_text(f"⚠️ Max {BATCH_LIMIT} posts allow hain.")
            return

        status_msg = await message.reply_text(
            f"⏳ `{total}` item(s) processing (ek-ek karke)..."
        )
        await run_batch(client, user_id, chat_id, start_id, end_id, status_msg)


# --- MAIN ENGINE ---
async def main():
    await start_web_server()

    print("🚀 Starting Bot Client...", flush=True)
    await app.start()

    me = await app.get_me()
    try:
        await app.set_bot_commands([
            BotCommand("start", "Bot start karo"),
            BotCommand("login", "Apne number se login"),
            BotCommand("logout", "Session khatam karo"),
            BotCommand("batch", "Bulk extraction (range)"),
            BotCommand("settings", "Customize panel"),
            BotCommand("plan", "Apna plan dekho"),
            BotCommand("buy", "Premium kharido"),
            BotCommand("help", "Usage guide"),
            BotCommand("cancel", "Chal raha task roko"),
        ])
        print("👉 Bot command menu set ho gaya", flush=True)
    except Exception as e:
        print(f"[MENU] set_bot_commands fail: {e}", flush=True)
    print("========================================", flush=True)
    print(f"👉 BOT LISTENING AS: @{me.username}", flush=True)
    print(f"👉 VERSION: {VERSION}", flush=True)
    print("👉 MODE: sequential (ek download → upload → next)", flush=True)
    print(f"👉 OWNER_ID: {OWNER_ID if OWNER_ID else 'not set (open mode)'}", flush=True)
    print("========================================", flush=True)

    # Redeploy-proof data: backup channel se missing JSON wapas lao
    if BACKUP_CHAT_ID:
        print(f"👉 BACKUP_CHAT_ID: {BACKUP_CHAT_ID} (redeploy-proof backup ON)", flush=True)
        await _backup_restore_all()
    else:
        print("👉 BACKUP_CHAT_ID: not set (bina volume ke redeploy par data udega)", flush=True)

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
