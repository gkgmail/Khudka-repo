<div align="center">

<img src="https://github.com/user-attachments/assets/fe5bbb08-50ba-470b-a36b-29933787473d" width="400">

Bhai, is screenshot ne poori kahani khol di! Tumhare editor me dikh raha hai ki tumhari README.md me sirf 2 lines hain:

GIF wali image line (jo user-attachments link se aayi — isliye logo dikh raha tha)
README_(3).pdf wali link

Isliye page par kuch aur dikh hi nahi raha — README ke andar mera wala content hai hi nahi! Aur PDF isliye ban raha hai kyunki tum file ko editor me drag kar rahe ho — GitHub drag ki hui file ka content nahi daalta, uska link bana deta hai. Wahi link baar-baar badh ke (1), (2), (3) hota ja raha hai.

Ab final fix (editor tumhara khula hi hua hai — perfect timing)
Editor me Ctrl+A dabake sab kuch Delete karo
Neeche wale box se poora text copy karke editor me paste karo (koi file drag mat karna is baar)
Upar "Commit changes..." dabao

Ek khaas baat: is text me logo ki line me tumhara wahi link laga diya hai jo pehle se kaam kar raha hai (user-attachments wala), sirf size 400 kar diya hai — to GIF dobara upload karne ki zaroorat hi nahi hai.

README-TYPING-SVG.HEROKUAPP.COM

readme-typing-svg.herokuapp.com

](https://git.io/typing-svg)

Image not found
Image not found
Image not found
Image not found

A Telegram bot that saves restricted content 🎬 (videos, photos, documents, audio) from
private and public channels — one batch at a time, with per-user login, paid plans,
and automatic backup so redeploys never wipe your data. 💾

✨ Features
📦 Batch download — send a post link + range, the bot fetches every file in order
⚡ File-ID fast path — instant server-side copy whenever possible (no re-upload)
🔐 Per-user login — /login with phone → OTP → 2FA, each user's session stored separately
🚶 Sequential transfers with flood pacing — one file at a time + 3s gap between files,
so Telegram's FloodWait limits never trip on big batches
📂 Dual delivery — every file goes to the user's personal chat and to a log-group
topic folder (one folder per user, never recreated)
💎 Paid plans — /paid <user_id> <days>, lifetime /allow, /users, /plan, /unallow
🎛️ Custom settings — /settings panel: rename tag, caption template, replace/remove
words, thumbnail, PDF/video watermark, upload method
💾 Redeploy-proof backup — all data files auto-back up to a private channel and
restore themselves on startup
🔔 Force-join gate — optional updates-channel check on /start, /login and /batch
🖼️ Welcome screen — photo welcome on /start with buttons (Admin Panel, Commands,
Disclaimer, Warnings)
🛠️ Tech Stack
🐍 Python 3.11
Pyrogram 2.0.106 (MTProto)
☁️ Runs on Koyeb (free Eco tier works)
🚀 Setup
1️⃣ Clone & install
2️⃣ Environment variables

Create these in your host (Koyeb → Service → Environment variables):

Variable	Required	What it is
BOT_TOKEN	✅	Bot token from @BotFather
API_ID	✅	From my.telegram.org
API_HASH	✅	From my.telegram.org
OWNER_ID	✅	Your Telegram user ID (bot owner)
LOG_GROUP_ID	✅	Log group ID (forum/topics enabled) for per-user folders
BACKUP_CHAT_ID	⭐ Recommended	Private channel ID for redeploy-proof backup (see below)
UPDATES_CHANNEL	Optional	Updates channel for force-join — bare @username or numeric ID (not a t.me link)
START_IMAGE	Optional	Photo URL or file_id for the /start welcome screen (empty = text only)
3️⃣ Run

On Koyeb, connect this repo, set the env vars above, and deploy. Health checks run on
port 8080. ✅

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
⚠️ Notes
💸 Payments are manual (UPI etc.) — there is no automatic payment gateway in this bot.
🚶 Transfers are strictly sequential by design: one download → one upload → next file.
📦 Big batches (500+ files) are best split into chunks of 100–150 files with a short gap
between them.
🔒 Never share your BOT_TOKEN, API_ID/API_HASH, or any session string with
anyone, and never commit them to the repo — use environment variables only.
⚖️ Disclaimer

This project is for personal and educational use. Only download content you have the
right to access, respect creators' rights, and do not redistribute copyrighted
material. The author is not responsible for misuse.

✨ Made for fast, safe & hassle-free batches ✨

~~~

Commit karne ke baad page refresh karo — logo sahi size me aur poora README dikhna chahiye. (Aur haan, commit se auto-deploy ho sakta hai — batch chal raha ho to uska dhyaan rakhna.)
