# 🎧 Dr. Dev Support — Telegram Customer Support Bot + WhatsApp-Style Admin Panel (Flask + Firebase)

> **Open-source Telegram customer support system in Python.** Your customers message a Telegram bot, you answer from a
> beautiful **WhatsApp-style web admin panel** (or straight from Telegram with `/send`). Photos, videos and files, reactions,
> broadcasts with history, anti-spam, themes, and a full audit log — stored in **Firebase Realtime Database** and deployable
> for **free on Render**.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)
![Firebase](https://img.shields.io/badge/Firebase-Realtime%20Database-FFCA28?logo=firebase&logoColor=black)
![Render](https://img.shields.io/badge/Deploy-Render%20Free%20Tier-46E3B7?logo=render&logoColor=black)

**Keywords:** telegram support bot · telegram customer service bot · telegram helpdesk · telegram live chat · flask admin panel ·
firebase realtime database python · telegram broadcast bot · pyTelegramBotAPI example · WhatsApp-style chat UI · free bot hosting on Render

---

## 📑 Table of contents
- [Why this project?](#-why-this-project)
- [Features](#-features)
- [Telegram admin commands](#-telegram-admin-commands)
- [Quick start (5 minutes)](#-quick-start-5-minutes)
- [Environment variables](#-environment-variables)
- [Firebase setup](#-firebase-setup)
- [Deploy for free on Render](#-deploy-for-free-on-render)
- [Admin panel guide](#-admin-panel-guide)
- [How it works](#-how-it-works)
- [Security notes](#-security-notes)
- [FAQ](#-faq)
- [Author & custom projects](#-author--custom-projects)

## 💡 Why this project?
Most Telegram support bots just forward messages to a group and lose the history. **Dr. Dev Support** gives you a real
**helpdesk**: every customer gets a conversation thread with search, unread counters, read receipts, reactions, edit/delete,
file sharing and a block button — all in a fast mobile-friendly web panel, backed by a free database. It runs on one
small Python process, so the free tier of Render is enough.

## ✨ Features

### 💬 Conversations (WhatsApp-style)
- Chat bubbles with **Today / Yesterday / weekday / full-date** separators, ✓ / ✓✓ read ticks and an **edited** tag
- Deleted messages show a clean **❌ Deleted** marker (the record is kept in Firebase — see *Soft delete*)
- **Search inside a chat** like Telegram: result counter (`2 of 9`), ↑ goes to older matches, ↓ to newer ones, matches highlighted
- **Smart user search**: name, `@username`, chat ID *and* last message — accent-insensitive, multi-word, highlighted — plus *All / Unread / Blocked* filters
- **Reactions both ways** — admin reactions appear on the user's Telegram message, user reactions appear in the panel and ping you
- Photos, videos and documents both directions (lazy-loaded, streamed, up to 49 MB)
- Edit your replies (also edits the message in Telegram), see when a user edits theirs
- Block / unblock users, profile card with avatar, pagination for huge chats

### 📢 Broadcast manager
- Send **text, HTML, image, video or file** to every user — as a background job (no frozen page)
- **History of every broadcast** with ✅ successful / ❌ failed counts, delivery rate, failure reasons per user
- **Broadcast again**, **Edit** (updates the message in every user's chat) and **Delete** (removes it from every chat)
- Users who blocked the bot are detected and skipped next time; Telegram flood-limits (429) are respected
- Works from the panel **and** from Telegram with `/broadcast` — both are tracked in the same history

### 🛠 Admin power from Telegram
`/send`, `/broadcast`, `/help` — answer customers and message everyone without opening the website
(see [commands](#-telegram-admin-commands)). Customers' **photos / videos / files are forwarded to your admin chat** too.

### ⚙️ Settings page (no redeploy needed)
Website name, browser title, tagline, logo emoji · **dark / light / auto theme** + accent colour presets · animations on/off ·
change **admin username & password** (stored hashed; other sessions are signed out) · auto-reply text & cool-down ·
notification toggles · anti-spam thresholds · pagination · broadcast speed.

### 🛡 Safety
Anti-spam (rate limit, duplicate detection, mute, auto-block) · login brute-force lock · CSRF-safe cookies ·
ID validation on every route · **nothing is ever hard-deleted from Firebase** · full **activity log**.

### 🎨 Polish
Animated gradient background, staggered sidebar, ripple buttons, animated counters, springy modals, skeleton loading,
Telegram-HTML live preview, fully responsive (great on phones), respects *reduced motion*.

## 🤖 Telegram admin commands
Only the chat IDs in `ADMIN_CHAT_ID` can use these. Everyone else is a normal customer.

| Command | What it does |
|---|---|
| `/help` | Shows how to use the bot |
| `/send <chat_id> <message>` | Sends the message to that user |
| `/send <chat_id>` | Then send an **image / video / file** (caption optional) and it is delivered to that user |
| `/broadcast <text>` | Sends the text to **all** users (shows a preview with **Send / Cancel** first) |
| `/broadcast` | Then send an **image / video / file** (caption optional) to broadcast it |
| `/cancel` | Aborts the current action |

The `chat_id` is in every new-message notification (tap to copy). HTML such as `<b>bold</b>` works in broadcasts.
When a broadcast finishes the bot tells you **how many were successful and how many failed**, and it appears in the panel's history.
The Send/Cancel confirmation can be switched off in *Settings → Bot behaviour*.

## 🚀 Quick start (5 minutes)
```bash
git clone https://github.com/YOUR_USERNAME/Dr.-Dev-Support.git
cd Dr.-Dev-Support
python3 -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # fill in your values
export $(grep -v '^#' .env | xargs)                   # or set them in your IDE / hosting dashboard
python app.py                                         # http://localhost:5000
```
1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token into `BOT_TOKEN`.
2. Get your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot) → `ADMIN_CHAT_ID`.
3. Set up Firebase (below) → `FIREBASE_URL` + `FIREBASE_SECRET`.
4. Open the panel, log in, then **change the default password in Settings → Security**.

## 🔧 Environment variables
| Variable | Required | Default | Description |
|---|:--:|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram bot token from @BotFather |
| `ADMIN_CHAT_ID` | ✅ | — | Your Telegram user ID (several IDs: `123,456`) |
| `FIREBASE_URL` | ✅ | — | `https://<project>-default-rtdb.firebaseio.com` |
| `FIREBASE_SECRET` | ✅ | — | Database secret (Project settings → Service accounts → Database secrets) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | ✅ | `admin` / `admin123` | First-run login — change it in **Settings** |
| `SECRET_KEY` | ✅ | random | Flask session key (Render can generate it) |
| `PANEL_URL` | ➖ | — | Public URL of the panel → adds an *Open in Admin Panel* button to notifications |
| `PANEL_NAME` | ➖ | `Support Panel` | Initial site name (editable in Settings) |
| `AUTO_REPLY`, `AUTO_REPLY_COOLDOWN_MINUTES` | ➖ | see `config.py` | Away-message; also editable in Settings |
| `MAX_UPLOAD_MB` | ➖ | `49` | Largest file you can send from the panel |
| `BROADCAST_DELAY` | ➖ | `0.05` | Seconds between users (≈20 msgs/s) |

Anything you save on the **Settings** page is stored in Firebase and overrides the environment default.

## 🔥 Firebase setup
1. [console.firebase.google.com](https://console.firebase.google.com) → *Create project* → *Build → Realtime Database → Create database*.
2. Copy the database URL → `FIREBASE_URL`.
3. ⚙️ Project settings → *Service accounts* → *Database secrets* → copy → `FIREBASE_SECRET`.
4. **Rules** tab → paste the content of [`database.rules.json`](database.rules.json) and *Publish*. It keeps the database private
   (only your server, using the secret, can read/write) and adds the indexes that make long chats and the log load instantly.

> Without the indexes everything still works — the app falls back to slower full reads and prints a hint in the logs.

## ☁️ Deploy for free on Render
1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo (`render.yaml` is included) — or *New → Web Service* with  
   build `pip install -r requirements.txt` and start `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`.
3. Fill in the environment variables, deploy, then set `PANEL_URL` to your `https://<name>.onrender.com` URL.
4. Keep it awake with a free [UptimeRobot](https://uptimerobot.com) monitor on `https://<name>.onrender.com/ping` (every 5 min).

> ⚠️ Keep **one worker** (`--workers 1`): the bot polls Telegram from a background thread and a few small caches live in memory.

## 🖥 Admin panel guide
| Page | What you do there |
|---|---|
| **Conversations** | Search users, filter unread/blocked, open a chat |
| **Chat** | Reply, attach files, react, edit, delete, search messages, block |
| **Broadcast** | Compose, attach media, watch progress, re-send / edit / delete old broadcasts |
| **Welcome Message** | Edit the `/start` greeting (Text or HTML, live preview, validated before saving) |
| **Activity Log** | Every message, login, block, broadcast and settings change |
| **Settings** | Branding, theme, password, bot behaviour, anti-spam, pagination |
| **Contact Developer** | Who built this and how to hire them |

## 🧠 How it works
```
Telegram user ─▶ Bot (pyTelegramBotAPI, polling thread) ─▶ Firebase RTDB ◀─ Flask admin panel (REST)
                      │                                         ▲
                      └─▶ your admin chat (notifications, media, /send, /broadcast)
```
- `support/<chat_id>/messages` — every message · `support/<chat_id>/meta` — profile, unread, blocked
- `chat_index/<chat_id>` — tiny mirror of the meta used for the fast chat list and broadcast recipients
- `broadcasts/<id>` · `broadcast_deliveries/<id>/<chat_id>` · `broadcast_failures/<id>/<chat_id>` — broadcast history
- `settings/site`, `settings/auth`, `settings/welcome` — runtime settings · `logs` — audit trail

**Soft delete:** deleting a message or a broadcast in the panel removes it from Telegram (when Telegram allows it) but only
sets `deleted: true` in Firebase. Nothing is ever removed from your database. Edits keep the previous text in `edit_history`.

**Telegram limits worth knowing:** bots can only react with Telegram's built-in reaction emoji (the panel offers exactly those),
and Telegram may refuse to delete very old messages — the panel tells you when that happens.

## 🔐 Security notes
- Change the default password on first login (the panel nags you until you do).
- Use HTTPS (Render does by default) and a long random `SECRET_KEY`.
- Keep `FIREBASE_SECRET` and `BOT_TOKEN` in environment variables only — never commit `.env`.

## ❓ FAQ
**How do I build a Telegram customer support bot with Python?** Clone this repo, add your bot token and Firebase keys, deploy to Render — the bot, database and admin panel are all included.

**Can I reply to customers from my phone?** Yes — the panel is fully responsive, and `/send <chat_id> message` works directly in Telegram.

**Does it work on Render's free plan?** Yes. Use one worker and an UptimeRobot ping on `/ping` so the service doesn't sleep.

**Can I broadcast an image or video to all users?** Yes — in the panel or with `/broadcast` in Telegram. The file is uploaded once and reused for every user.

**Can I undo a broadcast?** You can **delete** it (removed from users' chats) or **edit** it. The record stays in Firebase.

**Is anything stored forever?** Yes — by design nothing is hard-deleted, so you always have a complete audit trail.

## 👨‍💻 Author & custom projects
Made by **Dr. Dev || Dr. Hamza** (**@drdevhacks**) — I build Python, Android (Java), PHP, MySQL and JavaScript projects and
create coding & ethical-hacking content on YouTube, Instagram and Facebook.

**Need a custom project, or want an existing project modified?** Message me on Telegram 👉 **[t.me/doctordevsupport](https://t.me/doctordevsupport)**

⭐ If this project saved you time, please **star the repo** and share it — it really helps!

<!--
Suggested GitHub "About" description (≈160 chars):
Telegram customer support bot with a WhatsApp-style Flask admin panel, Firebase storage, broadcasts, reactions, anti-spam. Free Render deploy.

Suggested GitHub topics:
telegram-bot, customer-support, helpdesk, flask, firebase, firebase-realtime-database, python, admin-panel, broadcast, pytelegrambotapi,
whatsapp-style, live-chat, support-bot, render, open-source
-->
