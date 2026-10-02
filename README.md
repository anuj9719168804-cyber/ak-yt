# 🚀 YouTube Downloader Bot

> ⚡ A powerful Telegram bot that downloads YouTube videos & audio — send a link, pick a quality, get the file.

## ✨ Features

| Feature | Description |
|---|---|
| 📥 **Direct Downloads** | youtube.com / youtu.be / Shorts / music.youtube.com links |
| 🎬 **Quality Picker** | Only the qualities the video really has (144p → 4K) with approx. size |
| 🎵 **MP3 Audio** | 128k / 320k audio-only download |
| 📊 **Live Progress** | Download & upload progress bar with speed + ETA |
| 🔎 **Search** | `/search <name>` (or `/yts`), or just type a song/video name — paginated results, tap a number to pick quality |
| 📚 **Playlists** | Send a playlist link, pick a quality — every video is sent one by one (no limit) |
| 🛑 **/cancel** | Stop an active download any time |
| ✂️ **Auto split** | Files over 2 GB are cut into playable parts (no re-encode) and sent one after another |
| 📢 **Force-join** | Users must join your channels before using the bot (✅ Verify button, photo on the Access Denied screen). Add channels from `/admin` or via `FORCE_SUB` |
| 🧾 **Log channel** | `LOG_CHANNEL`: bot posts bot start, new users, finished downloads, failures, payment claims and 429 alerts |
| 👑 **Admin panel** | `/admin`: broadcast, stats, maintenance mode, force channels, users list, banner, CSV export |
| 📊 **Stats** | `/stats` per-user stats; About shows global users / downloads / uptime |
| ⏳ **Cooldown** | Per-user delay between links (default 2 s) |
| 💎 **Premium plans** | `/plans` (or 💎 Plans button): pay via UPI, tap "I've Paid", admin activates with `/addpremium`. Unlimited downloads, full playlists, priority queue |
| 🆓 **Free tier** | `DAILY_FREE_LIMIT` downloads per day and `PLAYLIST_FREE_MAX` videos per playlist; when the limit is hit the user gets the referral / upgrade prompt |
| 🎁 **Referrals** | `/referral`: 5 invites → 1 day premium, 10 invites → +1 day (stacks) |
| 🎯 **Priority queue** | When all download slots are busy, premium users go ahead of free users |
| 🔑 **PO-token provider** | Local bgutil server so yt-dlp's `web` client works → full 1080p / 2K / 4K ladder without cookies (`/potstatus` for admins) |
| 🍪 **Cookie manager** | Admin uploads `cookies.txt` in Telegram (`/cookies`), validated + installed atomically, or seeded from `COOKIES_CONTENT` |
| 🛑 **HTTP 429 cooldown** | A 429 is per server IP: the bot stops calling YouTube for a while, DMs the admins once, stops playlists early and never retries other clients on a block |
| 🖼 **Thumbnail fallback** | yt-dlp thumbnail → YouTube CDN (maxres → sd → hq) → frame grabbed from the video |
| ✨ **Premium emoji** | `premium_emoji.py` turns ✅ ❌ ⚡ 💎 👑 … into Telegram custom emoji in every message (`PREMIUM_EMOJI=0` to disable) |

## ⚙️ Setup

1. Get a bot token from `@BotFather`, and `API_ID` / `API_HASH` from `my.telegram.org`.
2. Fill in the settings — either `cp .env.example .env`, or write the values straight into the `os.getenv("NAME", "default")` lines at the top of `bot.py` (env still wins if both are set).
3. Install **ffmpeg** (and **Node.js 20+** for YouTube's JS challenge), then:

```bash
pip install -r requirements.txt
python bot.py
```

Or with Docker (ffmpeg and Node are included):

```bash
docker build -t ytbot .
docker run --env-file .env ytbot
```

## 🔐 Environment variables

| Variable | Required | Description |
|---|---|---|
| `API_ID` / `API_HASH` / `BOT_TOKEN` | ✅ | Telegram credentials |
| `OWNER_ID` | – | Gets a ping when someone hits /start. Also the admin if `ADMIN_ID` is empty |
| `ADMIN_ID` | – | Telegram ID allowed to use `/admin` (defaults to `OWNER_ID`) |
| `LOG_CHANNEL` | – | Channel/group (`-100…` ID or `@username`) where the bot logs new users, downloads, failures and payment claims. Add the bot as admin |
| `FORCE_SUB` | – | Comma-separated `@channel` / `t.me` links users must join. Bot must be admin there. Also manageable from `/admin` |
| `FORCE_SUB_PHOTO_URL` | – | Photo on the force-subscribe screen (direct URL or Telegram post link). Default `https://t.me/log_ak_bot/165` |
| `SEARCH_CHUNK_SIZE` / `SEARCH_PAGE_SIZE` / `SEARCH_MAX` | – | Results fetched per search call (30) / shown per page (10) / hard cap (100) |
| `COOLDOWN_SECONDS` | – | Delay between two links per user (default 2, `0` = off) |
| `DATA_FILE` | – | JSON file with users / channels / banner / maintenance (default `bot_data.json`) |
| `PORT` / `KEEP_ALIVE` | – | Starts a tiny health-check web server for Koyeb/Render. Hosts set `PORT` automatically; `KEEP_ALIVE=1` forces it |
| `START_PHOTO_URL` | – | Photo shown with the welcome message |
| `POWERED_BY` / `POWERED_BY_URL` | – | Credit line in welcome / help / captions |
| `MAX_HEIGHT` | – | Highest quality offered (default 2160 = 4K) |
| `MAX_CONCURRENT_DOWNLOADS` | – | Parallel downloads (default 3) |
| `MAX_FILE_SIZE_MB` | – | Size above which a file is split into parts (default 2000) |
| `PLAYLIST_MAX` | – | Max videos per playlist. `0` = unlimited (default) |
| `PLAYLIST_DEFAULT_QUALITY` | – | Default playlist quality, shown as a one-tap ⭐ Start button (default 720) |
| `PLAYLIST_AUTO_START` | – | `1` = skip the menu and start playlists instantly in the default quality (default 0) |
| `ADMINS` | – | Extra admin IDs (comma-separated). Admins are always Lifetime Premium |
| `DAILY_FREE_LIMIT` | – | Free downloads per UTC day (default 5, `0` = unlimited) |
| `PLAYLIST_FREE_MAX` | – | Free users: videos taken per playlist (default 3, `0` = unlimited) |
| `PLANS` | – | `price:days` list for /plans, `lifetime` = forever (default `19:12,29:21,45:35,99:99,999:lifetime`) |
| `UPI_ID` | – | UPI ID shown on the payment screen. Empty = "contact admin" |
| `PLANS_PHOTO_URL` / `REFERRAL_PHOTO_URL` | – | Optional image (URL or Telegram post link) for /plans and the referral prompt |
| `YT_COOKIES` | – | Optional custom cookie-file path (default: `cookies.txt` next to `DATA_FILE`) |
| `COOKIES_CONTENT` | – | Optional. Whole Netscape `cookies.txt` text; seeds the file only if no valid one exists yet |
| `YOUTUBE_COOLDOWN_MINUTES` | `30` | How long to pause YouTube requests after an HTTP 429 |
| `CONCURRENT_FRAGMENTS` | `10` | Parallel fragment downloads per video (faster DASH downloads, `1` = off) |
| `DL_RETRIES` | `2` | Extra attempts with backoff (1s, 2s…) on network errors; never retried on 429 / bot-check |

> ⚠️ Never commit your real `.env` or `cookies.txt`.

## ✏️ Changing the welcome / help / about text

Everything lives in `bot.py`: `build_welcome()`, `HELP_TEXT`, `build_about()` (About + live stats), `SUPPORT_TEXT`.
Welcome, Help and About use the 𝙗𝙤𝙡𝙙-𝙞𝙩𝙖𝙡𝙞𝙘 font and Telegram blockquotes (`<blockquote>`; Help's tips are `<blockquote expandable>`).
Use the `_bi("text")` helper for styled text; `{first_name}`, `POWERED_BY`, uptime and stats are filled in automatically.
Remember to escape `&` as `&amp;` (the messages are sent in HTML mode).

## 🧾 Log channel & force-subscribe setup

1. Create the channel(s) and add the bot as **admin**.
2. `LOG_CHANNEL` = `-100…` ID or `@username`. `FORCE_SUB` = `@channel1,@channel2`.
3. Restart. The bot posts "🟢 Bot started" in the log channel; force channels show up under `/admin`.

Private invite-link channels can't be auto-checked, and a `FORCE_SUB` channel removed in `/admin` returns on restart until it is removed from `FORCE_SUB` too.

## 👑 Admin panel

Set `OWNER_ID` (or `ADMIN_ID`), then send `/admin` (or tap 👑 Admin).

- **Add Channel** — send `@username` / `t.me/username` (bot must be admin there) or a private `t.me/+hash` link.
  Private invite-link channels can't be auto-checked; users are trusted when they tap ✅ Verify.
- **Broadcast** — send any text/photo/video/document/audio; it is copied to every user.
- **Set Banner** — send a photo or image URL; it replaces `START_PHOTO_URL` on /start.
- Adding/removing a channel or *Reset Verifications* makes everyone verify again.

> 💾 `bot_data.json` must live on a persistent disk/volume, otherwise users and channels reset on every redeploy.


## 💎 Premium — admin commands

| Command | What it does |
|---|---|
| `/addpremium <user_id> <days>` | Grants (or extends) premium. `lifetime` instead of days = forever |
| `/removepremium <user_id>` | Removes premium |
| `/potstatus` | Shows whether the PO-token server is up, or exactly which setup step failed |
| `/cookies` | Opens a 15-min window: send your `cookies.txt` as a File. Validated first — a bad file never replaces a working one. No restart needed |
| `/authstatus` | Cookie status (rows, login markers, expired rows), PO-token and 429 cooldown |
| `/authcheck` | One live YouTube request to verify the server can really fetch formats; clears the cooldown if it passes |
| `/clearcooldown` | Manually end the HTTP 429 cooldown |
| `/ytdlpupdate` | `pip install -U yt-dlp` (restart the bot afterwards) |

Premium data (expiry, daily counter, referrals) is stored in `bot_data.json` next to the users — keep it on a persistent volume.
When a user taps **I've Paid**, every admin gets a message with the ready-to-run `/addpremium` line.

## 🔑 PO-token provider (full quality ladder)

Docker: the Dockerfile builds the bgutil server into the image and `entrypoint.sh` starts it before the bot.
Without Docker: `pot_provider.py` clones + builds it into `.bgutil-pot/` on first boot if `git`, `node` and `npm` are on PATH.
If it can't start, the bot just keeps using its normal cookie-free client sets — nothing breaks.
