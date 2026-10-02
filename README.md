# 🚀 YouTube Downloader Bot

> ⚡ A powerful Telegram bot that downloads YouTube videos & audio — send a link (or just a song name), pick a quality, get the file.

Built with **Pyrogram (kurigram)** + **yt-dlp**. Fast, premium-ready, with force-subscribe, log channel, admin panel and referral system.

---

## 📑 Table of contents

1. [Features](#-features)
2. [Quick start](#-quick-start)
3. [Configuration](#-configuration)
4. [Force subscribe](#-force-subscribe)
5. [Log channel](#-log-channel)
6. [Premium & free tier](#-premium--free-tier)
7. [Admin panel & commands](#-admin-panel--commands)
8. [Customising texts](#-customising-texts)
9. [YouTube auth: cookies, PO-token, 429](#-youtube-auth-cookies-po-token-429)
10. [Deployment](#-deployment)
11. [Project structure](#-project-structure)
12. [Troubleshooting](#-troubleshooting)

---

## ✨ Features

### 📥 Downloading

| Feature | Description |
|---|---|
| 📥 **Direct downloads** | `youtube.com`, `youtu.be`, Shorts, `music.youtube.com` links |
| 🎬 **Quality picker** | Only the qualities the video really has (144p → 4K) with approx. size |
| 🎵 **MP3 audio** | 128k / 320k audio-only download |
| 📊 **Live progress** | Download & upload progress bar with speed + ETA |
| 🔎 **Search** | `/search <name>` (or `/yts`), or just type a song/video name — paginated results, tap a number to pick quality |
| 📚 **Playlists** | Send a playlist link, pick a quality — every video is sent one by one |
| ✂️ **Auto split** | Files over 2 GB are cut into playable parts (no re-encode) and sent one after another |
| 🛑 **/cancel** | Stop an active download any time |
| 🖼 **Thumbnail fallback** | yt-dlp thumbnail → YouTube CDN (maxres → sd → hq) → frame grabbed from the video |
| ⏳ **Cooldown** | Per-user delay between links (default 2 s) |

### 💎 Business / growth

| Feature | Description |
|---|---|
| 💎 **Premium plans** | `/plans`: pay via UPI, tap "I've Paid", admin activates with `/addpremium` |
| 🆓 **Free tier** | Daily download limit + playlist cap for free users |
| 🎁 **Referrals** | `/referral`: 5 invites → 1 day premium, 10 invites → +1 day (stacks) |
| 🎯 **Priority queue** | When all download slots are busy, premium users go first |

### 🛡 Control

| Feature | Description |
|---|---|
| 📢 **Force subscribe** | Users must join your channels first (✅ Verify button, photo on the Access Denied screen). Accepts `@username`, `t.me` link **or** `-100…` ID |
| 🧾 **Log channel** | Bot start, new users, finished downloads, failures, payment claims, 429 alerts |
| 👑 **Admin panel** | `/admin`: broadcast, stats, maintenance mode, force channels, users list, banner, CSV export |
| 📊 **Stats** | `/stats` per-user stats; About shows global users / downloads / uptime |

### 🔧 Reliability

| Feature | Description |
|---|---|
| 🔑 **PO-token provider** | Local bgutil server so yt-dlp's `web` client works → full 1080p / 2K / 4K ladder without cookies |
| 🍪 **Cookie manager** | Upload `cookies.txt` in Telegram (`/cookies`), validated + installed atomically |
| 🛑 **HTTP 429 cooldown** | A 429 is per server IP: bot pauses YouTube calls, DMs admins once, stops playlists early |
| ✨ **Premium emoji** | `premium_emoji.py` turns ✅ ❌ ⚡ 💎 👑 … into Telegram custom emoji (`PREMIUM_EMOJI=0` to disable) |

---

## 🚀 Quick start

1. Get a bot token from [@BotFather](https://t.me/BotFather), and `API_ID` / `API_HASH` from [my.telegram.org](https://my.telegram.org).
2. Put your values in `.env` (copy `.env.example`) **or** write them straight into the `os.getenv("NAME", "default")` lines at the top of `bot.py`. If both are set, the environment variable wins.
3. Install **ffmpeg** and **Node.js 20+** (needed for YouTube's JS challenge), then:

```bash
pip install -r requirements.txt
python bot.py
```

Docker (ffmpeg and Node are already included):

```bash
docker build -t ytbot .
docker run --env-file .env ytbot
```

> ⚠️ Never commit or share your real `BOT_TOKEN`, `API_HASH`, `.env` or `cookies.txt`. If a token ever leaks, revoke it in @BotFather (`/revoke`) and use the new one.

---

## 🔐 Configuration

All settings are read from environment variables. Each one also has a default in `bot.py`, so you can hardcode values there instead.

### Required

| Variable | Description |
|---|---|
| `API_ID` / `API_HASH` | Telegram API credentials |
| `BOT_TOKEN` | Token from @BotFather |

### Admin & channels

| Variable | Description |
|---|---|
| `OWNER_ID` | Your Telegram user ID. Gets a ping on /start; also the admin if `ADMIN_ID` is empty |
| `ADMIN_ID` | ID allowed to use `/admin` (defaults to `OWNER_ID`) |
| `ADMINS` | Extra admin IDs, comma-separated. Admins are always Lifetime Premium |
| `LOG_CHANNEL` | Log channel: `-100…` ID or `@username` or a `t.me/` link — public or private invite (bot must be admin) |
| `FORCE_SUB` | Force-subscribe channels, comma-separated: `@username`, `t.me/…` link or `-100…` ID |
| `FORCE_SUB_PHOTO_URL` | Photo on the force-subscribe screen (URL or Telegram post link). Default `https://t.me/log_ak_bot/165` |

### Look & feel

| Variable | Description |
|---|---|
| `START_PHOTO_URL` | Photo shown with the welcome message (URL or Telegram post link) |
| `PLANS_PHOTO_URL` / `REFERRAL_PHOTO_URL` | Optional images for /plans and the referral prompt |
| `POWERED_BY` / `POWERED_BY_URL` | Credit line used in welcome / about / captions |
| `PREMIUM_EMOJI` / `BUTTON_ICONS` | `1` = on (default), `0` = off |

### Downloads

| Variable | Default | Description |
|---|---|---|
| `MAX_HEIGHT` | `2160` | Highest quality offered (2160 = 4K) |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | Parallel downloads |
| `MAX_FILE_SIZE_MB` | `2000` | Size above which a file is split |
| `PLAYLIST_MAX` | `0` | Max videos per playlist (`0` = unlimited) |
| `PLAYLIST_DEFAULT_QUALITY` | `720` | Quality of the one-tap ⭐ Start button |
| `PLAYLIST_AUTO_START` | `0` | `1` = skip the menu, start playlists instantly |
| `COOLDOWN_SECONDS` | `2` | Delay between two links per user (`0` = off) |
| `CONCURRENT_FRAGMENTS` | `10` | Parallel fragments per video (`1` = off) |
| `DL_RETRIES` | `2` | Extra retries on network errors (never on 429) |
| `DL_STALL_TIMEOUT` | `120` | Seconds without new bytes before a download counts as stalled |
| `DL_HARD_TIMEOUT_MIN` | `120` | Max minutes for one video download |
| `UPLOAD_TIMEOUT_MIN` | `60` | Max minutes for one upload |
| `SEARCH_CHUNK_SIZE` / `SEARCH_PAGE_SIZE` / `SEARCH_MAX` | `30` / `10` / `100` | Search results per call / per page / hard cap |

### Premium

| Variable | Default | Description |
|---|---|---|
| `DAILY_FREE_LIMIT` | `5` | Free downloads per UTC day (`0` = unlimited) |
| `PLAYLIST_FREE_MAX` | `3` | Free users: videos per playlist (`0` = unlimited) |
| `PLANS` | `19:12,29:21,45:35,99:99,999:lifetime` | `price:days` list; `lifetime` = forever |
| `UPI_ID` | – | UPI ID on the payment screen. Empty = "contact admin" |

### YouTube / hosting

| Variable | Default | Description |
|---|---|---|
| `YT_COOKIES` | – | Custom cookie-file path (default: `cookies.txt` next to `DATA_FILE`) |
| `COOKIES_CONTENT` | – | Whole Netscape `cookies.txt` text; seeds the file if no valid one exists |
| `YOUTUBE_COOLDOWN_MINUTES` | `30` | Pause on YouTube requests after an HTTP 429 |
| `DATA_FILE` | `bot_data.json` | Users / channels / banner / premium data |
| `DOWNLOAD_DIR` | `downloads` | Temp download folder |
| `PORT` / `KEEP_ALIVE` | – | Tiny health-check web server for Koyeb/Render. Hosts set `PORT`; `KEEP_ALIVE=1` forces it |

---

## 📢 Force subscribe

Users must join your channel(s) before they can use the bot. Until then they see an **Access Denied** screen with the force-subscribe photo, a Join button per channel and a **✅ Verify Join** button.

### Accepted formats

| You enter | Real join check? |
|---|---|
| `@username` or `https://t.me/username` | ✅ Yes |
| `-1003951808679` (channel ID) | ✅ Yes (bot builds the invite link itself) |
| `https://t.me/+AbCdEf` (private invite link) | ❌ No — Telegram gives the bot no channel ID, so users are trusted when they tap Verify |

For a private channel with a real check, use its `-100…` ID instead of the invite link.

### Setup

1. Add the bot to each channel as **admin**. For ID-based channels also grant **Invite Users**, otherwise the Join button can't be created.
2. Add the channels in either place (you can mix both):

```python
# env / bot.py default — comma separated
FORCE_SUB=@channel1,-1003951808679,@channel2
```

or send `/admin` → **Add Channel** and paste a username, link or ID.

3. Restart. Added channels show up under `/admin` → Channel List.

### Notes

- Adding/removing a channel or tapping *Reset Verifications* makes everybody verify again.
- A channel from `FORCE_SUB` that you remove in `/admin` comes back on the next restart until you remove it from `FORCE_SUB` too.
- If the join check fails (bot not admin, channel not found) the user is treated as **not joined** — check the bot logs.

---

## 🧾 Log channel

Set `LOG_CHANNEL` (`-100…` ID or `@username`) and add the bot as admin. The bot then posts:

| Event | Message |
|---|---|
| Bot start | 🟢 Bot started |
| New user | 🆕 name, username, ID, total users |
| Download finished | 📥 user, title, quality, size, link |
| Failure | ❌ Download failed / File too big, with error text |
| Payment claim | 🔔 user + plan |
| YouTube 429 | 🛑 cooldown alert |

**Private invite link (`t.me/+…`):** Telegram doesn't let a bot turn an invite link into a channel ID, so the bot learns it itself: make the bot admin in that channel (auto-binds if the channel's link matches, otherwise admins get a *Yes, this is the log channel* button), or post `/setlog` inside the channel. The ID is saved in the DB (MongoDB / `DATA_FILE`) and restored on restart.

If `LOG_CHANNEL` is empty nothing is logged. If a post fails, the bot keeps running and only writes a warning to its own log.

---

## 💎 Premium & free tier

- **Free users:** `DAILY_FREE_LIMIT` downloads per day, `PLAYLIST_FREE_MAX` videos per playlist. When the limit is hit they get the referral / upgrade prompt.
- **Premium:** unlimited downloads, full playlists, priority queue.
- **Buying:** `/plans` → pick a plan → pay to `UPI_ID` → tap **I've Paid**. Every admin (and the log channel) gets a message with a ready-to-run `/addpremium` line.
- **Referrals:** `/referral` gives a personal link; 5 invites = 1 day premium, 10 invites = +1 day (stacks).

Default plans:

| Price | Validity |
|---|---|
| ₹19 | 12 days |
| ₹29 | 21 days |
| ₹45 | 35 days |
| ₹99 | 99 days |
| ₹999 | Lifetime |

Change them with `PLANS`, e.g. `PLANS=49:30,199:lifetime`.

---

## 👑 Admin panel & commands

Set `OWNER_ID` (or `ADMIN_ID`), then send `/admin` or tap **👑 Admin**.

**Panel**

- **Add / Remove / List Channel** — manage force-subscribe channels.
- **Broadcast** — send any text/photo/video/document/audio; it is copied to every user.
- **Set Banner** — send a photo or image URL; it replaces `START_PHOTO_URL` on /start.
- **Maintenance mode**, **users list**, **CSV export**, **Reset Verifications**.

**Commands**

| Command | What it does |
|---|---|
| `/addpremium <user_id> <days>` | Grants or extends premium. `lifetime` instead of days = forever |
| `/removepremium <user_id>` | Removes premium |
| `/cookies` | Opens a 15-min window: send your `cookies.txt` as a file. Validated first — a bad file never replaces a working one |
| `/authstatus` | Cookie status, PO-token and 429 cooldown |
| `/authcheck` | One live YouTube request to verify formats can be fetched; clears the cooldown if it passes |
| `/clearcooldown` | Manually end the 429 cooldown |
| `/potstatus` | Shows if the PO-token server is up, or which step failed |
| `/ytdlpupdate` | `pip install -U yt-dlp` (restart the bot afterwards) |

**User commands:** `/start`, `/help`, `/search`, `/cancel`, `/stats`, `/plans`, `/myplan`, `/referral`.

> 💾 `bot_data.json` must live on a persistent disk/volume, otherwise users, channels, premium and referrals reset on every redeploy.

---

## ✏️ Customising texts

Everything lives in `bot.py`:

| Text | Where |
|---|---|
| Welcome | `build_welcome()` |
| Help | `HELP_TEXT` |
| About (+ live stats) | `build_about()` |
| Support | `SUPPORT_TEXT` |
| Access Denied | `ACCESS_DENIED_TEXT` |
| Plans | `plans_text()` |

Welcome, Help and About use the 𝙗𝙤𝙡𝙙-𝙞𝙩𝙖𝙡𝙞𝙘 font and Telegram blockquotes:

- Use `_bi("text")` to convert text to the bold-italic font.
- `<blockquote>…</blockquote>` for a quote box; `<blockquote expandable>` for a collapsible one (used for Help's tips).
- `{first_name}`, `POWERED_BY`, quality range, users/downloads/uptime are filled in automatically.
- Messages are sent in **HTML mode**: write `&` as `&amp;`, and don't use raw `<` or `>` in text.

Other texts still use `SC()`, which converts plain text to small-caps and leaves HTML tags, `<code>` blocks and @mentions alone.

---

## 🔑 YouTube auth: cookies, PO-token, 429

- **PO-token provider:** Docker builds the bgutil server into the image and `entrypoint.sh` starts it. Without Docker, `pot_provider.py` clones + builds it into `.bgutil-pot/` on first boot if `git`, `node` and `npm` are on PATH. If it can't start, the bot keeps using its normal cookie-free clients — nothing breaks.
- **Cookies:** if YouTube shows "Sign in to confirm you're not a bot", send `/cookies` and upload a fresh `cookies.txt`, or set `COOKIES_CONTENT`.
- **HTTP 429:** the server IP is rate-limited. The bot pauses YouTube requests for `YOUTUBE_COOLDOWN_MINUTES`, tells the admins once and never retries other clients on a block. Use `/authcheck` to test, `/clearcooldown` to reset.

---

## ☁️ Deployment

- **Docker / VPS:** use the included `Dockerfile` (ffmpeg + Node.js included) and mount a persistent volume for `DATA_FILE` and the cookies.
- **Koyeb / Render / Railway:** set the environment variables in the dashboard. `PORT` is set by the host and starts the health-check server automatically (`KEEP_ALIVE=1` forces it). Attach a persistent volume, or users/premium reset on redeploy.
- **Cookies on hosts without file upload:** paste the whole text into `COOKIES_CONTENT`.

---

## 🗂 Project structure

```
├── bot.py               # the bot: handlers, texts, downloads, admin, premium
├── premium_emoji.py     # custom-emoji patch for every outgoing HTML message
├── yt_auth.py           # cookies, 429 cooldown, auth checks
├── pot_provider.py      # bgutil PO-token server bootstrap
├── resolve_emoji_ids.py # helper to look up custom emoji IDs
├── entrypoint.sh        # starts PO-token server, then the bot
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## 🩺 Troubleshooting

| Problem | Fix |
|---|---|
| Force subscribe never lets users in | Bot must be **admin** in the channel; check the logs for "membership check failed" |
| Join button missing for an ID channel | Give the bot the **Invite Users** permission, restart |
| Log channel is empty | Bot must be admin there; check the logs for "log channel send failed" / "LOG_CHANNEL … par message nahi gaya" |
| "Sign in to confirm you're not a bot" | Upload fresh cookies with `/cookies`; run `/authcheck` |
| HTTP 429 messages | Server IP is rate-limited — wait for the cooldown, use cookies/proxy, `/authcheck` |
| Only low qualities offered | Check `/potstatus`; make sure Node.js 20+ is installed |
| Welcome photo not shown | Bot must be able to read that Telegram post; otherwise use a direct image URL |
| Users/premium reset after redeploy | Put `DATA_FILE` on a persistent volume |
| Message fails with a parse error | Escape `&` as `&amp;` and close all HTML tags in the text |

---

<p align="center">👑 Powered by <a href="https://t.me/anujedits76">Anuj Kumar</a> — ⚡ Speed • Performance • Reliability</p>
