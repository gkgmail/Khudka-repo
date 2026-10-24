<div align="center">

<img src="https://github.com/user-attachments/assets/fe5bbb08-50ba-470b-a36b-29933787473d" width="400">



A Telegram bot that saves restricted content 🎬 (videos, photos, documents, audio) from
private and public channels — one batch at a time, with per-user login, paid plans,
and automatic backup so redeploys never wipe your data. 💾

📚 About
Pyrogram 2.0.106 based bot for saving restricted content. Public channel links
work without login — the bot fetches them itself. Private content needs
/login with your own Telegram account.

✨ Features
📦 Batch download — send a post link + range, the bot fetches every file in order
⚡ File-ID fast path — instant server-side copy whenever possible (no re-upload)
🔐 Per-user login — /login with phone → OTP → 2FA, each user's session stored separately
🚶 Sequential transfers with flood pacing — one file at a time + 3s gap between files,
so Telegram's FloodWait limits never trip on big batches
📌 Pinned batch status — one pinned status message per batch, plus a fresh progress
message below each delivered file
📂 Dual delivery — every file goes to the user's personal chat and to a log-group
topic folder (one folder per user, never recreated)
💎 Paid plans — /paid <user_id> <days>, lifetime /allow, /users, /plan, /unallow
🎛️ Custom settings — /settings panel: rename tag, caption template, replace/remove
words, thumbnail, PDF/video watermark, upload method
💾 Redeploy-proof backup — logins, plans, settings and topic folders auto-back up to
a private channel and restore themselves on startup
🔔 Force-join gate — optional updates-channel check on /start, /login and /batch
(owner always bypasses, fail-open on API errors so the bot never bricks)
🖼️ Welcome screen — photo welcome on /start with buttons (Admin Panel, Commands,
Disclaimer, Warnings)
❤️ Health check — built-in HTTP endpoint for Koyeb health checks
🛠️ Tech Stack
🐍 Python 3.11
Pyrogram 2.0.106 (MTProto)
🎬 ffmpeg (video watermark)
📄 Pillow, pypdf, reportlab (thumbnails, PDF watermark)
☁️ Runs on Koyeb (Dockerfile build)
🤖 Commands
For users:

Command	What it does
/start	👋 Welcome screen + login/access status
/login	🔐 Log in with your own Telegram account (phone → OTP → 2FA)
/logout	🚪 Remove your saved login
/batch	📦 Start a batch from a post link
/plan	💎 Check your remaining premium time
/settings	🎛️ Customize rename, caption, thumbnail, watermarks
/cancel	❌ Cancel the current step or batch
/help	❓ Usage guide
For the owner only:

Command	What it does
/paid <user_id> <days>	💰 Grant a time-based premium plan
/allow <user_id>	♾️ Grant lifetime free access
/unallow <user_id>	🚫 Remove all access
/users	👥 List paid + lifetime users with remaining days
⚙️ Required Variables
Set these as environment variables on your host. Never hard-code them
in the repo.

Variable	Required	What it is
BOT_TOKEN	✅	Bot token from @BotFather
API_ID	✅	From my.telegram.org
API_HASH	✅	From my.telegram.org
OWNER_ID	✅	Your Telegram user ID (bot owner)
LOG_GROUP_ID	✅	Log group ID (forum/topics enabled) for per-user folders
SESSION_STRING	Recommended	Owner account session string — powers backup restore and topic search (keep it on one service only)
BACKUP_CHAT_ID	⭐ Recommended	Private channel ID for redeploy-proof backup (see below)
UPDATES_CHANNEL	Optional	Updates channel for force-join — bare @username or numeric ID (not a t.me link)
START_IMAGE	Optional	Photo URL or file_id for the /start welcome screen (empty = text only)
DATA_DIR	Optional	Custom folder for data files
PORT	Optional	Health check port (default 8080)
💾 Backup setup (one-time, highly recommended)
Without this, every redeploy wipes logins, plans and settings. With it, everything
restores automatically:

Create a private Telegram channel (only you + the bot).
Add the bot as admin in that channel.
Set BACKUP_CHAT_ID to that channel's ID (e.g. -100...).
Grant one /paid or /allow, wait ~10 seconds, and confirm backup:... files
appear in the channel.
Redeploy once — startup logs must show backup restore: <file> OK lines. 🎉
⚠️ Keep this channel private. Its backup files contain login session strings.
Never share them, and never post them publicly.

🚀 Deployment
Koyeb
Koyeb me naya service banao, build type Dockerfile select karo.
Is repo ko connect karo.
Upar wali environment variables set karo aur deploy karo.
VPS
bash


sudo apt update
sudo apt install ffmpeg git python3-pip
git clone https://github.com/gkgmail/Khudka-repo
cd Khudka-repo
pip install -r requirements.txt
python main.py
Background me chalane ke liye:

bash


screen -S villan
python main.py
Detach: Ctrl + A, phir Ctrl + D
Wapas aane ke liye: screen -r villan
Band karne ke liye: screen -S villan -X quit
⚠️ Notes
💸 Payments are manual (UPI etc.) — there is no automatic payment gateway in this bot.
🚶 Transfers are strictly sequential by design: one download → one upload → next file.
📦 Big batches (500+ files) are best split into chunks of 100–150 files with a short gap
between them.
📉 Koyeb's free Eco tier throttles bandwidth — full speed needs a VPS or paid host.
🔒 Never share your BOT_TOKEN, API_ID/API_HASH, or any session string with
anyone, and never commit them to the repo — use environment variables only.
⚖️ Disclaimer
This project is for personal and educational use. Only download content you have the
right to access, respect creators' rights, and do not redistribute copyrighted
material. The author is not responsible for misuse.

✨ Made for fast, safe & hassle-free batches ✨
