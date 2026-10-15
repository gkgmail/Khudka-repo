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
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
import pyrogram.utils as _pyro_utils
_pyro_utils.MIN_CHANNEL_ID = -100999999999999

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("VILLAN_BOT")

VERSION = "v18"

# --- CONFIGURATION (Koyeb Environment Variables) ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()
PORT = int(os.environ.get("PORT", 8080))
PARALLEL_TASKS = max(1, int(os.environ.get("PARALLEL_TASKS", "3")))
PROGRESS_EVERY = float(os.environ.get("PROGRESS_EVERY", "5"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
SESSIONS_FILE = os.environ.get("SESSIONS_FILE", os.path.join(DATA_DIR, "sessions.json"))
ALLOWED_FILE = os.environ.get("ALLOWED_FILE", os.path.join(DATA_DIR, "allowed_users.json"))
SUBS_FILE = os.environ.get("SUBS_FILE", os.path.join(DATA_DIR, "subscriptions.json"))
LOG_GROUP_ID = int(os.environ.get("LOG_GROUP_ID", "-1004328020590") or 0) or None
TOPICS_FILE = os.environ.get("TOPICS_FILE", os.path.join(DATA_DIR, "user_topics.json"))
BATCH_LIMIT = max(1, int(os.environ.get("BATCH_LIMIT", "1000")))

try:
    _raw_backup = os.environ.get("BACKUP_CHAT_ID", "").strip()
    BACKUP_CHAT_ID = int(_raw_backup) if _raw_backup else None
except ValueError:
    BACKUP_CHAT_ID = None
_BACKUP_KNOWN = ("sessions.json", "allowed_users.json", "subscriptions.json",
                  "user_settings.json", "user_topics.json")
_BACKUP_MSG_IDS = {}


def _backup_caption(fname):
    return f"backup:{fname}"


async def _backup_upload(fname, path):
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
    if not BACKUP_CHAT_ID:
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
            async for m in app.get_chat_history(BACKUP_CHAT_ID, limit=100):
                if m.document and (m.caption or "") == cap:
                    latest = m
                    break
            if latest is None:
                continue
            await app.download_media(latest, file_name=path)
            _BACKUP_MSG_IDS[fname] = latest.id
            print(f"backup restore: {fname}", flush=True)
        except Exception as e:
            print(f"backup restore {fname} failed: {e}", flush=True)

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

USER_STATES = {}
ACTIVE_BATCHES = {}
LOGIN_STATES = {}
USER_CLIENTS = {}
_OWNER_UB_STARTED = False


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
    sub = _load_subs().get(str(uid))
    if not sub:
        return 0
    return max(0, int(sub.get("expiry", 0) - time.time()))


def _is_allowed(uid: int) -> bool:
    if OWNER_ID == 0:
        return True
    if uid in _get_allowed():
        return True
    return _sub_remaining(uid) > 0


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
    try:
        await msg.edit_text(text, parse_mode=enums.ParseMode.MARKDOWN)
    except FloodWait:
        pass
    except Exception:
        pass


class BatchStats:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.start = time.time()
        self.lock = asyncio.Lock()


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


async def _fetch_with_warmup(user_client, src_chat, msg_id):
    try:
        return await user_client.get_messages(src_chat, msg_id)
    except PeerIdInvalid:
        pass
    except ValueError as e:
        if "Peer id invalid" not in str(e):
            raise
    try:
        await user_client.get_dialogs(limit=500)
    except Exception as e:
        print(f"dialog warmup failed: {e}", flush=True)
    return await user_client.get_messages(src_chat, msg_id)


async def fetch_one(user_client, src_chat, msg_id):
    if not user_client:
        return "OFFLINE"
    try:
        target_msg = await _fetch_with_warmup(user_client, src_chat, msg_id)
        if not target_msg or target_msg.empty:
            return None
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
        info = {
            "kind": kind,
            "msg_id": msg_id,
            "file_id": file_id,
            "file_size": file_size,
            "file_name": os.path.basename(
                getattr(media_obj, "file_name", "") or f"media_{msg_id}"
            ),
            "caption": target_msg.caption or "",
            "target_msg": target_msg,
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
    except PeerIdInvalid:
        return "PEER_INVALID"
    except ValueError as e:
        if "Peer id invalid" in str(e):
            return "PEER_INVALID"
        raise
    except (ChannelPrivate, ChatAdminRequired, UserNotParticipant):
        return "BREAK"
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return None
    except Exception as e:
        print(f"Fetch error on {msg_id}: {e}", flush=True)
        return None


async def download_file(user_client, info, status_msg, stats=None):
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
                status_msg, start_time, last_update,
                f"📥 Downloading • ID `{msg_id}`", stats,
            ),
        )
        if not file_path or not os.path.exists(file_path):
            return None
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
    kw = {"reply_to_message_id": topic_id} if topic_id else {}
    if kind == "video":
        return await client.send_video(
            dest, video=file_id, caption=caption,
            duration=info.get("duration"),
            width=info.get("width"), height=info.get("height"), **kw)
    elif kind == "audio":
        return await client.send_audio(
            dest, audio=file_id, caption=caption,
            duration=info.get("duration"),
            performer=info.get("performer"), title=info.get("title"), **kw)
    elif kind == "photo":
        return await client.send_photo(dest, photo=file_id, caption=caption, **kw)
    elif kind == "voice":
        return await client.send_voice(
            dest, voice=file_id, caption=caption,
            duration=info.get("duration"), **kw)
    elif kind == "video_note":
        return await client.send_video_note(dest, video_note=file_id, **kw)
    else:
        return await client.send_document(
            dest, document=file_id, caption=caption, **kw)


# --- GROUP TOPIC: raw API se bhejo (top_msg_id) ---
async def _raw_send_to_topic(client, peer, topic_id, kind, file_id=None,
                             file_path=None, caption="", info=None, thumb=None):
    from pyrogram import raw
    from pyrogram.file_id import FileId, FileType
    import random as _rand
    import mimetypes
    info = info or {}
    try:
        if file_id:
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
        await client.invoke(raw.functions.messages.SendMedia(
            peer=peer,
            media=media,
            message=caption or "",
            random_id=_rand.randint(1, 2**63 - 1),
            top_msg_id=topic_id,
        ))
        return True
    except Exception as e:
        print(f"Raw topic send failed: {e}", flush=True)
        return False


async def _find_topic_id(client, channel, title):
    from pyrogram import raw
    try:
        res = await client.invoke(raw.functions.channels.GetForumTopics(
            channel=channel, offset_date=0, offset_id=0,
            offset_topic=0, limit=100, q=title))
        for t in res.topics:
            if t.title == title:
                return t.id
    except Exception as e:
        print(f"Find topic failed: {e}", flush=True)
    return None


async def get_or_create_topic(client, uid):
    if not LOG_GROUP_ID:
        return None
    try:
        u = await client.get_users(uid)
        title = f"{(u.first_name or 'User')[:25]} • {uid}"
    except Exception:
        title = f"User • {uid}"
    try:
        from pyrogram import raw
        import random as _rand
        peer = await client.resolve_peer(LOG_GROUP_ID)
        channel = raw.types.InputChannel(
            channel_id=peer.channel_id, access_hash=peer.access_hash)
        topic_id = await _find_topic_id(client, channel, title)
        if not topic_id:
            await client.invoke(raw.functions.channels.CreateForumTopic(
                channel=channel, title=title,
                random_id=_rand.randint(1, 2**63 - 1)))
            await asyncio.sleep(1)
            topic_id = await _find_topic_id(client, channel, title)
        if topic_id:
            topics = _load_json(TOPICS_FILE, {})
            topics[str(uid)] = topic_id
            _save_json(TOPICS_FILE, topics)
            print(f"Topic ready for {uid}: topic={topic_id}", flush=True)
            return topic_id
    except Exception as e:
        print(f"Topic setup failed: {e}", flush=True)
    return _load_json(TOPICS_FILE, {}).get(str(uid))


async def upload_one(client, userbot, uid, dest_chat, topic_id, status_msg, info, stats=None):
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
                await client.invoke(raw.functions.messages.SendMessage(
                    peer=peer, message=text,
                    random_id=_rand.randint(1, 2**63 - 1),
                    top_msg_id=topic_id))
            else:
                await client.send_message(group_id, text)
        except Exception as e:
            print(f"Group text send failed: {e}", flush=True)

    try:
        cap = info.get("caption") or ""
        if s["caption"]:
            try:
                cap = s["caption"].format(filename=info.get("file_name", "file"))
            except Exception:
                cap = s["caption"]

        if kind == "text":
            await client.send_message(dest_chat, info["text"])
            await _to_group_text(info["text"])
            return True
        if kind == "too_big":
            await client.send_message(
                dest_chat,
                f"⚠️ **Message {info['msg_id']}:** File 2GB se badi hai.",
            )
            return False

        file_id = info.get("file_id")
        if file_id and not _needs_file(s, info):
            try:
                await _send_by_file_id(client, dest_chat, kind, file_id, cap, info)
                if group_id and topic_id:
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
                    try:
                        await _send_by_file_id(
                            client, group_id, kind, file_id, cap, info)
                    except Exception as e:
                        print(f"Group file_id send failed: {e}", flush=True)
                return True
            except Exception as e:
                print(f"Fast path failed ({info['msg_id']}): {e} — downloading...",
                      flush=True)

        fp = await download_file(userbot, info, status_msg, stats)
        if not fp:
            _clean()
            return False

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

        th = info.get("thumb_path")
        if s["thumb_file_id"]:
            th = s["thumb_file_id"]

        if kind == "document" and new_base.lower().endswith(".pdf") and s["pdf_wm"]:
            fp = apply_pdf_watermark(fp, s["pdf_wm"], info["tmpdir"])
            info["file_path"] = fp
        if kind == "video" and s["video_wm"]:
            fp = apply_video_watermark(fp, s["video_wm"], info["tmpdir"])
            info["file_path"] = fp

        send_kind = kind
        if kind == "video" and s["upload_as_doc"]:
            send_kind = "document"

        async def _do_send(dchat, dtopic, show_progress):
            kw = {"reply_to_message_id": dtopic} if dtopic else {}
            if show_progress:
                last_update = [time.time()]
                start_time = time.time()
                pa = (status_msg, start_time, last_update)
                title = f"🚀 Uploading • ID `{info.get('msg_id')}`"
                prog = {"progress": progress_bar,
                        "progress_args": pa + (title, stats)}
            else:
                prog = {}
            if send_kind == "video":
                await client.send_video(
                    dchat, video=fp, caption=cap, thumb=th,
                    duration=info.get("duration"),
                    width=info.get("width"), height=info.get("height"),
                    **prog, **kw)
            elif send_kind == "document":
                await client.send_document(
                    dchat, document=fp, caption=cap, thumb=th, **prog, **kw)
            elif send_kind == "audio":
                await client.send_audio(
                    dchat, audio=fp, caption=cap,
                    duration=info.get("duration"),
                    performer=info.get("performer"), title=info.get("title"),
                    **prog, **kw)
            elif send_kind == "photo":
                await client.send_photo(dchat, photo=fp, caption=cap, **kw)
            elif send_kind == "voice":
                await client.send_voice(
                    dchat, voice=fp, caption=cap,
                    duration=info.get("duration"), **kw)
            elif send_kind == "video_note":
                await client.send_video_note(dchat, video_note=fp, **kw)
            else:
                await client.send_document(
                    dchat, document=fp, caption=cap, **prog, **kw)

        try:
            await _do_send(dest_chat, None, True)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            _clean()
            return False
        except Exception as e:
            print(f"Personal upload failed ({info.get('msg_id')}): {e}", flush=True)
            _clean()
            return False

        if group_id and topic_id:
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


async def run_batch(client, owner_id, src_chat, start_id, end_id, status_msg):
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
    dest_chat = get_settings(owner_id)["chat_id"] or owner_id
    topic_id = await get_or_create_topic(client, owner_id)
    peer_msg = (
        "❌ **Channel/group nahi mila.**\n"
        "Tumhara logged-in account is channel ka **member nahi hai**, "
        "ya link galat hai.\n"
        "• Usi account se channel **join** karo jisse `/login` kiya tha\n"
        "• Link bilkul usi channel ka hona chahiye\n"
        "Phir dobara try karo."
    )

    try:
        await safe_edit(
            status_msg,
            f"📦 **Batch:** `{total}` files — ek-ek karke\n"
            f"⚡ **Fast mode:** file_id se instant delivery\n"
            f"⏹ Cancel: `/cancel`",
        )
        for mid in ids:
            if cancel_event.is_set():
                break
            info = await fetch_one(userbot, src_chat, mid)
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
                continue
            ok_one = await upload_one(
                client, userbot, owner_id, dest_chat, topic_id,
                status_msg, info, stats)
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
