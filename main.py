import os
import re
import time
import math
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

# Multi-step batch state storage: {user_id: {"step": "...", "chat_id": ..., "start_id": ...}}
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
    tmp = (
        ((str(days) + "d, ") if days else "") +
        ((str(hours) + "h, ") if hours else "") +
        ((str(minutes) + "m
    
