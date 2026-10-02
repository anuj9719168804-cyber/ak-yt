"""
YouTube Downloader Bot  —  Telegram bot (Pyrogram/kurigram + yt-dlp)

Send a YouTube link  ->  pick a quality (or MP3)  ->  get the file in chat.
Welcome / help / about texts follow the same small-caps + emoji style as the
original Faphouse bot.
"""
import asyncio
import csv
import glob
import html
import io
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import pot_provider
import premium_emoji
import yt_auth
import yt_dlp
from dotenv import load_dotenv
from pyrogram import Client, StopPropagation, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, InputUserDeactivated, MessageNotModified, PeerIdInvalid, UserIsBlocked, UserNotParticipant
from pyrogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)

try:  # coloured inline buttons — only on newer kurigram builds
    from pyrogram.enums import ButtonStyle
    BUTTON_STYLE_SUPPORTED = True
except Exception:  # pragma: no cover
    BUTTON_STYLE_SUPPORTED = False

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("ytbot")

# ---------------------------------------------------------------------
# Config (everything comes from env — nothing secret is hardcoded)
# ---------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "20432885") or 0)
API_HASH = os.getenv("API_HASH", "4fdcfab1c7f5e24ae69f3ce6bb234dec")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8609525656:AAHYKR952QdEUriHFzfei9gKoX79AoqtMLQ")
OWNER_ID = int(os.getenv("OWNER_ID", "8729304171") or 0)
# Admin panel owner (ported from dl.py). Falls back to OWNER_ID.
ADMIN_ID = int(os.getenv("ADMIN_ID", "8729304171") or 0) or OWNER_ID


def _parse_chat_ref(raw: str):
    """'-1001234567890' -> int, '@name' / 't.me/name' -> '@name', else None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    m = re.search(r"(?:t|telegram)\.me/([A-Za-z]\w{3,31})/?$", raw)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@?[A-Za-z]\w{3,31}", raw):
        return "@" + raw.lstrip("@")
    return None


# ---- LOG CHANNEL: yahan apna log channel ID / @username daalo (bot ko us channel mein admin banao) ----
# Example: "-1001234567890" ya "@mylogchannel"
LOG_CHANNEL = _parse_chat_ref(os.getenv("LOG_CHANNEL", "-1003951808679"))
# ---- FORCE SUBSCRIBE: comma-separated @username / t.me links (bot ko un channels mein admin banao) ----
# Example: "@channel1,@channel2,-1001234567890"   (@username, t.me link ya -100… ID; /admin se bhi add/remove hota hai)
FORCE_SUB_RAW = os.getenv("FORCE_SUB", "-1003951808679")
# Force-subscribe screen ki photo (direct URL ya Telegram post link)
FORCE_SUB_PHOTO_URL = os.getenv("FORCE_SUB_PHOTO_URL", "https://t.me/log_ak_bot/165").strip()

# Direct image URL OR a Telegram post link (https://t.me/channel/123)
START_PHOTO_URL = os.getenv("START_PHOTO_URL", "https://t.me/log_ak_bot/163")
POWERED_BY = os.getenv("POWERED_BY", "Anuj Kumar")
POWERED_BY_URL = os.getenv("POWERED_BY_URL", "https://t.me/anujedits76")

MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "2160"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "2000")) * 1024 * 1024
# Target size per part when a file is over MAX_FILE_SIZE. Kept ~7% below the limit:
# ffmpeg cuts by time using the AVERAGE bitrate, so a part can land a bit above
# its estimate on a high-bitrate stretch.
SPLIT_PART_TARGET = int(MAX_FILE_SIZE * 0.93)
# Cookies are handled by yt_auth.py (YT_COOKIES / COOKIES_CONTENT / admin /cookies upload).
# 0 = unlimited playlist size (default). Set a number to cap videos per playlist.
PLAYLIST_MAX = int(os.getenv("PLAYLIST_MAX", "0") or 0)
# Default quality for playlists (one-tap "Start" button on the menu).
PLAYLIST_DEFAULT_QUALITY = int(os.getenv("PLAYLIST_DEFAULT_QUALITY", "720") or 720)
# 1 = skip the menu and start downloading right away in the default quality.
PLAYLIST_AUTO_START = os.getenv("PLAYLIST_AUTO_START", "0") == "1"
# Watchdogs (ported from fbot): one stuck video must never freeze a whole playlist.
# Download: no new bytes for this many seconds -> skip the video.
DL_STALL_TIMEOUT = int(os.getenv("DL_STALL_TIMEOUT", "120") or 120)
# Download: absolute max minutes for a single video.
DL_HARD_TIMEOUT = int(os.getenv("DL_HARD_TIMEOUT_MIN", "120") or 120) * 60
# Upload: absolute max minutes for one part/file.
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT_MIN", "60") or 60) * 60
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
# Speed: DASH fragments fetched in parallel per video (ported from Yt_Downloder). 1 = off.
CONCURRENT_FRAGMENTS = max(1, int(os.getenv("CONCURRENT_FRAGMENTS", "10") or 10))
# Extra attempts for a download that fails on a flaky network (waits 1s, 2s, 4s...). 0 = off.
DL_RETRIES = max(0, int(os.getenv("DL_RETRIES", "2") or 0))
# Seconds a user must wait between two link messages (0 = off)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2") or 0)
# JSON file for users / force-join channels / banner / maintenance flag
DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")
# MongoDB (optional). If MONGO_URI is set, all bot data lives in MongoDB; otherwise the JSON file is used.
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Anujedit:Anujedit@cluster0.7cs2nhd.mongodb.net/?appName=Cluster0").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "ytbot")
# Auto-delete sent video/audio from the user's chat after N seconds (0 = off). Cache is NOT affected.
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "3600") or 0)
# Photo (Telegram post link or image URL) sent with the "file deleted" notice
AUTO_DELETE_PHOTO = os.getenv("AUTO_DELETE_PHOTO", "https://t.me/log_ak_bot/167")
# YouTube search (ported from fbot): results fetched per yt-dlp call / shown per page / hard cap
SEARCH_CHUNK_SIZE = int(os.getenv("SEARCH_CHUNK_SIZE", "30") or 30)
SEARCH_PAGE_SIZE = int(os.getenv("SEARCH_PAGE_SIZE", "10") or 10)
SEARCH_MAX = int(os.getenv("SEARCH_MAX", "100") or 100)

# ---- Premium / free-tier limits (ported from fbot) ----
# Extra admins besides ADMIN_ID / OWNER_ID: comma-separated Telegram IDs. Admins are
# always treated as Lifetime Premium.
ADMIN_IDS = {
    i for i in ({ADMIN_ID, OWNER_ID} | {int(x) for x in re.findall(r"\d+", os.getenv("ADMINS", "8729304171"))}) if i
}
# Free users: downloads per UTC day (0 = unlimited). Premium is never limited.
DAILY_FREE_LIMIT = int(os.getenv("DAILY_FREE_LIMIT", "5") or 0)
# Free users: max videos taken from one playlist (0 = unlimited). Premium gets everything.
PLAYLIST_FREE_MAX = int(os.getenv("PLAYLIST_FREE_MAX", "3") or 0)
# Plans shown by /plans, as "price:days" pairs; days = lifetime for a forever plan.
PLANS_RAW = os.getenv("PLANS", "19:12,29:21,45:35,99:99,999:lifetime")
UPI_ID = os.getenv("UPI_ID", "971916880@ybl").strip()
# Telegram post holding the payment QR image; shown as a clickable "Scan to Pay" link
QR_CODE_URL = os.getenv("QR_CODE_URL", "https://t.me/log_ak_bot/166").strip()
# Optional images (URL or Telegram post link) shown with /plans and the referral prompt
PLANS_PHOTO_URL = os.getenv("PLANS_PHOTO_URL", "https://t.me/log_ak_bot/164").strip()
REFERRAL_PHOTO_URL = os.getenv("REFERRAL_PHOTO_URL", "https://t.me/log_ak_bot/162").strip()
# (referrals needed, premium days granted when that count is first reached). Rewards stack.
REFERRAL_REWARDS = [(5, 1), (10, 1)]
REFERRAL_GOAL = REFERRAL_REWARDS[-1][0]


def _parse_plans(raw: str) -> list:
    out = []
    for part in (raw or "").split(","):
        if ":" not in part:
            continue
        price, days = (x.strip().lower() for x in part.split(":", 1))
        try:
            price = int(price)
        except ValueError:
            continue
        if days in ("0", "life", "lifetime", "forever"):
            out.append((price, None))
        elif days.isdigit() and int(days) > 0:
            out.append((price, int(days)))
    return out


PLANS = _parse_plans(PLANS_RAW)

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("API_ID, API_HASH and BOT_TOKEN must be set (see .env.example).")

# ---------------------------------------------------------------------
# Small-caps helpers (same as the original bot)
# ---------------------------------------------------------------------
_SMALLCAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)
_TAG_OR_MENTION_RE = re.compile(r"(<[^>]+>|@[A-Za-z][A-Za-z0-9_]{3,31})")


def smallcaps(text: str) -> str:
    return text.translate(_SMALLCAPS_MAP)


def SC(text):
    """Small-caps all plain text, but leave HTML tags, <code>…</code> contents
    and @mentions untouched (so links/commands stay copy-pasteable)."""
    if not isinstance(text, str):
        return text
    out, in_code = [], 0
    for part in _TAG_OR_MENTION_RE.split(text):
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith("<code"):
                in_code += 1
            elif low.startswith("</code"):
                in_code = max(0, in_code - 1)
            out.append(part)
        elif part.startswith("@"):
            out.append(part)
        else:
            out.append(part if in_code else part.translate(_SMALLCAPS_MAP))
    return "".join(out)


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def hms(seconds) -> str:
    seconds = max(0, int(round(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bar(done, total, width=12) -> str:
    pct = 0 if not total else min(1.0, done / total)
    filled = int(width * pct)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct * 100:.0f}%"


def human_speed(n) -> str:
    n = float(n or 0)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB/s"


def human_time(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def hex_bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(width * pct / 100))
    return "⬢" * filled + "⬡" * (width - filled)


def progress_text(prefix: str, is_download: bool, title: str, label: str, current, total,
                  speed, elapsed, eta, tag: str = "") -> str:
    """fbot-style progress card (⬢⬡ bar, ╭━❰Progress❱━➣ box)."""
    pct = (current / total * 100) if total else 0
    emoji = "📥" if is_download else "📤"
    verb = "Downloading" if is_download else "Uploading"
    word = "download" if is_download else "upload"
    return prefix + SC(
        f"{emoji} <b>Fast {verb}{tag} via Main Engine</b>\n\n"
        "╭━━━━❰Progress❱━➣\n"
        f"┣⪼ 🎬 File: <code>{esc(title[:60])}</code>\n"
        f"┣⪼ 🎞 Quality: {label}\n"
        f"┣⪼ [{hex_bar(pct)}]\n"
        f"┣⪼ ✅ {pct:.1f}%\n"
        f"┣⪼ 💾 {human_size(current)} / {human_size(total) if total else '?'}\n"
        f"┣⪼ ⚡ {human_speed(speed)}\n"
        f"┣⪼ 🕐 Elapsed: {human_time(elapsed)}\n"
        f"┣⪼ ⏳ ETA: {human_time(eta)}\n"
        "╰━━━━━━━━━━━━━━━➣\n\n"
        f"⚡ Hyper {word} connections active"
    )


def _auto_delete_note() -> str:
    n = AUTO_DELETE_SECONDS
    t = f"{n // 3600} hour" + ("s" if n // 3600 > 1 else "") if n >= 3600 and n % 3600 == 0 else (
        f"{max(1, n // 60)} min" if n >= 60 else f"{n} sec")
    return f"⏳ {smallcaps('This file will be deleted in')} {smallcaps(t)} — {smallcaps('save it now!')}"


def build_caption(title, name, size_bytes, quality_label, duration, dl_seconds, ul_seconds,
                  user_id, user_name, username, source_url, info, cache_hit=False) -> str:
    """fbot-style caption: bold title + blockquote details + Powered by."""
    info = info or {}
    powered = f'<a href="{POWERED_BY_URL}">{smallcaps(POWERED_BY)}</a>' if POWERED_BY_URL else smallcaps(POWERED_BY)
    if user_name:
        by = f'<a href="tg://user?id={user_id}">{smallcaps(user_name)}</a>'
    elif username:
        by = f'<a href="https://t.me/{username}">{smallcaps(username)}</a>'
    else:
        by = str(user_id)
    author = info.get("uploader") or info.get("channel")
    author_url = info.get("uploader_url") or info.get("channel_url")
    author_line = ""
    if author:
        a = f'<a href="{esc(author_url)}">{smallcaps(author)}</a>' if author_url else smallcaps(author)
        author_line = f"👤 {smallcaps('By')}: {a}\n"
    stats = ""
    if info.get("view_count"):
        stats += f"👁️ {smallcaps('Views')}: {info['view_count']:,}\n"
    if info.get("like_count"):
        stats += f"👍 {smallcaps('Likes')}: {info['like_count']:,}\n"
    if info.get("comment_count"):
        stats += f"💬 {smallcaps('Comments')}: {info['comment_count']:,}\n"
    cats = info.get("categories") or []
    if cats:
        stats += f"🏷️ {smallcaps('Category')}: {smallcaps(', '.join(cats[:2]))}\n"
    ud = str(info.get("upload_date") or "")
    if len(ud) == 8 and ud.isdigit():
        stats += f"📅 {smallcaps('Uploaded on')}: {ud[:4]}-{ud[4:6]}-{ud[6:]}\n"
    src = f'<a href="{esc(source_url)}">{smallcaps("YouTube Link")}</a>' if source_url else smallcaps("YouTube Link")
    return (
        f"🎬 <b>{smallcaps(title)}</b>\n\n"
        "<blockquote>"
        f"📄 {smallcaps('File Name')}: {smallcaps(name)}\n"
        f"{author_line}"
        f"📦 {smallcaps('Size')}: {human_size(size_bytes)}\n"
        f"🎞️ {smallcaps('Quality')}: {smallcaps(quality_label)}\n"
        f"⏱️ {smallcaps('Duration')}: {hms(duration) if duration else smallcaps('Unknown')}\n"
        f"{stats}"
        + (f"⚡ {smallcaps('Sent from')}: {smallcaps('Cache (instant)')}\n" if cache_hit else
           f"⬇️ {smallcaps('Downloaded in')}: {hms(dl_seconds)} sec\n"
           f"⬆️ {smallcaps('Uploaded in')}: {hms(ul_seconds)} sec\n") +
        f"🙋 {smallcaps('Downloaded by')}: {by}\n"
        f"🔗 {smallcaps('Source')}: {src}\n"
        "</blockquote>\n\n"
        + (f"{_auto_delete_note()}\n\n" if AUTO_DELETE_SECONDS > 0 else "") +
        f"⚡ {smallcaps('Powered by')} {powered}"
    )


# ---------------------------------------------------------------------
# Persistent data: users, stats, force-join channels, banner, maintenance
# (ported from dl.py, stored as JSON in DATA_FILE)
# ---------------------------------------------------------------------
BOT_START_TIME = datetime.now()
_DB_LOCK = threading.Lock()
_last_save = [0.0]


_MONGO = None
if MONGO_URI:
    try:
        from mongo_store import MongoStore
        _MONGO = MongoStore(MONGO_URI, MONGO_DB_NAME)
        logger.info("MongoDB connected — using it for persistent data")
    except Exception as e:
        logger.error(f"MongoDB connection failed ({e}); falling back to {DATA_FILE}")
        _MONGO = None


def _load_data() -> dict:
    data = {
        "users": {}, "total_downloads": 0, "force_channels": [],
        "banner_url": None, "banner_file_id": None, "maintenance_mode": False,
        "backup_channels": [], "banned": [], "file_cache": {},
    }
    if _MONGO is not None:
        try:
            first_run = _MONGO.is_empty()
            if not first_run:
                return _MONGO.load(data)
        except Exception as e:
            logger.error(f"Error loading from MongoDB: {e}")
            first_run = False
        # first run with Mongo: fall through, import the old JSON file (if any), then save to Mongo
    else:
        first_run = False
    if os.path.isfile(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception as e:
            logger.error(f"Error loading {DATA_FILE}: {e}")
    if _MONGO is not None and first_run:
        try:
            _MONGO.save(data)
            logger.info("Imported existing JSON data into MongoDB")
        except Exception as e:
            logger.error(f"JSON -> MongoDB import failed: {e}")
    return data


DB = _load_data()
FORCE_CHANNELS: list = DB["force_channels"]  # mutate in place, never reassign
FILE_CACHE: dict = DB.setdefault("file_cache", {})  # "<video_id>::<v720|a320>" -> sent-file record; mutate in place
FILE_CACHE_MAX = int(os.getenv("FILE_CACHE_MAX", "5000") or 5000)
# Optional private channel holding one copy of every cached file. Re-sending via copy_message from it
# is the most reliable way to give *any* user a valid file; without it the raw file_id is used.
_cc = re.findall(r"-100\d+", os.getenv("CACHE_CHANNEL_ID", ""))
CACHE_CHANNEL_ID = int(_cc[0]) if _cc else None
BANNED: list = DB.setdefault("banned", [])  # user ids banned via /ban; mutate in place
BACKUP_CHANNELS: list = DB.setdefault("backup_channels", [])  # linked via /set_channel_id; mutate in place
# Optional fixed backup channels from env (comma/space separated -100... ids)
BACKUP_CHANNEL_IDS = [int(x) for x in re.findall(r"-100\d+", os.getenv("BACKUP_CHANNEL_IDS", ""))]


def save_data(force: bool = True):
    """Atomic write. force=False throttles to once per 30 s (used for last_active)."""
    if not force and time.time() - _last_save[0] < 30:
        return
    with _DB_LOCK:
        if _MONGO is not None:
            try:
                _MONGO.save(DB)
                _last_save[0] = time.time()
            except Exception as e:
                logger.error(f"Error saving to MongoDB: {e}")
            return
        try:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(DB, f, indent=2, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
            _last_save[0] = time.time()
        except Exception as e:
            logger.error(f"Error saving {DATA_FILE}: {e}")


PENDING_DELETES: list = DB.setdefault("pending_deletes", [])  # [{"chat":..,"msg":..,"at":ts}]; mutate in place


def _bi(text: str) -> str:
    """Bold-italic sans font (the style used in the delete notice)."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D63C + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D656 + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def _delete_duration_text() -> str:
    n = AUTO_DELETE_SECONDS
    if n >= 3600 and n % 3600 == 0:
        h = n // 3600
        return f"{h} hour" + ("s" if h > 1 else "")
    if n >= 60:
        return f"{max(1, n // 60)} min"
    return f"{n} sec"


def deleted_notice_text() -> str:
    d = _delete_duration_text()
    return (
        f"🗑️ {_bi('Your video / file has been deleted.')}\n\n"
        f"⏱️ {_bi('The ' + d + ' access time has expired.')}\n\n"
        f"📥 {_bi('If you want to access it again, please download & save it again.')}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🇮🇳 {_bi('Video / file delete ho gaya hai.')}\n"
        f"📥 {_bi('Dobara download karke save kar len.')}\n\n"
        f"🙏 {_bi('Dhanyavaad!')}"
    )


_LAST_NOTICE: dict = {}  # chat_id -> ts, so a playlist doesn't spam one notice per video


def schedule_delete(chat_id: int, msg_id: int):
    """Queue a sent file for deletion AUTO_DELETE_SECONDS from now (survives restarts)."""
    if AUTO_DELETE_SECONDS <= 0 or not msg_id:
        return
    PENDING_DELETES.append({"chat": chat_id, "msg": msg_id, "at": time.time() + AUTO_DELETE_SECONDS})
    save_data()


async def auto_delete_worker(client: Client):
    """Every 30 s deletes the files whose time is up. Pending items are stored in DB, so a restart resumes them."""
    while True:
        try:
            now = time.time()
            due = [e for e in PENDING_DELETES if e.get("at", 0) <= now]
            if due:
                by_chat: dict = {}
                for e in due:
                    by_chat.setdefault(e["chat"], []).append(e["msg"])
                for cid, ids in by_chat.items():
                    deleted_ok = False
                    for i in range(0, len(ids), 100):
                        try:
                            await client.delete_messages(cid, ids[i:i + 100])
                            deleted_ok = True
                        except FloodWait as fw:
                            await asyncio.sleep(fw.value)
                        except Exception as e:
                            logger.debug(f"auto-delete failed in {cid}: {e}")
                    if deleted_ok and time.time() - _LAST_NOTICE.get(cid, 0) > 600:
                        _LAST_NOTICE[cid] = time.time()
                        try:
                            await send_photo_or_text(client, cid, AUTO_DELETE_PHOTO, deleted_notice_text())
                        except Exception as e:
                            logger.debug(f"delete notice failed in {cid}: {e}")
                due_ids = {id(e) for e in due}
                PENDING_DELETES[:] = [e for e in PENDING_DELETES if id(e) not in due_ids]
                save_data()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"auto_delete_worker error: {e}")
        await asyncio.sleep(30)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


admin_only_early = filters.create(lambda _, __, m: bool(getattr(m, "from_user", None)) and is_admin(m.from_user.id))


def full_name(u) -> str:
    return " ".join(p for p in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if p)


def register_user(user_id: int, username=None, name=None) -> bool:
    """Create/update the user record. Returns True if the user is new."""
    uid, now = str(user_id), datetime.now().isoformat()
    is_new = uid not in DB["users"]
    if is_new:
        DB["users"][uid] = {
            "username": username, "full_name": name, "join_date": now,
            "total_downloads": 0, "last_active": now, "verified": False,
        }
    else:
        u = DB["users"][uid]
        u["last_active"] = now
        if username:
            u["username"] = username
        if name:
            u["full_name"] = name
    save_data(force=is_new)
    return is_new


def increment_downloads(user_id: int):
    u = DB["users"].get(str(user_id))
    if u is not None:
        u["total_downloads"] = u.get("total_downloads", 0) + 1
    DB["total_downloads"] = DB.get("total_downloads", 0) + 1
    save_data()


def set_verified(user_id: int, verified: bool = True):
    u = DB["users"].get(str(user_id))
    if u is not None:
        u["verified"] = verified
        save_data()


def is_verified(user_id: int) -> bool:
    return bool(DB["users"].get(str(user_id), {}).get("verified", False))


def reset_verifications():
    for u in DB["users"].values():
        u["verified"] = False
    save_data()


def get_stats():
    """(total_users, total_downloads, active_today, uptime_text)"""
    today, active = datetime.now().date(), 0
    for u in DB["users"].values():
        try:
            if datetime.fromisoformat(u["last_active"]).date() == today:
                active += 1
        except Exception:
            pass
    up = datetime.now() - BOT_START_TIME
    uptime = f"{up.days}d {up.seconds // 3600}h {(up.seconds // 60) % 60}m"
    return len(DB["users"]), DB.get("total_downloads", 0), active, uptime


# ---------------------------------------------------------------------
# Premium / daily limit / referrals  (ported from fbot, JSON-backed)
# ---------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(iso):
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ensure_user(user_id: int) -> dict:
    """Get the user record, creating a stub if they never pressed /start
    (e.g. admin grants premium to an ID before that person starts the bot)."""
    uid = str(user_id)
    if uid not in DB["users"]:
        now = datetime.now().isoformat()
        DB["users"][uid] = {
            "username": None, "full_name": None, "join_date": now,
            "total_downloads": 0, "last_active": now, "verified": False,
        }
    return DB["users"][uid]


def get_premium_status(user_id: int) -> dict:
    """{'is_premium', 'lifetime', 'expires_at'} — admins are always Lifetime Premium."""
    if is_admin(user_id):
        return {"is_premium": True, "lifetime": True, "expires_at": None}
    u = DB["users"].get(str(user_id))
    if u:
        if u.get("premium_lifetime"):
            return {"is_premium": True, "lifetime": True, "expires_at": None}
        until = _parse_dt(u.get("premium_until"))
        if until and until > _now_utc():
            return {"is_premium": True, "lifetime": False, "expires_at": until}
    return {"is_premium": False, "lifetime": False, "expires_at": None}


def _extend_premium(user_id: int, days: int):
    """Adds days on top of any still-active premium (otherwise from now)."""
    u = _ensure_user(user_id)
    if u.get("premium_lifetime"):
        return
    base = _now_utc()
    until = _parse_dt(u.get("premium_until"))
    if until and until > base:
        base = until
    u["premium_until"] = (base + timedelta(days=days)).isoformat()
    save_data()


def set_premium(user_id: int, days):
    """days=None -> lifetime. A number extends an active plan instead of replacing it."""
    if days is None:
        u = _ensure_user(user_id)
        u["premium_lifetime"], u["premium_until"] = True, None
        save_data()
    else:
        _ensure_user(user_id)["premium_lifetime"] = False
        _extend_premium(user_id, days)


def remove_premium(user_id: int):
    u = DB["users"].get(str(user_id))
    if u is not None:
        u["premium_lifetime"], u["premium_until"] = False, None
        save_data()


def premium_count() -> int:
    return sum(1 for uid in list(DB["users"]) if get_premium_status(int(uid))["is_premium"]
               and not is_admin(int(uid)))


def _today_utc() -> str:
    return _now_utc().strftime("%Y-%m-%d")


def get_daily_count(user_id: int) -> int:
    d = (DB["users"].get(str(user_id)) or {}).get("daily") or {}
    return d.get("count", 0) if d.get("date") == _today_utc() else 0


def bump_daily_count(user_id: int):
    u = _ensure_user(user_id)
    d = u.get("daily")
    if not d or d.get("date") != _today_utc():
        d = {"date": _today_utc(), "count": 0}
    d["count"] += 1
    u["daily"] = d
    save_data()


def set_referrer(user_id: int, referrer_id: int) -> bool:
    """Only the first time, never self, and only for a referrer who really exists."""
    if user_id == referrer_id or str(referrer_id) not in DB["users"]:
        return False
    u = DB["users"].get(str(user_id))
    if u is None or u.get("referred_by"):
        return False
    u["referred_by"] = referrer_id
    save_data()
    return True


def increment_referral_count(referrer_id: int) -> int:
    u = _ensure_user(referrer_id)
    u["referral_count"] = u.get("referral_count", 0) + 1
    save_data()
    return u["referral_count"]


def get_referral_count(user_id: int) -> int:
    return (DB["users"].get(str(user_id)) or {}).get("referral_count", 0)


def get_referral_rewards_claimed(user_id: int) -> list:
    return (DB["users"].get(str(user_id)) or {}).get("referral_rewards", [])


def mark_referral_reward_claimed(user_id: int, threshold: int):
    u = _ensure_user(user_id)
    claimed = u.setdefault("referral_rewards", [])
    if threshold not in claimed:
        claimed.append(threshold)
    save_data()


def start_keep_alive():
    """Tiny health-check web server for Koyeb/Render (dl.py used Flask).
    Runs only if PORT is set (hosts set it) or KEEP_ALIVE=1."""
    port = os.getenv("PORT")
    if not port and os.getenv("KEEP_ALIVE", "0") != "1":
        return

    class Handler(BaseHTTPRequestHandler):
        def _ok(self, body=True):
            data = b"YouTube Downloader Bot is alive and running!"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_GET(self):
            self._ok()

        def do_HEAD(self):
            self._ok(body=False)

        def log_message(self, *args):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(port or 8080)), Handler)
    except Exception as e:
        logger.warning(f"keep-alive server failed to start: {e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    logger.info(f"keep-alive server on port {srv.server_address[1]}")


# ---------------------------------------------------------------------
# Buttons / menus
# ---------------------------------------------------------------------
BTN_PRIMARY = ButtonStyle.PRIMARY if BUTTON_STYLE_SUPPORTED else None
BTN_DANGER = ButtonStyle.DANGER if BUTTON_STYLE_SUPPORTED else None
# green; if this kurigram build has no SUCCESS style, fall back to blue instead of crashing
BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", BTN_PRIMARY) if BUTTON_STYLE_SUPPORTED else None


def make_button(text, callback_data=None, url=None, style=None):
    kw = {"text": text}
    if callback_data:
        kw["callback_data"] = callback_data
    if url:
        kw["url"] = url
    if BUTTON_STYLE_SUPPORTED and style is not None:
        kw["style"] = style
    # premium emoji as the button icon (leading emoji of the label); dropped again if this build can't do it
    label, icon_id = premium_emoji.split_button_icon(text)
    if icon_id:
        kw["text"] = label
        kw["icon_custom_emoji_id"] = icon_id
    try:
        return InlineKeyboardButton(**kw)
    except TypeError:
        if icon_id:
            kw.pop("icon_custom_emoji_id", None)
            kw["text"] = text
            try:
                return InlineKeyboardButton(**kw)
            except TypeError:
                pass
        kw.pop("style", None)
        return InlineKeyboardButton(**kw)


def make_reply_button(text, style=None):
    if BUTTON_STYLE_SUPPORTED and style is not None:
        try:
            return KeyboardButton(text=text, style=style)
        except TypeError:
            pass
    return text


BTN_HELP = "❓ ʜᴇʟᴘ"
BTN_ABOUT = "ℹ️ ᴀʙᴏᴜᴛ"
BTN_SUPPORT = "☎️ sᴜᴘᴘᴏʀᴛ"
BTN_STATS = "📊 ᴍʏ sᴛᴀᴛs"
BTN_ADMIN = "👑 ᴀᴅᴍɪɴ"
BTN_PLANS = "💎 ᴘʟᴀɴs"
MENU_BUTTON_TEXTS = {BTN_HELP, BTN_ABOUT, BTN_SUPPORT, BTN_STATS, BTN_ADMIN, BTN_PLANS}

def _menu_rows(admin: bool):
    rows = [
        [make_reply_button(BTN_HELP, BTN_PRIMARY), make_reply_button(BTN_ABOUT, BTN_PRIMARY)],
        [make_reply_button(BTN_SUPPORT, BTN_PRIMARY), make_reply_button(BTN_STATS, BTN_PRIMARY)],
    ]
    third = [make_reply_button(BTN_PLANS, BTN_PRIMARY)]
    if admin:
        third.append(make_reply_button(BTN_ADMIN, BTN_PRIMARY))
    rows.append(third)
    return rows


MAIN_MENU_KB = ReplyKeyboardMarkup(_menu_rows(False), resize_keyboard=True)
ADMIN_MENU_KB = ReplyKeyboardMarkup(_menu_rows(True), resize_keyboard=True)


def _tolerant_button_pattern(expected: str) -> str:
    # U+FE0F (emoji variation selector) is not always echoed back by clients
    base = expected.replace("\ufe0f", "")
    return "^" + r"\ufe0f?".join(re.escape(ch) for ch in base) + r"\ufe0f?$"


def menu_text_filter(expected: str):
    return filters.regex(_tolerant_button_pattern(expected))


NOT_MENU_BUTTON = ~filters.regex(
    "|".join(f"(?:{_tolerant_button_pattern(t)})" for t in MENU_BUTTON_TEXTS)
)


def _is_menu_text(text) -> bool:
    t = (text or "").replace("\ufe0f", "")
    return any(t == x.replace("\ufe0f", "") for x in MENU_BUTTON_TEXTS)


def fallback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            make_button(SC("📥 Download"), "fallback_download", style=BTN_PRIMARY),
            make_button(SC("❓ Help"), "fallback_help", style=BTN_PRIMARY),
        ]]
    )


# ---------------------------------------------------------------------
# Texts  (welcome / help / about — same style as the original bot)
# ---------------------------------------------------------------------
FALLBACK_TEXT = "👇 Apna YouTube link ya video/song ka naam bhejo boss!"

NOT_A_LINK_TEXT = (
    "🤨 <b>Bhai ye kaunsa link hai? YouTube ka toh nahi lagta!</b>\n\n"
    "Agar lagta hai YouTube ka hai aur error aa rha, toh screenshot ke saath "
    f'idhar report kro 👉 <a href="{POWERED_BY_URL}">{POWERED_BY}</a>\n\n'
    "📌 <b>Example:</b>\n"
    "<code>https://youtu.be/dQw4w9WgXcQ</code>"
)


def _bi(text: str) -> str:
    """Sans-serif bold-italic unicode (𝙃𝙚𝙡𝙡𝙤 / 𝟭𝟰𝟰𝙋) for the welcome text."""
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(0x1D63C + ord(ch) - ord("A")))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D656 + ord(ch) - ord("a")))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def build_welcome(first_name: str, bot_username: str, bot_name: str) -> str:
    powered_link = f'<a href="{POWERED_BY_URL}">{_bi(POWERED_BY)}</a>'
    top_q = "4K" if MAX_HEIGHT >= 2160 else f"{MAX_HEIGHT}p"
    line = "━━━━━━━━━━━━━━━━━━"
    return (
        f"👋 {_bi('Hello')} {esc(first_name) or 'there'},\n\n"
        f"✨ {_bi('Welcome to YouTube Downloader')}\n\n"
        f"{line}\n\n"
        f"<blockquote>📥 {_bi('YOUTUBE DOWNLOADER')}\n"
        f"💎 {_bi('FAST & POWERFUL SERVICE').replace('&', '&amp;')}\n\n"
        f"🔥 {_bi('YOUTUBE DOWNLOADING IS LIVE!')}\n\n"
        f"🎬 {_bi('VIDEO DOWNLOAD')}\n"
        f"🎵 {_bi('MP3 AUDIO DOWNLOAD')}\n"
        f"⚡ {_bi('144P TO ' + top_q.upper() + ' QUALITY')}\n"
        f"🔎 {_bi('SEARCH BY NAME')}\n"
        f"📚 {_bi('FULL PLAYLIST DOWNLOAD')}\n"
        f"📊 {_bi('LIVE PROGRESS & SPEED').replace('&', '&amp;')}\n"
        f"✂️ {_bi('AUTO-SPLIT FOR 2 GB+ FILES')}</blockquote>\n"
        f"🚀 {_bi('Just send your YouTube link — we\'ll do the rest!')}\n\n"
        f"{line}\n\n"
        f"<blockquote>🔗 {_bi('SUPPORTED LINKS')}\n\n"
        "• youtube.com / youtu.be\n"
        "• youtube.com/shorts\n"
        "• music.youtube.com\n"
        "• youtube.com/playlist</blockquote>\n\n"
        f"{line}\n\n"
        f"👑 {_bi('Powered by')} {powered_link}\n"
        f"⚡ {_bi('Speed • Performance • Reliability')}\n"
        f"{line}"
    )


HELP_TEXT = '''ℹ️ 𝙃𝙊𝙒 𝙏𝙊 𝙐𝙎𝙀

━━━━━━━━━━━━━━━━━━
<blockquote>🔹 𝙎𝙀𝙉𝘿 𝙏𝙃𝙀 𝙇𝙄𝙉𝙆
📥 𝙋𝙖𝙨𝙩𝙚 𝙖𝙣𝙮 𝙔𝙤𝙪𝙏𝙪𝙗𝙚 𝙐𝙍𝙇 𝙙𝙞𝙧𝙚𝙘𝙩𝙡𝙮 𝙞𝙣 𝙩𝙝𝙚 𝙘𝙝𝙖𝙩.

🔗 𝙎𝙐𝙋𝙋𝙊𝙍𝙏𝙀𝘿 𝙁𝙊𝙍𝙈𝘼𝙏𝙎
• youtube.com / youtu.be
• youtube.com/shorts
• music.youtube.com
• youtube.com/playlist</blockquote>
━━━━━━━━━━━━━━━━━━
<blockquote>📌 𝙀𝙓𝘼𝙈𝙋𝙇𝙀𝙎

🎬 https://youtu.be/dQw4w9WgXcQ
🎬 https://www.youtube.com/shorts/abcDEF12345
📚 https://www.youtube.com/playlist?list=PLxxxxxxxx</blockquote>
━━━━━━━━━━━━━━━━━━
<blockquote expandable>💡 𝙐𝙎𝙀𝙁𝙐𝙇 𝙏𝙄𝙋𝙎

🎬 𝙑𝙄𝘿𝙀𝙊 𝙌𝙐𝘼𝙇𝙄𝙏𝙔
𝙎𝙚𝙡𝙚𝙘𝙩 𝙮𝙤𝙪𝙧 𝙥𝙧𝙚𝙛𝙚𝙧𝙧𝙚𝙙 𝙦𝙪𝙖𝙡𝙞𝙩𝙮 𝙛𝙧𝙤𝙢 𝙩𝙝𝙚 𝙗𝙪𝙩𝙩𝙤𝙣𝙨.

🎵 𝙈𝙋𝟯 𝘼𝙐𝘿𝙄𝙊
𝙏𝙖𝙥 𝙈𝙋𝟯 𝙛𝙤𝙧 𝙖𝙪𝙙𝙞𝙤-𝙤𝙣𝙡𝙮 𝙙𝙤𝙬𝙣𝙡𝙤𝙖𝙙.

✂️ 𝟮 𝙂𝘽+ 𝙁𝙄𝙇𝙀𝙎
𝙇𝙖𝙧𝙜𝙚 𝙛𝙞𝙡𝙚𝙨 𝙖𝙧𝙚 𝙖𝙪𝙩𝙤𝙢𝙖𝙩𝙞𝙘𝙖𝙡𝙡𝙮 𝙨𝙥𝙡𝙞𝙩 𝙞𝙣𝙩𝙤 𝙥𝙖𝙧𝙩𝙨.

📚 𝙋𝙇𝘼𝙔𝙇𝙄𝙎𝙏 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿
𝙎𝙚𝙣𝙙 𝙖 𝙥𝙡𝙖𝙮𝙡𝙞𝙨𝙩 𝙡𝙞𝙣𝙠 𝙖𝙣𝙙 𝙘𝙝𝙤𝙤𝙨𝙚 𝙮𝙤𝙪𝙧 𝙦𝙪𝙖𝙡𝙞𝙩𝙮.
𝙑𝙞𝙙𝙚𝙤𝙨 𝙬𝙞𝙡𝙡 𝙗𝙚 𝙙𝙚𝙡𝙞𝙫𝙚𝙧𝙚𝙙 𝙤𝙣𝙚 𝙗𝙮 𝙤𝙣𝙚.
💎 𝙋𝙧𝙚𝙢𝙞𝙪𝙢: 𝙉𝙤 𝙡𝙞𝙢𝙞𝙩

🔎 𝙎𝙀𝘼𝙍𝘾𝙃 𝘽𝙔 𝙉𝘼𝙈𝙀
𝙉𝙤 𝙡𝙞𝙣𝙠? 𝙅𝙪𝙨𝙩 𝙨𝙚𝙣𝙙 𝙩𝙝𝙚 𝙨𝙤𝙣𝙜/𝙫𝙞𝙙𝙚𝙤 𝙣𝙖𝙢𝙚 𝙤𝙧 𝙪𝙨𝙚:
"/search name"

🔄 𝙁𝘼𝙄𝙇𝙀𝘿 𝘿𝙊𝙒𝙉𝙇𝙊𝘼𝘿
𝙅𝙪𝙨𝙩 𝙨𝙚𝙣𝙙 𝙩𝙝𝙚 𝙡𝙞𝙣𝙠 𝙖𝙜𝙖𝙞𝙣.

🛑 "/cancel"
𝙎𝙩𝙤𝙥 𝙖𝙣 𝙖𝙘𝙩𝙞𝙫𝙚 𝙙𝙤𝙬𝙣𝙡𝙤𝙖𝙙.

📊 "/stats"
𝘾𝙝𝙚𝙘𝙠 𝙮𝙤𝙪𝙧 𝙙𝙤𝙬𝙣𝙡𝙤𝙖𝙙 𝙨𝙩𝙖𝙩𝙨.

💎 "/plans"
𝙑𝙞𝙚𝙬 𝙥𝙧𝙚𝙢𝙞𝙪𝙢 𝙥𝙡𝙖𝙣𝙨.

👤 "/myplan"
𝘾𝙝𝙚𝙘𝙠 𝙮𝙤𝙪𝙧 𝙘𝙪𝙧𝙧𝙚𝙣𝙩 𝙥𝙡𝙖𝙣.

🎁 "/referral"
𝙄𝙣𝙫𝙞𝙩𝙚 𝙛𝙧𝙞𝙚𝙣𝙙𝙨 𝙖𝙣𝙙 𝙚𝙖𝙧𝙣 𝙛𝙧𝙚𝙚 𝙥𝙧𝙚𝙢𝙞𝙪𝙢.</blockquote>
━━━━━━━━━━━━━━━━━━
<blockquote>⚠️ 𝙃𝘼𝙑𝙄𝙉𝙂 𝙏𝙍𝙊𝙐𝘽𝙇𝙀?

𝙈𝙖𝙠𝙚 𝙨𝙪𝙧𝙚 𝙮𝙤𝙪'𝙧𝙚 𝙨𝙚𝙣𝙙𝙞𝙣𝙜 𝙖 𝙫𝙖𝙡𝙞𝙙 𝙔𝙤𝙪𝙏𝙪𝙗𝙚 𝙡𝙞𝙣𝙠.

🚀 𝙎𝙞𝙢𝙥𝙡𝙮 𝙨𝙚𝙣𝙙 𝙖 𝙡𝙞𝙣𝙠 𝙖𝙣𝙙 𝙡𝙚𝙩 𝙩𝙝𝙚 𝙗𝙤𝙩 𝙙𝙤 𝙩𝙝𝙚 𝙧𝙚𝙨𝙩!</blockquote>
━━━━━━━━━━━━━━━━━━'''

SUPPORT_TEXT = SC(
    "📞 <b>Support</b>\n\n"
    "Koi problem? Idhar baat karo:\n\n"
    f'👤 Admin: <a href="{POWERED_BY_URL}">{POWERED_BY}</a>\n\n'
    "⏰ 24 ghante ke andar reply, pakka!"
)

ACCESS_DENIED_TEXT = SC("⚠️ <b>Access Denied!</b>\n\nIs bot ko use karne ke liye pehle ye channels join karo:")
MAINTENANCE_TEXT = SC("🔧 <b>Bot maintenance mein hai.</b>\nThodi der baad try karo.")


def build_about() -> str:
    """About text (bold-italic style) + live global stats."""
    users, downloads, active, uptime = get_stats()
    uptime_bi = re.sub(r"[A-Za-z]+", lambda m: _bi(m.group()), str(uptime))
    dev = f'<a href="{POWERED_BY_URL}">{_bi(POWERED_BY)}</a>'
    lib = '<a href="https://docs.pyrogram.org/">' + _bi("Pyrogram Async") + "</a>"
    eng = '<a href="https://github.com/yt-dlp/yt-dlp">' + _bi("YT-DLP") + "</a>"
    lang = '<a href="https://www.python.org/">' + _bi("Python") + " 3.11+</a>"
    line = "━━━━━━━━━━━━━━━━━━"
    return (
        f"💠 {_bi('ABOUT THIS BOT')} 💠\n\n"
        f"╭──────[ ✨ {_bi('YOUTUBE DOWNLOADER')} ]──────⍟\n\n"
        f"<blockquote>├⍟ 🤖 {_bi('BOT NAME')}\n"
        f"├─ {_bi('YouTube Downloader Bot')}\n\n"
        f"├⍟ 👨‍💻 {_bi('DEVELOPER')}\n"
        f"├─ {dev}\n\n"
        f"├⍟ 🔗 {_bi('LIBRARY')}\n"
        f"├─ {lib}\n\n"
        f"├⍟ 🎬 {_bi('ENGINE')}\n"
        f"├─ {eng}\n\n"
        f"├⍟ ⚡ {_bi('LANGUAGE')}\n"
        f"├─ {lang}</blockquote>\n"
        f"{line}\n\n"
        f"<blockquote>├⍟ 👥 {_bi('TOTAL USERS')}\n"
        f"├─ {users:,}\n\n"
        f"├⍟ 📥 {_bi('TOTAL DOWNLOADS')}\n"
        f"├─ {downloads:,}\n\n"
        f"├⍟ 🟢 {_bi('ACTIVE TODAY')}\n"
        f"├─ {active}\n\n"
        f"├⍟ ⏱ {_bi('UPTIME')}\n"
        f"├─ {uptime_bi}</blockquote>\n"
        "╰────────────────────⍟\n\n"
        f"🚀 {_bi('FAST • RELIABLE • POWERFUL')}\n"
        f"👑 {_bi('Designed')} &amp; {_bi('Powered by')} {dev}"
    )

# ---------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
premium_emoji.install()  # custom emoji in every HTML message (no-op if unsupported / PREMIUM_EMOJI=0)

app = Client(
    "yt_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    sleep_threshold=60,
    max_concurrent_transmissions=4,
    in_memory=True,
    parse_mode=ParseMode.HTML,
)

PENDING: dict[str, dict] = {}   # token -> request waiting for a quality pick
WAITING: dict[int, str] = {}    # admin_id -> action waiting for their next message
COOLDOWNS: dict[int, float] = {}  # user_id -> time of last accepted link
ACTIVE: dict[int, dict] = {}    # user_id -> running job (cancel event / task / phase)


def _utag(uid: int) -> str:
    info = DB["users"].get(str(uid), {})
    name = info.get("full_name") or str(uid)
    uname = f" @{info['username']}" if info.get("username") else ""
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>{uname} (<code>{uid}</code>)'


async def _send_log(text: str):
    try:
        await app.send_message(LOG_CHANNEL, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        logger.warning(f"log channel send failed: {e}")


def log_event(text: str):
    """Fire-and-forget post to LOG_CHANNEL (no-op when it isn't configured)."""
    if LOG_CHANNEL:
        asyncio.ensure_future(_send_log(text))


class PriorityDownloadSemaphore:
    """Bounds concurrent downloads like asyncio.Semaphore, plus a priority lane:
    when every slot is busy, a freed slot goes to the oldest *premium* waiter first
    ("🎯 Tumhara kaam pehle"), then to the oldest normal waiter. FIFO inside each lane.
    Use as `async with download_semaphore(priority=is_premium):`."""

    def __init__(self, value: int):
        self._value = value
        self._priority_waiters: deque = deque()
        self._normal_waiters: deque = deque()

    async def _acquire(self, priority: bool) -> None:
        if self._value > 0:
            self._value -= 1
            return
        fut = asyncio.get_running_loop().create_future()
        queue = self._priority_waiters if priority else self._normal_waiters
        queue.append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()  # slot was already handed to us — give it back
            else:
                try:
                    queue.remove(fut)
                except ValueError:
                    pass
            raise

    def release(self) -> None:
        # Hand the slot straight to the next waiter (no counter bump) so a brand-new
        # acquire() can't steal it and defeat the ordering.
        for queue in (self._priority_waiters, self._normal_waiters):
            while queue:
                fut = queue.popleft()
                if not fut.done():
                    fut.set_result(None)
                    return
        self._value += 1

    def __call__(self, priority: bool = False):
        return _PriorityAcquireCtx(self, priority)

    async def __aenter__(self):
        await self._acquire(False)
        return self

    async def __aexit__(self, *exc):
        self.release()


class _PriorityAcquireCtx:
    def __init__(self, sem: PriorityDownloadSemaphore, priority: bool):
        self._sem, self._priority = sem, priority

    async def __aenter__(self):
        await self._sem._acquire(self._priority)
        return self._sem

    async def __aexit__(self, *exc):
        self._sem.release()


download_semaphore = PriorityDownloadSemaphore(MAX_CONCURRENT_DOWNLOADS)

YT_RE = re.compile(
    r"(?<![\w.@-])"                                   # no 'notyoutube.com' / 'x.youtube.com.evil'
    r"(?:https?://)?(?:www\.|m\.|music\.|gaming\.)?"
    r"(?:youtube\.com/(?:watch\?\S*?v=|shorts/|live/|embed/|v/|e/)"
    r"|youtube-nocookie\.com/embed/"
    r"|youtu\.be/)"
    r"[\w-]{11}(?![\w-])\S*",                         # exactly 11-char video ID
    re.I,
)


PLAYLIST_PAGE_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?youtube\.com/playlist\?\S*?list=([\w-]+)", re.I
)
LIST_PARAM_RE = re.compile(r"[?&]list=([\w-]+)")


def extract_playlist_url(text: str):
    """Pure playlist link (youtube.com/playlist?list=...) -> canonical URL."""
    m = PLAYLIST_PAGE_RE.search(text or "")
    return f"https://www.youtube.com/playlist?list={m.group(1)}" if m else None


def playlist_url_from_video_url(url: str):
    """Video link that also carries &list=... -> that playlist's URL."""
    m = LIST_PARAM_RE.search(url or "")
    return f"https://www.youtube.com/playlist?list={m.group(1)}" if m else None


def extract_youtube_url(text: str):
    m = YT_RE.search(text or "")
    if not m:
        return None
    url = m.group(0)
    return url if url.lower().startswith("http") else "https://" + url


# ---------------------------------------------------------------------
# yt-dlp helpers (blocking — always run in a thread)
# ---------------------------------------------------------------------
LADDER = [144, 240, 360, 480, 720, 1080, 1440, 2160]


# Cookie-free client sets, tried in order. The first one asks several clients
# at once (yt-dlp merges their formats); the rest are single-client retries used
# when the merged result comes back with only low resolutions.
CLIENT_SETS = [
    ["default", "android_vr", "tv", "web_safari", "mweb"],
    ["android_vr"],
    ["tv"],
    ["web_safari"],
    ["mweb"],
    ["ios"],
]
GOOD_ENOUGH_HEIGHT = 1080


def client_sets() -> list:
    """With the PO-token provider up (pot_provider.py), the "web" client is tried first —
    it is the only one exposing the full 1080p/1440p/4K ladder. Otherwise the plain list."""
    if pot_provider.is_ready():
        return [["web", "default", "android_vr", "tv", "web_safari", "mweb"]] + CLIENT_SETS
    return CLIENT_SETS


def base_opts(clients=None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "geo_bypass": True,
        # JS runtime for YouTube's challenge solver (Node is in the Dockerfile)
        "js_runtimes": {"node": {}},
        "extractor_args": {"youtube": {"player_client": clients or client_sets()[0]}},
        # keeps yt-dlp's warnings so a real HTTP 429 / "only images" can be recognised
        "logger": yt_auth.DiagLogger(),
    }
    # Cookies are optional. Re-checked on every call, so a fresh /cookies upload works at once.
    cookie_file = yt_auth.active_cookie_path()
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    return opts


class YouTubeCooldown(Exception):
    """Raised instead of calling YouTube while the global 429 cooldown is active."""


def check_cooldown():
    left = yt_auth.cooldown_remaining()
    if left > 0:
        raise YouTubeCooldown(
            f"YouTube ne is server ko abhi rate-limit kiya hai (HTTP 429). "
            f"{yt_auth.human_time_short(left)} baad dobara try karo."
        )


async def _alert_admins_429():
    mins = yt_auth.cooldown_remaining() // 60 or 1
    text = (f"🛑 <b>YouTube HTTP 429</b>\n\nServer IP rate-limited hai. Bot ~{mins} min ke liye YouTube requests "
            f"rok raha hai.\n\n• Fresh cookies: <code>/cookies</code>\n• Check: <code>/authcheck</code>\n"
            f"• Manual clear: <code>/clearcooldown</code>")
    log_event(text)
    for aid in ADMIN_IDS:
        try:
            await app.send_message(aid, text)
        except Exception:
            pass


def note_youtube_error(err: str) -> bool:
    """Call with every yt-dlp error text. An explicit 429 starts the shared cooldown (and DMs the
    admins once per incident). Returns True for 429 / sign-in wall, where other clients can't help."""
    if yt_auth.is_rate_limit_error(err):
        if yt_auth.activate_cooldown():
            logger.warning("YouTube 429 -> cooldown started")
            try:
                asyncio.run_coroutine_threadsafe(_alert_admins_429(), loop)
            except Exception:
                pass
        return True
    return yt_auth.is_hard_youtube_block(err)


def _err_with_diag(e: Exception, opts: dict) -> str:
    diag = getattr(opts.get("logger"), "context", lambda: "")()
    return f"{e} | {diag}" if diag else str(e)


def _max_height(info) -> int:
    return max((f.get("height") or 0 for f in info.get("formats", [])), default=0)


_INFO_CACHE: dict = {}  # url -> (ts, info, clients)
INFO_CACHE_TTL = 600    # seconds; repeat links / playlist re-taps answer instantly
FETCH_PARALLEL = 3      # client sets tried at the same time
FETCH_OK_HEIGHT = 720   # once this is reached and the main sets answered, stop waiting


def _fetch_one(url: str, clients):
    opts = base_opts(clients)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False), None
    except Exception as e:
        return None, _err_with_diag(e, opts)


def fetch_info(url: str):
    """Returns (info, clients). Client sets are tried IN PARALLEL (was one after another,
    which made low-quality videos wait for all 7 sets). First set reaching GOOD_ENOUGH_HEIGHT
    wins; otherwise the best one after the sets finish. Results are cached for 10 min."""
    check_cooldown()
    now = time.time()
    for k in [k for k, v in _INFO_CACHE.items() if now - v[0] > INFO_CACHE_TTL]:
        _INFO_CACHE.pop(k, None)
    hit = _INFO_CACHE.get(url)
    if hit:
        return hit[1], hit[2]

    import concurrent.futures as cf
    sets = client_sets()
    best, best_clients, last_err, blocked = None, None, None, False
    ex = cf.ThreadPoolExecutor(max_workers=FETCH_PARALLEL)
    try:
        futs = {ex.submit(_fetch_one, url, cs): cs for cs in sets}
        done_n = 0
        for fut in cf.as_completed(futs):
            clients = futs[fut]
            info, err = fut.result()
            done_n += 1
            if info is None:
                last_err = RuntimeError(err)
                logger.info(f"client set {clients} failed: {err[:160]}")
                if note_youtube_error(err):  # 429 / sign-in wall: more clients only make it worse
                    blocked = True
                    break
                continue
            h = _max_height(info)
            logger.info(f"{info.get('id')}: clients={clients} max_height={h}")
            if best is None or h > _max_height(best):
                best, best_clients = info, clients
            if h >= GOOD_ENOUGH_HEIGHT:
                break
            # two sets answered and we already have a decent ladder: don't wait for the rest
            if done_n >= 2 and _max_height(best) >= FETCH_OK_HEIGHT:
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if best is None:
        raise last_err or RuntimeError("Could not fetch video info.")
    _INFO_CACHE[url] = (time.time(), best, best_clients)
    return best, best_clients


def fetch_playlist(url: str) -> dict:
    """Fast flat listing of a playlist (no per-video extraction)."""
    check_cooldown()
    # flat listing makes no per-video player requests, so one light client is enough (was 6)
    opts = {**base_opts(["default"]), "noplaylist": False, "extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        full = _err_with_diag(e, opts)
        note_youtube_error(full)
        raise RuntimeError(full) from e
    entries = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        title = e.get("title") or "video"
        if title in ("[Private video]", "[Deleted video]"):
            continue
        entries.append({"id": e["id"], "title": title,
                        "url": f"https://www.youtube.com/watch?v={e['id']}"})
    if PLAYLIST_MAX:
        entries = entries[:PLAYLIST_MAX]
    logger.info(f"playlist {info.get('id')}: {len(entries)} videos")
    return {"title": info.get("title") or "Playlist", "entries": entries}


def _video_formats(info):
    return [
        f for f in info.get("formats", [])
        if f.get("vcodec") not in (None, "none") and f.get("width") and f.get("height")
    ]


def is_vertical(info) -> bool:
    fm = _video_formats(info)
    return bool(fm) and max(fm, key=lambda f: f["height"])["height"] > max(fm, key=lambda f: f["height"])["width"]


def quality_options(info) -> list[tuple[int, str]]:
    """[(height, size_label)] — only heights this video really has."""
    fm = _video_formats(info)
    vertical = is_vertical(info)
    key = "width" if vertical else "height"
    sides = {f[key] for f in fm}
    audio = [f for f in info.get("formats", []) if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audio, key=lambda f: f.get("abr") or 0, default=None)
    a_size = (best_audio or {}).get("filesize") or (best_audio or {}).get("filesize_approx") or 0

    def label_for(h):
        cands = [f for f in fm if f[key] <= h]
        v = max(cands, key=lambda f: (f[key], f.get("tbr") or 0), default=None)
        v_size = (v or {}).get("filesize") or (v or {}).get("filesize_approx") or 0
        return f"~{human_size(v_size + a_size)}" if v_size else ""

    # Always offer the whole ladder (144p .. MAX_HEIGHT). Steps YouTube did not list
    # for this fetch get label "?" — download_blocking() then retries other clients
    # to try to reach that quality instead of silently giving a lower one.
    out, prev = [], 0
    for h in LADDER:
        if h > MAX_HEIGHT:
            break
        if any(prev < s <= h for s in sides):
            out.append((h, label_for(h)))
        else:
            out.append((h, "?"))
        prev = h
    return out


def format_selector(height: int, vertical: bool) -> str:
    k = "width" if vertical else "height"
    return (
        f"bestvideo[{k}<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[{k}<={height}]+bestaudio/best[{k}<={height}]/best"
    )


def strict_selector(height: int, vertical: bool) -> str:
    """Only streams that really land in (previous ladder step, height] — no silent fallback."""
    k = "width" if vertical else "height"
    prev = max((x for x in LADDER if x < height), default=0)
    return (f"bestvideo[{k}<={height}][{k}>{prev}]+bestaudio/"
            f"best[{k}<={height}][{k}>{prev}]")


_CDN_THUMBS = ("maxresdefault", "sddefault", "hqdefault")


def _thumb_to_telegram(src: str, dst: str) -> bool:
    """Telegram wants a small JPEG (<=320px, <200 KB). Blocking."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vf", "scale=320:-2", "-q:v", "5", dst],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def _download_cdn_thumb(vid: str, out_dir: str):
    """Fallback 1: YouTube's own CDN thumbnail (best quality first). Blocking."""
    import urllib.request
    dst = os.path.join(out_dir, f"cdn_{vid}.jpg")
    for name in _CDN_THUMBS:
        try:
            req = urllib.request.Request(f"https://img.youtube.com/vi/{vid}/{name}.jpg",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if r.status == 200 and len(data) >= 5000:  # tiny 120x90 image = "no thumbnail"
                with open(dst, "wb") as f:
                    f.write(data)
                return dst
        except Exception as e:
            logger.debug(f"cdn thumb {name} failed: {e}")
    return None


def _frame_thumb(video_path: str, dst: str) -> bool:
    """Fallback 2: grab a frame from the downloaded video (10s, 5s, 1s, then first frame)."""
    for seek in ("10", "5", "1", "0"):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", seek, "-i", video_path, "-vframes", "1",
                 "-vf", "scale=320:-2", "-q:v", "5", dst],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
                return True
        except Exception as e:
            logger.debug(f"frame thumb seek={seek} failed: {e}")
            break
    return False


def make_thumb(src_dir: str, vid: str, video_path=None):
    """Thumbnail chain: yt-dlp's own thumbnail (webp/png/jpg) -> YouTube CDN -> frame from the video."""
    dst = os.path.join(src_dir, "thumb_tg.jpg")
    cands = [c for c in glob.glob(os.path.join(src_dir, f"{vid}.*"))
             if c.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    for src in cands:
        if _thumb_to_telegram(src, dst):
            return dst
    cdn = _download_cdn_thumb(vid, src_dir)
    if cdn and _thumb_to_telegram(cdn, dst):
        return dst
    if video_path and _frame_thumb(video_path, dst):
        return dst
    return None


def probe_media(path: str):
    """(duration_s, width, height) via ffprobe; zeros on failure. Blocking."""
    try:
        import json
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(r.stdout)
        st = (data.get("streams") or [{}])[0]
        return (float(data["format"]["duration"]), int(st.get("width") or 0), int(st.get("height") or 0))
    except Exception:
        try:  # audio-only files have no video stream
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=30,
            )
            return float(r.stdout.strip()), 0, 0
        except Exception:
            return 0.0, 0, 0


def _segment(src: str, pattern: str, secs: int) -> list:
    """ffmpeg segment muxer with stream copy (no re-encode: seconds, not minutes)."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-map", "0:v?", "-map", "0:a?", "-c", "copy",
             "-f", "segment", "-segment_time", str(secs), "-reset_timestamps", "1", pattern],
            capture_output=True, timeout=3600,
        )
    except Exception as e:
        logger.warning(f"ffmpeg segment failed: {e}")
        return []
    prefix, ext = pattern.split("%")[0], pattern.rsplit(".", 1)[-1]
    return sorted(glob.glob(f"{prefix}*.{ext}"))


def split_file(src: str, workdir: str, base: str, max_part_bytes: int) -> list:
    """Split src into playable parts each <= max_part_bytes. Deletes src on
    success. If splitting is impossible returns [src] unchanged. Blocking."""
    size = os.path.getsize(src)
    duration, _, _ = probe_media(src)
    ext = src.rsplit(".", 1)[-1]
    if duration <= 0:
        logger.warning(f"split: cannot read duration of {src}")
        return [src]
    secs = max(30, int(max_part_bytes / (size / duration)))
    parts = _segment(src, os.path.join(workdir, f"{base}_part%03d.{ext}"), secs)
    if not parts:
        return [src]

    fixed = []
    for p in parts:  # re-split any part that still landed over budget
        psize = os.path.getsize(p)
        if psize <= max_part_bytes:
            fixed.append(p)
            continue
        pdur, _, _ = probe_media(p)
        if pdur <= 0:
            fixed.append(p)
            continue
        tighter = max(10, int(pdur * (max_part_bytes / psize) * 0.9))
        subs = _segment(p, p[: -(len(ext) + 1)] + f"_sub%03d.{ext}", tighter)
        if subs and all(os.path.getsize(s) <= max_part_bytes for s in subs):
            os.remove(p)
            fixed.extend(subs)
        else:
            for s in subs:
                try:
                    os.remove(s)
                except OSError:
                    pass
            fixed.append(p)
    try:
        os.remove(src)
    except OSError:
        pass
    return sorted(fixed)


_TRANSIENT_MARKERS = (
    "timed out", "connection", "reset by peer", "http error 5", "incomplete read", "remote end closed",
    "temporary failure", "unable to download", "fragment", "network is unreachable", "ssl", "eof occurred",
)


def _is_transient_error(err: str) -> bool:
    """Worth another attempt? Only flaky-network errors — never a 429/sign-in wall, a cancel or a watchdog abort."""
    low = (err or "").lower()
    if yt_auth.is_hard_youtube_block(err) or "cancelled" in low:
        return False
    return any(m in low for m in _TRANSIENT_MARKERS)


def _download_with_retry(opts: dict, url: str):
    """extract_info(download=True) with exponential backoff on transient errors. yt-dlp resumes .part files."""
    for attempt in range(DL_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as e:
            full = _err_with_diag(e, opts)
            if attempt < DL_RETRIES and _is_transient_error(full):
                wait = 2 ** attempt
                logger.info(f"download attempt {attempt + 1} failed ({full[:100]}) - retry in {wait}s")
                time.sleep(wait)
                continue
            note_youtube_error(full)
            raise RuntimeError(full) from e


def download_blocking(url, mode, value, vertical, workdir, hook, clients=None):
    """mode: 'v' (value = height) or 'a' (value = mp3 bitrate)."""
    check_cooldown()
    opts = {
        **base_opts(clients),
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "writethumbnail": True,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
    }
    if mode == "a":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(value)}
        ]
    else:
        opts["merge_output_format"] = "mp4"

    info = None
    if mode == "v" and int(value) >= 720:
        # Try to really get the requested height: strict selector across the client sets
        # (web+PO token first). If no client has it, fall through to the normal selector.
        tried = []
        for cs in ([clients] if clients else []) + client_sets():
            if cs in tried:
                continue
            tried.append(cs)
            o = {**opts, **base_opts(cs), "format": strict_selector(int(value), vertical)}
            try:
                info = _download_with_retry(o, url)
                break
            except yt_dlp.utils.DownloadCancelled:
                raise
            except Exception as e:
                logger.info(f"strict {value}p via {cs} failed: {str(e)[:140]}")
                if note_youtube_error(str(e)):
                    break
    if info is None:
        opts["format"] = format_selector(int(value), vertical) if mode == "v" else opts["format"]
        info = _download_with_retry(opts, url)

    vid = info.get("id")
    ext = "mp3" if mode == "a" else "mp4"
    path = os.path.join(workdir, f"{vid}.{ext}")
    if not os.path.isfile(path):  # e.g. merge fell back to mkv/webm
        skip = (".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp")
        files = [
            p for p in glob.glob(os.path.join(workdir, f"{vid}.*"))
            if not p.lower().endswith(skip)
        ]
        if not files:
            raise RuntimeError("Download finished but output file was not found.")
        path = max(files, key=os.path.getsize)
    thumb = make_thumb(workdir, vid, path if mode == "v" else None)
    return path, thumb, info


# ---------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------
async def safe_edit(msg: Message, text: str, markup=None):
    try:
        await msg.edit_text(
            text, reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except MessageNotModified:
        pass
    except FloodWait:
        pass
    except Exception as e:  # message deleted etc.
        logger.debug(f"edit failed: {e}")


def friendly_error(err: str) -> str:
    err = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", err or "").strip()
    low = err.lower()
    if yt_auth.is_rate_limit_error(err):
        left = yt_auth.cooldown_remaining()
        wait = f" ~{yt_auth.human_time_short(left)} baad" if left else " thodi der baad"
        return f"YouTube ne is server ko rate-limit kar diya hai (HTTP 429). Please{wait} dobara try karo."
    if low.startswith("youtube ne is server ko abhi rate-limit"):
        return err[:300]
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube ne bot-check laga diya. Admin ko cookies set karni padengi."
    if "private video" in low:
        return "Ye video private hai."
    if "age" in low and "restrict" in low:
        return "Ye video age-restricted hai (cookies chahiye)."
    if "unavailable" in low or "removed" in low:
        return "Ye video available nahi hai."
    if "live" in low and "not supported" in low:
        return "Live streams supported nahi hai."
    return err[:300] or "Unknown error"


# ---------------------------------------------------------------------
# Handlers — start / help / about / support / cancel
# ---------------------------------------------------------------------
_TG_POST_RE = re.compile(r"^https?://t\.me/(?:(c)/(\d+)|([A-Za-z0-9_]+))/(\d+)/?(?:\?.*)?$")


def banner_source() -> str:
    """Admin-set banner (file_id or URL) wins over START_PHOTO_URL."""
    return DB.get("banner_file_id") or DB.get("banner_url") or ""


async def send_start_photo(client: Client, chat_id: int, caption: str, src: str) -> bool:
    """Send the start photo with the welcome text as caption (below the photo).
    src = direct image URL, a Telegram post link, or a Telegram file_id."""
    src = (src or "").strip()
    if not src:
        return False
    tg = _TG_POST_RE.match(src)
    if tg:
        # Telegram post link -> copy that photo and attach the welcome caption
        private_id, chan_id, username, msg_id = tg.groups()
        chat = int(f"-100{chan_id}") if private_id else username
        await client.copy_message(
            chat_id, chat, int(msg_id),
            caption=caption, reply_markup=fallback_keyboard(),
        )
    else:
        await client.send_photo(chat_id, src, caption=caption, reply_markup=fallback_keyboard())
    return True


async def send_welcome(client: Client, chat_id: int, first_name: str, uid: int):
    me = await client.get_me()
    caption = build_welcome(first_name or "there", me.username or "", me.first_name or "YouTube Downloader")
    sent = False
    for src in (banner_source(), START_PHOTO_URL):
        if not (src or "").strip():
            continue
        try:
            if await send_start_photo(client, chat_id, caption, src):
                sent = True
                break
        except Exception as e:
            logger.warning(f"start photo failed ({src[:40]}): {e}")
    if not sent:
        await client.send_message(
            chat_id, caption, reply_markup=fallback_keyboard(),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    await client.send_message(
        chat_id, SC(FALLBACK_TEXT),
        reply_markup=ADMIN_MENU_KB if is_admin(uid) else MAIN_MENU_KB,
    )


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, m: Message):
    u = m.from_user
    WAITING.pop(u.id, None)  # /start aborts any pending admin input
    is_new = register_user(u.id, u.username, full_name(u))
    if is_new:
        await process_referral(client, m)
        log_event(f"🆕 <b>New User</b>\n\n👤 {_utag(u.id)}\n👥 Total users: <code>{len(DB['users'])}</code>")

    if OWNER_ID and u.id != OWNER_ID:
        try:
            uname = f"@{u.username}" if u.username else "(no username)"
            await client.send_message(
                OWNER_ID,
                f"🆕 <b>/start</b>\n👤 {esc(u.first_name)} {uname}\n🆔 <code>{u.id}</code>",
            )
        except Exception:
            pass

    if not await check_subscription(client, u.id):
        return await send_photo_or_text(client, m.chat.id, FORCE_SUB_PHOTO_URL, ACCESS_DENIED_TEXT, subscription_keyboard())
    await send_welcome(client, m.chat.id, u.first_name, u.id)


@app.on_message((filters.command("help") | menu_text_filter(BTN_HELP)) & filters.private)
async def help_handler(client: Client, m: Message):
    await m.reply(HELP_TEXT, link_preview_options=LinkPreviewOptions(is_disabled=True))


@app.on_message((filters.command("about") | menu_text_filter(BTN_ABOUT)) & filters.private)
async def about_handler(client: Client, m: Message):
    kb = InlineKeyboardMarkup([[make_button(SC("❌ Close"), "about_close", style=BTN_DANGER)]])
    await m.reply(build_about(), reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))


@app.on_message(menu_text_filter(BTN_SUPPORT) & filters.private)
async def support_handler(client: Client, m: Message):
    await m.reply(SUPPORT_TEXT, link_preview_options=LinkPreviewOptions(is_disabled=True))


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, m: Message):
    if WAITING.pop(m.from_user.id, None):  # pending admin input
        return await m.reply(SC("<b>✅ Operation cancelled!</b>"))
    job = ACTIVE.get(m.from_user.id)
    if not job:
        return await m.reply(SC("<b>⚠️ No active download to cancel.</b>"))
    job["cancel"].set()
    if job["phase"] != "download":  # queue / upload phase -> cancel the asyncio task
        job["task"].cancel()
    await m.reply(SC("<b>🛑 Cancelling your active download...</b>"))


@app.on_callback_query(filters.regex(r"^about_close$"))
async def about_close_cb(client, query):
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


@app.on_callback_query(filters.regex(r"^fallback_download$"))
async def fallback_download_cb(client, query):
    await query.answer(SC(FALLBACK_TEXT), show_alert=True)


@app.on_callback_query(filters.regex(r"^fallback_help$"))
async def fallback_help_cb(client, query):
    await query.message.reply(HELP_TEXT, link_preview_options=LinkPreviewOptions(is_disabled=True))
    await query.answer()


# ---------------------------------------------------------------------
# Force-join channels, access gates, /stats  (ported from dl.py)
# ---------------------------------------------------------------------
def extract_channel_info(text: str):
    """-> ("public", username) | ("private", invite_hash) | ("id", -100…chat_id) | (None, None)"""
    text = (text or "").strip()
    if re.fullmatch(r"-100\d{5,}", text):
        return "id", int(text)
    if re.search(r"(?:t|telegram)\.me/", text):
        path = urlparse(text if "://" in text else "https://" + text).path.strip("/")
        if path.startswith("+"):
            return "private", path[1:]
        if path.startswith("joinchat/"):
            return "private", path.split("/", 1)[1]
        if path:
            return "public", path.split("/")[0]
        return None, None
    if re.fullmatch(r"@?[A-Za-z]\w{3,31}", text):
        return "public", text.lstrip("@")
    return None, None


def ch_label(ch: dict) -> str:
    t = ch.get("type", "public")
    if t == "public":
        return f"@{ch['identifier']}"
    if t == "id":
        return ch.get("title") or str(ch.get("chat_id"))
    return f"Private ({ch.get('invite_hash', '')[:8]}...)"


async def build_id_entry(client: Client, chat_id: int):
    """Resolve a -100… channel ID -> (entry, error). Needs the bot to be admin there."""
    try:
        chat = await client.get_chat(chat_id)
    except Exception as e:
        return None, f"channel nahi mila: {str(e)[:100]}"
    try:
        me_member = await client.get_chat_member(chat_id, "me")
        if me_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return None, "bot is channel mein admin nahi hai"
    except Exception as e:
        return None, f"admin check failed: {str(e)[:100]}"
    link = None
    if getattr(chat, "username", None):
        link = f"https://t.me/{chat.username}"
    else:
        try:
            link = await client.export_chat_invite_link(chat_id)
        except Exception as e:
            logger.warning(f"invite link for {chat_id} failed: {e} — bot ko 'Invite Users' permission do")
    return {"type": "id", "chat_id": chat_id, "title": getattr(chat, "title", None) or str(chat_id),
            "invite_link": link, "added_date": datetime.now().strftime("%Y-%m-%d %H:%M")}, None


async def apply_env_force_channels(client: Client):
    """Add channels listed in FORCE_SUB to the force-join list, once, at startup."""
    added = 0
    for raw in re.split(r"[,\s]+", FORCE_SUB_RAW.strip()):
        if not raw:
            continue
        ch_type, ident = extract_channel_info(raw)
        if not ch_type:
            logger.warning(f"FORCE_SUB: '{raw}' samajh nahi aaya — skipped")
            continue
        if ch_type == "id":
            old = next((c for c in FORCE_CHANNELS if c.get("type") == "id" and c.get("chat_id") == ident), None)
            if old and old.get("invite_link"):
                continue
            entry, err = await build_id_entry(client, ident)
            if err:
                logger.warning(f"FORCE_SUB: {ident}: {err}")
                if not entry:
                    continue
            entry["env"] = True
            if old:
                old.update(entry)
            else:
                FORCE_CHANNELS.append(entry)
            added += 1
            continue
        key = "identifier" if ch_type == "public" else "invite_hash"
        if any(c.get("type", "public") == ch_type and c.get(key) == ident for c in FORCE_CHANNELS):
            continue
        if ch_type == "public":
            try:
                me_member = await client.get_chat_member(f"@{ident}", "me")
                if me_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                    logger.warning(f"FORCE_SUB: bot @{ident} mein admin nahi hai — join check kaam nahi karega")
            except Exception as e:
                logger.warning(f"FORCE_SUB: @{ident} check failed: {e}")
        FORCE_CHANNELS.append({"type": ch_type, key: ident, "added_date": datetime.now().strftime("%Y-%m-%d %H:%M"), "env": True})
        added += 1
    if added:
        save_data()
        reset_verifications()
        logger.info(f"FORCE_SUB: {added} channel(s) added")


def subscription_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for ch in FORCE_CHANNELS:
        if ch.get("type", "public") == "public":
            rows.append([make_button(f"📢 Join @{ch['identifier']}", url=f"https://t.me/{ch['identifier']}", style=BTN_PRIMARY)])
        elif ch.get("type") == "id":
            if ch.get("invite_link"):
                rows.append([make_button(f"📢 Join {ch_label(ch)}", url=ch["invite_link"], style=BTN_PRIMARY)])
        else:
            rows.append([make_button("📢 Join Private Channel", url=f"https://t.me/+{ch['invite_hash']}", style=BTN_PRIMARY)])
    rows.append([make_button(SC("✅ Verify Join"), "vsub", style=BTN_SUCCESS)])
    rows.append([make_button(SC("🔄 Refresh"), "vsub", style=BTN_PRIMARY)])
    return InlineKeyboardMarkup(rows)


async def check_subscription(client: Client, user_id: int) -> bool:
    """True if the user joined every force channel (or none are configured).
    Private invite-link channels can't be checked, so they are trusted on Verify."""
    if not FORCE_CHANNELS or is_verified(user_id):
        return True
    for ch in FORCE_CHANNELS:
        if ch.get("type", "public") == "private":
            continue
        target = f"@{ch['identifier']}" if ch.get("type", "public") == "public" else ch["chat_id"]
        try:
            member = await client.get_chat_member(target, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
            if member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", True):
                return False
        except UserNotParticipant:
            return False
        except Exception as e:
            logger.error(f"membership check failed for {target}: {e}")
            return False
    set_verified(user_id, True)
    return True


async def gate_message(client: Client, m: Message) -> bool:
    """Register user, then enforce maintenance mode + force-join. True = may proceed."""
    u = m.from_user
    register_user(u.id, u.username, full_name(u))
    if DB["maintenance_mode"] and not is_admin(u.id):
        await m.reply(MAINTENANCE_TEXT)
        return False
    if not await check_subscription(client, u.id):
        await send_photo_or_text(client, m.chat.id, FORCE_SUB_PHOTO_URL, ACCESS_DENIED_TEXT, subscription_keyboard())
        return False
    return True


async def gate_query(client: Client, query) -> bool:
    u = query.from_user
    register_user(u.id, u.username, full_name(u))
    if DB["maintenance_mode"] and not is_admin(u.id):
        await query.answer(SC("Bot maintenance mein hai. Thodi der baad try karo."), show_alert=True)
        return False
    if not await check_subscription(client, u.id):
        await query.answer(SC("Pehle required channels join karo — /start dabao."), show_alert=True)
        return False
    return True


@app.on_callback_query(filters.regex(r"^vsub$"))
async def verify_sub_cb(client: Client, query):
    u = query.from_user
    register_user(u.id, u.username, full_name(u))
    if await check_subscription(client, u.id):
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer(SC("✅ Verified!"))
        await send_welcome(client, query.message.chat.id, u.first_name, u.id)
    else:
        await query.answer(SC("❌ Abhi join nahi kiya! Sab channels join karke Verify dabao."), show_alert=True)


@app.on_message((filters.command("stats") | menu_text_filter(BTN_STATS)) & filters.private)
async def stats_handler(client: Client, m: Message):
    u = m.from_user
    register_user(u.id, u.username, full_name(u))
    info = DB["users"].get(str(u.id), {})
    try:
        joined = datetime.fromisoformat(info["join_date"]).strftime("%Y-%m-%d")
    except Exception:
        joined = "—"
    await m.reply(
        SC("📊 <b>Your Statistics</b>\n\n")
        + f"📅 {smallcaps('Joined')}: {joined}\n"
        f"📥 {smallcaps('Downloads')}: {info.get('total_downloads', 0)}\n"
        f"🆔 {smallcaps('User ID')}: <code>{u.id}</code>\n"
        f"👤 {smallcaps('Username')}: {('@' + u.username) if u.username else 'None'}\n"
        f"✅ {smallcaps('Verified')}: {'Yes' if info.get('verified') else 'No'}\n"
        f"💎 {smallcaps('Plan')}: {plan_summary(u.id)}"
    )


# ---------------------------------------------------------------------
# Admin panel  (ported from dl.py: broadcast, stats, maintenance, force
# channels, users list, banner, reset verifications, CSV export)
# ---------------------------------------------------------------------
def _fmt_dt(iso, fmt="%Y-%m-%d %H:%M"):
    try:
        return datetime.fromisoformat(iso).strftime(fmt)
    except Exception:
        return "—"


def panel_content():
    users, downloads, active, uptime = get_stats()
    verified = sum(1 for u in DB["users"].values() if u.get("verified"))
    if FORCE_CHANNELS:
        lines = []
        for i, ch in enumerate(FORCE_CHANNELS, 1):
            if ch.get("type", "public") == "public":
                lines.append(f"{i}. @{esc(ch['identifier'])} (Public)")
            elif ch.get("type") == "id":
                lines.append(f"{i}. {esc(ch_label(ch))} (ID)")
            else:
                lines.append(f"{i}. Private Channel ({esc(ch['invite_hash'][:8])}...)")
        ch_text = "\n".join(lines)
    else:
        ch_text = "No channels configured"
    banner = bool(DB.get("banner_file_id") or DB.get("banner_url"))
    text = (
        "👑 <b>Admin Panel</b>\n\n"
        "📊 <b>Statistics:</b>\n"
        f"• Users: {users:,}\n• Verified: {verified:,}\n• Premium: {premium_count():,}\n• Downloads: {downloads:,}\n"
        f"• Active Today: {active}\n• Uptime: {uptime}\n\n"
        "🔧 <b>Settings:</b>\n"
        f"• Maintenance: {'🔴 ON' if DB['maintenance_mode'] else '🟢 OFF'}\n"
        f"• Force Channels: {len(FORCE_CHANNELS)}\n"
        f"• Banner: {'✅ Set' if banner else '❌ Not Set'}\n\n"
        f"📢 <b>Force Channels:</b>\n{ch_text}"
    )
    mb = make_button
    kb = InlineKeyboardMarkup([
        [mb("📢 Broadcast", "adm|bcast", style=BTN_PRIMARY), mb("📊 Stats", "adm|stats", style=BTN_PRIMARY)],
        [mb("🔧 Maintenance", "adm|maint", style=BTN_DANGER), mb("➕ Add Channel", "adm|addch", style=BTN_SUCCESS)],
        [mb("➖ Remove Channel", "adm|rmch", style=BTN_DANGER), mb("📋 Channels List", "adm|chlist", style=BTN_PRIMARY)],
        [mb("👥 Users List", "adm|users", style=BTN_PRIMARY), mb("🖼️ Set Banner", "adm|banner", style=BTN_SUCCESS)],
        [mb("🔄 Reset Verifications", "adm|reset", style=BTN_DANGER), mb("📊 Export Users", "adm|export", style=BTN_SUCCESS)],
        [mb("🍪 YouTube Auth", "adm|auth", style=BTN_PRIMARY)],
        [mb("❌ Close", "adm|close", style=BTN_DANGER)],
    ])
    return text, kb


async def send_panel(client: Client, chat_id: int):
    text, kb = panel_content()
    await client.send_message(chat_id, text, reply_markup=kb)


@app.on_message((filters.command("admin") | menu_text_filter(BTN_ADMIN)) & filters.private)
async def admin_panel_handler(client: Client, m: Message):
    if not is_admin(m.from_user.id):
        return await m.reply("⛔ <b>Unauthorized Access!</b>\nThis command is for admins only.")
    WAITING.pop(m.from_user.id, None)
    await send_panel(client, m.chat.id)


def _back_kb(label="🔙 Back"):
    return InlineKeyboardMarkup([[make_button(label, "adm|panel", style=BTN_DANGER)]])


@app.on_callback_query(filters.regex(r"^adm\|"))
async def admin_cb(client: Client, query):
    uid = query.from_user.id
    if not is_admin(uid):
        return await query.answer("⛔ Unauthorized!", show_alert=True)
    parts = query.data.split("|")
    act, msg = parts[1], query.message

    async def show_panel():
        text, kb = panel_content()
        await safe_edit(msg, text, kb)

    if act == "panel":
        WAITING.pop(uid, None)
        await show_panel()

    elif act == "close":
        WAITING.pop(uid, None)
        try:
            await msg.delete()
        except Exception:
            pass

    elif act == "stats":
        users, downloads, active, uptime = get_stats()
        verified = sum(1 for u in DB["users"].values() if u.get("verified"))
        top = sorted(DB["users"].values(), key=lambda x: x.get("total_downloads", 0), reverse=True)[:5]
        top_text = "\n".join(
            f"• {('@' + esc(u['username'])) if u.get('username') else esc(u.get('full_name') or 'No username')}: "
            f"{u.get('total_downloads', 0)} downloads {'✅' if u.get('verified') else '❌'}"
            for u in top
        ) or "No data yet"
        await safe_edit(
            msg,
            "📊 <b>Detailed Statistics</b>\n\n"
            f"👥 <b>Total Users:</b> {users:,}\n✅ <b>Verified Users:</b> {verified:,}\n"
            f"📥 <b>Total Downloads:</b> {downloads:,}\n📅 <b>Active Today:</b> {active}\n"
            f"⏱️ <b>Uptime:</b> {uptime}\n\n🏆 <b>Top Users:</b>\n{top_text}",
            _back_kb(),
        )

    elif act == "maint":
        DB["maintenance_mode"] = not DB["maintenance_mode"]
        save_data()
        await query.answer(f"Maintenance mode {'enabled' if DB['maintenance_mode'] else 'disabled'}!")
        await show_panel()

    elif act == "addch":
        WAITING[uid] = "add_channel"
        await safe_edit(
            msg,
            "📢 <b>Add Force Channel</b>\n\nSend me the channel <b>link</b> or <b>username</b>:\n\n"
            "• Public channel: <code>@username</code> or <code>https://t.me/username</code>\n"
            "• Private channel: <code>https://t.me/+invitehash</code>\n\n"
            "⚠️ <b>Important:</b> I must be an admin in the channel!\n"
            "Type /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    elif act == "rmch":
        if not FORCE_CHANNELS:
            return await query.answer("No channels to remove!", show_alert=True)
        rows = []
        for i, ch in enumerate(FORCE_CHANNELS):
            name = ch_label(ch)
            rows.append([make_button(f"❌ Remove {name}", f"adm|rm|{i}", style=BTN_DANGER)])
        rows.append([make_button("🔙 Back", "adm|panel", style=BTN_PRIMARY)])
        await safe_edit(msg, "📢 <b>Remove Force Channel</b>\n\nSelect channel to remove:", InlineKeyboardMarkup(rows))

    elif act == "rm":
        idx = int(parts[2])
        if 0 <= idx < len(FORCE_CHANNELS):
            removed = FORCE_CHANNELS.pop(idx)
            save_data()
            reset_verifications()
            name = ch_label(removed)
            await query.answer(f"Removed {name}!")
        await show_panel()

    elif act == "chlist":
        if not FORCE_CHANNELS:
            return await query.answer("No channels configured!", show_alert=True)
        text = "📢 <b>Force Channels List</b>\n\n"
        for i, ch in enumerate(FORCE_CHANNELS, 1):
            if ch.get("type", "public") == "public":
                text += f"{i}. Public: @{esc(ch['identifier'])}\n"
            elif ch.get("type") == "id":
                text += f"{i}. ID: <code>{esc(ch['chat_id'])}</code> — {esc(ch_label(ch))}\n"
            else:
                text += f"{i}. Private: <code>{esc(ch['invite_hash'])}</code>\n"
            text += f"   Added: {esc(ch.get('added_date', 'Unknown'))}\n\n"
        await safe_edit(msg, text, _back_kb())

    elif act == "users":
        total = len(DB["users"])
        verified = sum(1 for u in DB["users"].values() if u.get("verified"))
        text = f"👥 <b>Users List (Total: {total}, Verified: {verified})</b>\n\n<b>Last 10 Active Users:</b>\n"
        recent = sorted(DB["users"].values(), key=lambda x: x.get("last_active", ""), reverse=True)[:10]
        for u in recent:
            uname = f"@{esc(u['username'])}" if u.get("username") else "No username"
            text += (
                f"• {'✅' if u.get('verified') else '❌'} {uname} - {u.get('total_downloads', 0)} dl\n"
                f"  {esc((u.get('full_name') or 'Unknown')[:20])}\n"
                f"  Last: {_fmt_dt(u.get('last_active'))}\n\n"
            )
        await safe_edit(msg, text[:4000], _back_kb())

    elif act == "banner":
        WAITING[uid] = "set_banner"
        await safe_edit(
            msg,
            "🖼️ <b>Set Banner Image</b>\n\nSend me the new banner image or image URL:\n"
            "• Send a photo directly\n• Or send an image URL\n\nType /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    elif act == "reset":
        reset_verifications()
        await query.answer("All verifications reset!")
        await show_panel()

    elif act == "export":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["User ID", "Username", "Full Name", "Join Date", "Last Active", "Downloads", "Verified"])
        for u_id, u in DB["users"].items():
            w.writerow([u_id, u.get("username", ""), u.get("full_name", ""), u.get("join_date", ""),
                        u.get("last_active", ""), u.get("total_downloads", 0), u.get("verified", False)])
        bio = io.BytesIO(out.getvalue().encode("utf-8"))
        bio.name = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        await client.send_document(msg.chat.id, bio, caption="📊 Users Export")
        await query.answer("Export sent!")

    elif act == "auth":
        await safe_edit(msg, auth_panel_text(), _back_kb())

    elif act == "bcast":
        WAITING[uid] = "bcast"
        await safe_edit(
            msg,
            "📢 <b>Broadcast Message</b>\n\nSend me the message to broadcast to all users:\n"
            "• Text, photo, video, document or audio\n• Telegram formatting is kept\n\n"
            f"Total users: {len(DB['users']):,}\n\nType /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    await query.answer()


def _waiting_filter(_, __, m: Message) -> bool:
    if not m.from_user or m.from_user.id not in WAITING:
        return False
    if m.text and (m.text.startswith("/") or _is_menu_text(m.text)):
        return False  # commands / menu buttons are handled normally
    return True


@app.on_message(filters.private & filters.create(_waiting_filter), group=-1)
async def admin_input_handler(client: Client, m: Message):
    """Next message from an admin who pressed Add Channel / Set Banner / Broadcast."""
    uid = m.from_user.id
    action = WAITING.get(uid)

    if action == "add_channel":
        ch_type, ident = extract_channel_info(m.text or m.caption or "")
        if not ch_type:
            await m.reply("❌ <b>Invalid channel format!</b>\n\nSend a valid channel link or username.\n"
                          "Example: <code>@channel</code>, <code>https://t.me/+invite</code> or <code>-100xxxxxxxxxx</code>")
            return m.stop_propagation()  # keep waiting
        key = {"public": "identifier", "private": "invite_hash", "id": "chat_id"}[ch_type]
        if any(c.get("type", "public") == ch_type and c.get(key) == ident for c in FORCE_CHANNELS):
            await m.reply("❌ <b>Channel already exists!</b>")
        elif ch_type == "id":
            entry, err = await build_id_entry(client, ident)
            if err and not entry:
                await m.reply(f"❌ <b>Channel add nahi hua!</b>\n\n{esc(err)}\nBot ko channel mein admin banao (Invite Users permission ke saath).")
            else:
                FORCE_CHANNELS.append(entry)
                save_data()
                reset_verifications()
                await m.reply(f"✅ <b>{esc(ch_label(entry))} added!</b>\n\nUsers now need to join it to use the bot.")
        else:
            entry = {"type": ch_type, key: ident, "added_date": datetime.now().strftime("%Y-%m-%d %H:%M")}
            if ch_type == "public":
                try:
                    chat = await client.get_chat(f"@{ident}")
                    me_member = await client.get_chat_member(chat.id, "me")
                    if me_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                        FORCE_CHANNELS.append(entry)
                        save_data()
                        reset_verifications()  # so existing users must join the new channel too
                        await m.reply(f"✅ <b>Channel @{esc(ident)} added!</b>\n\n"
                                      "Users now need to join it to use the bot.")
                    else:
                        await m.reply(f"❌ <b>I'm not an admin in @{esc(ident)}!</b>\n\nMake me admin first, then try again.")
                except Exception as e:
                    await m.reply(f"❌ <b>Error accessing channel!</b>\n\nMake sure I am admin in @{esc(ident)}\n"
                                  f"Error: {esc(str(e)[:100])}")
            else:
                FORCE_CHANNELS.append(entry)
                save_data()
                reset_verifications()
                await m.reply("✅ <b>Private channel added!</b>\n\n"
                              "Note: private channels can't be auto-checked — users are trusted on ✅ Verify.")
        WAITING.pop(uid, None)
        await send_panel(client, m.chat.id)

    elif action == "set_banner":
        if m.photo:
            DB["banner_file_id"], DB["banner_url"] = m.photo.file_id, None
            await m.reply("✅ <b>Banner image updated!</b>")
        elif m.text and m.text.startswith(("http://", "https://")):
            DB["banner_url"], DB["banner_file_id"] = m.text.strip(), None
            await m.reply("✅ <b>Banner URL updated!</b>")
        else:
            await m.reply("❌ <b>Invalid input!</b>\n\nSend a photo or a valid image URL.")
            return m.stop_propagation()  # keep waiting
        save_data()
        WAITING.pop(uid, None)
        await send_welcome(client, m.chat.id, m.from_user.first_name, uid)  # preview
        await send_panel(client, m.chat.id)

    elif action == "bcast":
        ids = [int(x) for x in DB["users"]]
        total, ok, fail = len(ids), 0, 0
        proc = await m.reply(f"📤 <b>Broadcasting...</b>\nProgress: 0/{total}")
        for i, tid in enumerate(ids, 1):
            try:
                await m.copy(tid)
                ok += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                try:
                    await m.copy(tid)
                    ok += 1
                except Exception:
                    fail += 1
            except Exception as e:
                logger.info(f"broadcast to {tid} failed: {e}")
                fail += 1
            if i % 10 == 0 or i == total:
                await safe_edit(proc, f"📤 <b>Broadcasting...</b>\nProgress: {i}/{total}")
            await asyncio.sleep(0.05)  # stay under flood limits
        try:
            await proc.delete()
        except Exception:
            pass
        await m.reply(f"📊 <b>Broadcast Complete</b>\n\n✅ <b>Success:</b> {ok:,}\n"
                      f"❌ <b>Failed:</b> {fail:,}\n📢 <b>Total Users:</b> {total:,}")
        WAITING.pop(uid, None)
        await send_panel(client, m.chat.id)

    else:
        WAITING.pop(uid, None)
    m.stop_propagation()


# ---------------------------------------------------------------------
# YouTube search  (ported from fbot's ytsearch.py)
#   /search <query>  or  /yts <query>   — paginated results, tap a number
#   plain text that isn't a link        — "yt <q>", "<q> on youtube", or just <q>
# ---------------------------------------------------------------------
SEARCH_CACHE: dict[str, dict] = {}  # key -> {query, results, exhausted, user_id}
_URL_HINT = re.compile(r"https?://|www\.|t\.me/|magnet:\?", re.I)
_YT_TRIGGER = r"(?:yt|yts|youtube)"
_YT_SEARCH_PATTERNS = [
    re.compile(rf"(?i)^\s*{_YT_TRIGGER}\s+search\s+(.+)$"),
    re.compile(rf"(?i)^\s*search\s+(.+?)\s+on\s+{_YT_TRIGGER}\s*$"),
    re.compile(rf"(?i)^\s*{_YT_TRIGGER}\s+(.+)$"),
    re.compile(rf"(?i)^\s*(.+?)\s+on\s+{_YT_TRIGGER}\s*$"),
    re.compile(r"(?i)^\s*search\s+(.+)$"),
]


def extract_search_query(text: str):
    """Plain (non-link) text -> search query, or None if it isn't searchable."""
    text = (text or "").strip()
    if not text or text.startswith("/") or _URL_HINT.search(text):
        return None
    query = text
    for pat in _YT_SEARCH_PATTERNS:
        m = pat.match(text)
        if m and m.group(1).strip():
            query = m.group(1).strip()
            break
    return query if 2 <= len(query) <= 120 else None


def search_youtube(query: str, count: int) -> list:
    """Flat metadata-only search (fast, no per-video fetch). Blocking."""
    check_cooldown()
    opts = {**base_opts(), "noplaylist": False, "extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    except Exception as e:
        full = _err_with_diag(e, opts)
        note_youtube_error(full)
        raise RuntimeError(full) from e
    out = []
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        dur = e.get("duration")
        out.append({
            "id": e["id"],
            "title": (e.get("title") or "Untitled")[:70],
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": hms(dur) if isinstance(dur, (int, float)) and dur > 0 else "",
        })
    return out


def search_text(query: str, results: list, page: int, exhausted: bool) -> str:
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(results))
    lines = [SC("🔍 <b>YouTube Search:</b> ") + f"<i>{esc(query)}</i>\n"]
    for i, r in enumerate(results[start:end], start=start + 1):
        meta = " — ".join(x for x in (r["uploader"], r["duration"]) if x)
        lines.append(f"{i}. {esc(r['title'])}" + (f"\n    <i>{esc(meta)}</i>" if meta else ""))
    if not (exhausted and end >= len(results)):
        lines.append("\n" + SC("Tap a number to download."))
    return "\n".join(lines)


def search_kb(results: list, page: int, exhausted: bool, key: str) -> InlineKeyboardMarkup:
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(results))
    rows, row = [], []
    for i in range(start, end):
        row.append(make_button(str(i + 1), f"ys|{i}|{key}", style=BTN_PRIMARY))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(make_button("◀️ Prev", f"yp|{page - 1}|{key}", style=BTN_PRIMARY))
    nav.append(make_button(f"Page {page + 1}", "ys|n|x", style=BTN_PRIMARY))
    if not exhausted or end < len(results):
        nav.append(make_button("Next ▶️", f"yp|{page + 1}|{key}", style=BTN_PRIMARY))
    rows.append(nav)
    rows.append([make_button(SC("❌ Close"), f"x|{key}", style=BTN_DANGER)])
    return InlineKeyboardMarkup(rows)


async def do_search(m: Message, uid: int, query: str):
    status = await m.reply(SC("🔍 <b>Searching YouTube...</b>"))
    try:
        results = await loop.run_in_executor(None, search_youtube, query, SEARCH_CHUNK_SIZE)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Search failed</b>\n\n") + esc(friendly_error(str(e))))
    if not results:
        return await safe_edit(status, SC("❌ <b>No results for:</b> ") + f"<i>{esc(query)}</i>")
    key = uuid.uuid4().hex[:12]
    exhausted = len(results) < SEARCH_CHUNK_SIZE
    SEARCH_CACHE[key] = {"query": query, "results": results, "exhausted": exhausted, "user_id": uid}
    while len(SEARCH_CACHE) > 500:  # keep memory bounded
        SEARCH_CACHE.pop(next(iter(SEARCH_CACHE)), None)
    await safe_edit(status, search_text(query, results, 0, exhausted), search_kb(results, 0, exhausted, key))


@app.on_message(filters.command(["search", "yts"]) & filters.private)
async def search_cmd(client: Client, m: Message):
    if not await gate_message(client, m):
        return
    if len(m.command) < 2:
        return await m.reply(SC(
            "🔍 <b>Usage:</b> <code>/search song or video name</code>\n"
            "e.g. <code>/search Believer Imagine Dragons</code>"
        ))
    if not await cooldown_ok(m):
        return
    await do_search(m, m.from_user.id, m.text.split(None, 1)[1].strip())


@app.on_callback_query(filters.regex(r"^ys\|"))
async def search_select_cb(client: Client, query):
    _, arg, key = query.data.split("|")
    if arg == "n":  # "Page N" label button
        return await query.answer()
    if not await gate_query(client, query):
        return
    cached = SEARCH_CACHE.get(key)
    if not cached:
        return await query.answer(SC("⌛ Search expire ho gaya — dobara search karo."), show_alert=True)
    if cached["user_id"] != query.from_user.id:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    idx = int(arg)
    if not 0 <= idx < len(cached["results"]):
        return await query.answer(SC("Invalid selection."), show_alert=True)
    await query.answer(SC("⏳ Loading qualities..."))  # answer first: fetching info can be slow
    url = f"https://www.youtube.com/watch?v={cached['results'][idx]['id']}"
    await show_video_menu(query.message, query.from_user.id, url)


@app.on_callback_query(filters.regex(r"^yp\|"))
async def search_page_cb(client: Client, query):
    _, p, key = query.data.split("|")
    page = int(p)
    if not await gate_query(client, query):
        return
    cached = SEARCH_CACHE.get(key)
    if not cached:
        return await query.answer(SC("⌛ Search expire ho gaya — dobara search karo."), show_alert=True)
    if cached["user_id"] != query.from_user.id:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    results, exhausted = cached["results"], cached["exhausted"]

    if not exhausted and page * SEARCH_PAGE_SIZE >= len(results):
        await query.answer(SC("⏳ Loading more results..."))  # a callback can be answered only once
        want = min(len(results) + SEARCH_CHUNK_SIZE, SEARCH_MAX)
        try:
            more = await loop.run_in_executor(None, search_youtube, cached["query"], want)
        except Exception as e:
            logger.warning(f"search 'load more' failed: {e}")
            return
        seen, added = {r["id"] for r in results}, 0
        for r in more:
            if r["id"] not in seen:
                results.append(r)
                seen.add(r["id"])
                added += 1
        if added == 0 or want >= SEARCH_MAX or len(more) < want:
            exhausted = cached["exhausted"] = True
        if page * SEARCH_PAGE_SIZE >= len(results):  # nothing new -> stay on the last page
            page = max(0, (len(results) - 1) // SEARCH_PAGE_SIZE)
    else:
        await query.answer()
    await safe_edit(query.message, search_text(cached["query"], results, page, exhausted),
                    search_kb(results, page, exhausted, key))


# ---------------------------------------------------------------------
# Link handler -> quality menu
# ---------------------------------------------------------------------
def _is_plain_text(_, __, m: Message) -> bool:
    return bool(m.text) and not m.text.startswith("/")


async def cooldown_ok(m: Message) -> bool:
    uid = m.from_user.id
    now_c = time.time()
    wait = COOLDOWN_SECONDS - (now_c - COOLDOWNS.get(uid, 0))
    if wait > 0:
        await m.reply(SC(f"⏳ Please wait {math.ceil(wait)} seconds..."))
        return False
    COOLDOWNS[uid] = now_c
    return True


@app.on_message(filters.private & filters.create(_is_plain_text) & NOT_MENU_BUTTON)
async def link_handler(client: Client, m: Message):
    if not await gate_message(client, m):  # maintenance + force-join
        return
    if not await cooldown_ok(m):
        return
    uid = m.from_user.id

    pl_url = extract_playlist_url(m.text)
    if pl_url:  # pure playlist link
        status = await m.reply(SC("📚 <b>Fetching playlist...</b>"))
        return await send_playlist_menu(client, status, pl_url, uid)

    url = extract_youtube_url(m.text)
    if not url:
        query = extract_search_query(m.text)  # plain text -> YouTube search
        if query:
            return await do_search(m, uid, query)
        return await m.reply(NOT_A_LINK_TEXT, link_preview_options=LinkPreviewOptions(is_disabled=True))
    await show_video_menu(m, uid, url)


async def show_video_menu(m: Message, uid: int, url: str):
    """Fetch one video's info and show the quality picker (links + search results)."""
    status = await m.reply(SC("🔍 <b>Fetching video info...</b>"))
    try:
        info, clients = await loop.run_in_executor(None, fetch_info, url)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Failed to fetch video</b>\n\n") + esc(friendly_error(str(e))))

    if info.get("is_live"):
        return await safe_edit(status, SC("❌ <b>Live streams supported nahi hai.</b>"))

    # drop stale pending requests
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > 3600]:
        PENDING.pop(k, None)

    token = uuid.uuid4().hex[:8]
    vertical = is_vertical(info)
    PENDING[token] = {
        "url": url, "user_id": uid, "ts": now,
        "title": info.get("title") or "video", "vertical": vertical, "clients": clients,
        "playlist_url": playlist_url_from_video_url(url),
    }

    rows, row = [], []
    for h, label in quality_options(info):
        name = {2160: "4K", 1440: "2K"}.get(h, f"{h}p")
        txt = f"🎬 {name}" + (f" • {label}" if label and label != "?" else "")
        row.append(make_button(txt, f"q|{token}|v|{h}", style=BTN_PRIMARY))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        make_button("🎵 MP3 128k", f"q|{token}|a|128", style=BTN_PRIMARY),
        make_button("🎵 MP3 320k", f"q|{token}|a|320", style=BTN_PRIMARY),
    ])
    if PENDING[token]["playlist_url"]:
        rows.append([make_button(SC("📚 Full Playlist"), f"pl|{token}", style=BTN_PRIMARY)])
    rows.append([make_button(SC("❌ Close"), f"x|{token}", style=BTN_DANGER)])

    caption = (
        f"🎬 <b>{esc(info.get('title'))}</b>\n\n"
        f"📺 {smallcaps('Channel')}: {esc(info.get('uploader') or info.get('channel') or '—')}\n"
        f"⏱ {smallcaps('Duration')}: {hms(info.get('duration'))}\n\n"
        f"👇 {smallcaps('Select quality')}"
    )
    markup = InlineKeyboardMarkup(rows)
    thumb_url = info.get("thumbnail")
    try:
        if not thumb_url:
            raise ValueError("no thumbnail")
        await m.reply_photo(thumb_url, caption=caption, reply_markup=markup)
        await status.delete()
    except Exception:
        await safe_edit(status, caption, markup)


PL_LADDER = [(144, "144p"), (240, "240p"), (360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p"), (1440, "2K"), (2160, "4K")]


def _cleanup_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > 3600]:
        PENDING.pop(k, None)


async def send_playlist_menu(client: Client, status: Message, pl_url: str, uid: int):
    try:
        pl = await loop.run_in_executor(None, fetch_playlist, pl_url)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Failed to fetch playlist</b>\n\n") + esc(friendly_error(str(e))))
    if not pl["entries"]:
        return await safe_edit(status, SC("❌ <b>Playlist khaali hai ya private hai.</b>"))

    trim_note = ""
    if (PLAYLIST_FREE_MAX and len(pl["entries"]) > PLAYLIST_FREE_MAX
            and not get_premium_status(uid)["is_premium"]):
        found = len(pl["entries"])
        pl["entries"] = pl["entries"][:PLAYLIST_FREE_MAX]
        trim_note = SC(f"🔒 Free plan: sirf pehle {PLAYLIST_FREE_MAX} videos (playlist mein {found}). "
                       "💎 /plans se Premium lo — poori playlist milegi.\n\n")

    _cleanup_pending()
    token = uuid.uuid4().hex[:8]
    req = {"kind": "playlist", "user_id": uid, "ts": time.time(),
           "entries": pl["entries"], "title": pl["title"]}
    default_q = PLAYLIST_DEFAULT_QUALITY
    default_name = dict(PL_LADDER).get(default_q, f"{default_q}p")

    if PLAYLIST_AUTO_START:
        if uid in ACTIVE:
            return await safe_edit(status, SC("⚠️ <b>Pehle wala download complete hone do, ya <code>/cancel</code> karo.</b>"))
        if await over_daily_limit(client, uid):
            return await safe_edit(status, SC("⚠️ <b>Aaj ki free limit khatam!</b>"))
        await safe_edit(status, SC(f"📚 <b>{esc(pl['title'])}</b>\n\n🎞 Videos: {len(pl['entries'])}\n🎬 Quality: {default_name} (default)\n\n") + trim_note)
        return start_playlist(client, status.chat.id, uid, req, "v", str(default_q))

    PENDING[token] = req
    rows = [[make_button(f"⭐ Start • {default_name} (default)", f"pq|{token}|v|{default_q}", style=BTN_SUCCESS)]]
    row = []
    for h, name in PL_LADDER:
        if h > MAX_HEIGHT or h == default_q:
            continue
        row.append(make_button(f"🎬 {name}", f"pq|{token}|v|{h}", style=BTN_PRIMARY))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        make_button("🎵 MP3 128k", f"pq|{token}|a|128", style=BTN_PRIMARY),
        make_button("🎵 MP3 320k", f"pq|{token}|a|320", style=BTN_PRIMARY),
    ])
    rows.append([make_button(SC("❌ Close"), f"x|{token}", style=BTN_DANGER)])
    caption = (
        f"📚 <b>{esc(pl['title'])}</b>\n\n"
        f"🎞 {smallcaps('Videos')}: {len(pl['entries'])}\n"
        f"⭐ {smallcaps('Default quality')}: {default_name}\n\n"
        + trim_note +
        f"👇 {smallcaps('Tap Start for default, or pick another quality')}\n"
        + SC("Videos will arrive one by one. Use <code>/cancel</code> to stop.")
    )
    await safe_edit(status, caption, InlineKeyboardMarkup(rows))


def start_playlist(client, chat_id, uid, req, mode, value):
    job = {"cancel": threading.Event(), "task": None, "phase": "queue"}
    ACTIVE[uid] = job
    job["task"] = asyncio.ensure_future(
        process_playlist_job(client, chat_id, uid, req, mode, value, job), loop=loop
    )


@app.on_callback_query(filters.regex(r"^pl\|"))
async def full_playlist_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    if not req or not req.get("playlist_url"):
        return await query.answer(SC("Link expire ho gaya — dobara bhejo."), show_alert=True)
    if req["user_id"] != query.from_user.id:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    await query.answer()
    status = await query.message.reply(SC("📚 <b>Fetching playlist...</b>"))
    await send_playlist_menu(client, status, req["playlist_url"], query.from_user.id)


@app.on_callback_query(filters.regex(r"^pq\|"))
async def playlist_quality_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    try:
        _, token, mode, value = query.data.split("|")
    except ValueError:
        return await query.answer()
    req = PENDING.get(token)
    if not req or req.get("kind") != "playlist":
        return await query.answer(SC("Link expire ho gaya — dobara bhejo."), show_alert=True)
    uid = query.from_user.id
    if req["user_id"] != uid:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    if uid in ACTIVE:
        return await query.answer(
            "Pehle wala download complete hone do, ya /cancel karo.", show_alert=True
        )
    if await over_daily_limit(client, uid):
        return await query.answer(SC("⚠️ Aaj ki free limit khatam!"), show_alert=True)
    PENDING.pop(token, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()
    start_playlist(client, query.message.chat.id, uid, req, mode, value)


@app.on_callback_query(filters.regex(r"^x\|"))
async def close_menu_cb(client, query):
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    if req and req["user_id"] != query.from_user.id:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    PENDING.pop(token, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


@app.on_callback_query(filters.regex(r"^q\|"))
async def quality_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    try:
        _, token, mode, value = query.data.split("|")
    except ValueError:
        return await query.answer()
    req = PENDING.get(token)
    if not req:
        return await query.answer(SC("Link expire ho gaya — dobara bhejo."), show_alert=True)
    uid = query.from_user.id
    if req["user_id"] != uid:
        return await query.answer(SC("Ye aapka request nahi hai."), show_alert=True)
    if uid in ACTIVE:
        return await query.answer(
            "Pehle wala download complete hone do, ya /cancel karo.", show_alert=True
        )
    if await over_daily_limit(client, uid):
        return await query.answer(SC("⚠️ Aaj ki free limit khatam!"), show_alert=True)

    PENDING.pop(token, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()

    job = {"cancel": threading.Event(), "task": None, "phase": "queue"}
    ACTIVE[uid] = job
    job["task"] = asyncio.ensure_future(
        process_job(client, query.message.chat.id, uid, req, mode, value, job), loop=loop
    )


# ---------------------------------------------------------------------
# Download + upload job
# ---------------------------------------------------------------------
class TooBig(Exception):
    pass


# ---------------------------------------------------------------------
# ⚡ File cache — a video already sent once is re-sent instantly (ported from fbot)
# ---------------------------------------------------------------------
_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})")


def yt_video_id(url: str):
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


def cache_key_for(url: str, mode: str, value) -> str | None:
    vid = yt_video_id(url)
    return f"{vid}::{mode}{value}" if vid else None


def cache_has(url: str, mode: str, value) -> bool:
    k = cache_key_for(url, mode, value)
    return bool(k and k in FILE_CACHE)


def cache_put(key: str, entry: dict):
    FILE_CACHE.pop(key, None)  # re-insert at the end = newest
    FILE_CACHE[key] = entry
    while len(FILE_CACHE) > FILE_CACHE_MAX:
        FILE_CACHE.pop(next(iter(FILE_CACHE)), None)  # oldest out
    save_data()


async def _user_names(client, uid):
    try:
        u = await client.get_users(uid)
        return " ".join(x for x in (u.first_name, u.last_name) if x), (u.username or "")
    except Exception:
        return "", ""


async def send_from_cache(client, chat_id, uid, url, mode, value, status=None, prefix="") -> bool:
    """True if the file was delivered from cache. False = not cached / stale → caller downloads normally."""
    key = cache_key_for(url, mode, value)
    ent = FILE_CACHE.get(key) if key else None
    if not ent:
        return False
    if status:
        await safe_edit(status, prefix + SC("⚡ <b>Found in cache, sending instantly...</b>"))
    u_name, u_username = await _user_names(client, uid)
    caption = build_caption(
        title=ent["title"][:120], name=ent["name"], size_bytes=ent["size"], quality_label=ent["label"],
        duration=ent.get("duration") or 0, dl_seconds=0, ul_seconds=0, user_id=uid, user_name=u_name,
        username=u_username, source_url=url, info=ent.get("meta"), cache_hit=True)
    sent = None
    if CACHE_CHANNEL_ID and ent.get("cc_msg"):
        try:  # copy from the cache channel: always carries a fresh file reference
            sent = await client.copy_message(chat_id, CACHE_CHANNEL_ID, ent["cc_msg"], caption=caption)
        except Exception as e:
            logger.warning(f"cache-channel copy failed, trying file_id: {e}")
    if sent is None and ent.get("file_id"):
        try:
            if mode == "a":
                sent = await client.send_audio(chat_id, ent["file_id"], caption=caption,
                                               title=ent["title"][:64], duration=int(ent.get("duration") or 0))
            else:
                sent = await client.send_video(chat_id, ent["file_id"], caption=caption,
                                               supports_streaming=True, duration=int(ent.get("duration") or 0))
        except Exception as e:
            logger.warning(f"cached file_id send failed: {e}")
    if sent is None:
        FILE_CACHE.pop(key, None)  # stale: drop it, caller re-downloads and re-caches
        save_data()
        return False
    schedule_delete(sent.chat.id, sent.id)
    increment_downloads(uid)
    if not get_premium_status(uid)["is_premium"]:
        bump_daily_count(uid)
    log_event(f"⚡ <b>Cache Hit</b>\n\n👤 {_utag(uid)}\n🎬 {esc(ent['title'])}\n"
              f"🎞 {ent['label']} • 📦 {human_size(ent['size'])}\n🔗 {esc(url)}")
    if BACKUP_CHANNELS or BACKUP_CHANNEL_IDS:
        asyncio.ensure_future(backup_to_linked_channels(client, sent.chat.id, sent.id), loop=loop)
    return True


async def run_one(client, chat_id, uid, url, mode, value, vertical, clients,
                  title, job, status, prefix=""):
    """Download one video/audio and upload it. Raises on failure."""
    if await send_from_cache(client, chat_id, uid, url, mode, value, status, prefix):
        return {}
    check_cooldown()  # a 429 is per server IP: don't wait in the queue just to hit it again
    label = f"{value}p" if mode == "v" else f"MP3 {value}k"
    workdir = os.path.join(DOWNLOAD_DIR, f"{uid}_{uuid.uuid4().hex[:6]}")
    os.makedirs(workdir, exist_ok=True)
    last_edit = [0.0]
    prog = {"bytes": 0, "ts": time.time(), "started": False, "finished": False, "abort": None}

    def hook(d):  # runs inside yt-dlp's worker thread
        if job["cancel"].is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled by user")
        if prog["abort"]:
            raise yt_dlp.utils.DownloadCancelled(prog["abort"])
        if d.get("status") == "finished":
            prog["finished"] = True  # merge/post-processing has no byte progress
            return
        if d.get("status") != "downloading":
            return
        prog["started"] = True
        cur = d.get("downloaded_bytes") or 0
        if cur > prog["bytes"] or prog["finished"]:
            prog["bytes"], prog["ts"], prog["finished"] = cur, time.time(), False
        now = time.time()
        if now - last_edit[0] < 4:
            return
        last_edit[0] = now
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        prog.setdefault("t0", now)
        text = progress_text(prefix, True, title, label, done, total,
                             d.get("speed") or 0, now - prog["t0"], d.get("eta") or 0)
        asyncio.run_coroutine_threadsafe(safe_edit(status, text), loop)

    up_last = [0.0]
    part_tag = [""]
    up_state = {"t0": 0.0, "first": False, "wait": None}

    async def _wait_anim():  # shown until Telegram's first upload callback arrives
        i, w0 = 0, time.time()
        while not up_state["first"]:
            await asyncio.sleep(1.5 if time.time() - w0 < 10 else 4)
            if up_state["first"]:
                break
            await safe_edit(status, prefix + SC(
                f"{'⏳⌛'[i % 2]} <b>Uploading to Telegram{'.' * (i % 4)}</b>\n\n"
                "╭━━━━❰Please Wait❱━➣\n"
                f"┣⪼ 🎬 File: <code>{esc(title[:60])}</code>\n"
                "┣⪼ ⚙️ Preparing upload stream...\n"
                f"┣⪼ ⏱ Elapsed: {hms(time.time() - w0)}\n"
                "╰━━━━━━━━━━━━━━━➣"))
            i += 1

    def _stop_wait():
        t = up_state.get("wait")
        if t and not t.done():
            t.cancel()
        up_state["wait"] = None

    async def up_progress(current, total):
        if not up_state["first"]:
            up_state["first"] = True
            up_state["t0"] = time.time()
            _stop_wait()
        now = time.time()
        if now - up_last[0] < 3 and current != total:
            return
        up_last[0] = now
        el = now - up_state["t0"]
        sp = current / el if el > 0 else 0
        eta = (total - current) / sp if sp > 0 and total else 0
        await safe_edit(status, progress_text(prefix, False, title, label, current, total, sp, el, eta,
                                              tag=part_tag[0]))

    u_name, u_username, dl_seconds = "", "", 0.0
    try:
        _u = await client.get_users(uid)
        u_name = " ".join(x for x in (_u.first_name, _u.last_name) if x)
        u_username = _u.username or ""
    except Exception:
        pass

    try:
        async with download_semaphore(priority=get_premium_status(uid)["is_premium"]):
            job["phase"] = "download"
            await safe_edit(status, prefix + SC("📥 <b>Starting download...</b>"))
            dl_fut = loop.run_in_executor(
                None, partial(download_blocking, url, mode, value, vertical, workdir, hook, clients)
            )
            dl_start = time.time()
            while True:
                done_set, _ = await asyncio.wait({dl_fut}, timeout=5)
                if done_set:
                    break
                now_t = time.time()
                if now_t - dl_start > DL_HARD_TIMEOUT:
                    prog["abort"] = "timeout"
                    raise RuntimeError(f"Download timed out after {int(now_t - dl_start) // 60} min — skipped.")
                if prog["started"] and not prog["finished"] and now_t - prog["ts"] > DL_STALL_TIMEOUT:
                    prog["abort"] = "stalled"
                    raise RuntimeError(f"Download stalled ({DL_STALL_TIMEOUT}s no data) — skipped.")
            path, thumb, info = dl_fut.result()
            dl_seconds = time.time() - dl_start

        size = os.path.getsize(path)
        parts = [path]
        if size > MAX_FILE_SIZE:
            await safe_edit(
                status,
                prefix + SC(f"✂️ <b>File is {human_size(size)} — splitting into parts...</b>\n\n")
                + SC("Telegram 2 GB se badi file nahi leta, isliye parts mein bhej raha hoon."),
            )
            parts = await loop.run_in_executor(
                None, partial(split_file, path, workdir, info.get("id") or "video", SPLIT_PART_TARGET)
            )
            if any(os.path.getsize(p) > MAX_FILE_SIZE for p in parts):
                raise TooBig(
                    f"File {human_size(size)} ko split nahi kar paya — kam quality select karke dobara try karo."
                )

        job["phase"] = "upload"
        n = len(parts)
        for idx, part in enumerate(parts, 1):
            if job["cancel"].is_set():
                raise yt_dlp.utils.DownloadCancelled("cancelled by user")
            psize = os.path.getsize(part)
            if n > 1:
                pdur, pw, ph = await loop.run_in_executor(None, probe_media, part)
                part_tag[0] = f" Part {idx}/{n}"
            else:
                pdur, pw, ph = info.get("duration") or 0, info.get("width") or 0, info.get("height") or 0
            _ext = os.path.splitext(part)[1] or (".mp3" if mode == "a" else ".mp4")
            fname = re.sub(r'[\\/:*?"<>|]', "", title).strip()[:60] + (f" (part {idx})" if n > 1 else "") + _ext
            qlabel = label + (f" • Part {idx}/{n}" if n > 1 else "")
            cap_args = dict(title=title[:120], name=fname, size_bytes=psize, quality_label=qlabel,
                            duration=pdur, dl_seconds=dl_seconds, user_id=uid, user_name=u_name,
                            username=u_username, source_url=url, info=info)
            caption = build_caption(ul_seconds=0, **cap_args)
            up_last[0] = 0.0
            up_state["first"], up_state["t0"] = False, 0.0
            _stop_wait()
            up_state["wait"] = asyncio.ensure_future(_wait_anim(), loop=loop)
            ul_start = time.time()
            if mode == "a":
                send_coro = client.send_audio(
                    chat_id, part, caption=caption,
                    title=(title[:55] + (f" ({idx}/{n})" if n > 1 else ""))[:64],
                    performer=(info.get("uploader") or "")[:64],
                    duration=int(pdur or 0), thumb=thumb, progress=up_progress,
                )
            else:
                send_coro = client.send_video(
                    chat_id, part, caption=caption,
                    duration=int(pdur or 0), width=pw or 0, height=ph or 0,
                    thumb=thumb, supports_streaming=True, progress=up_progress,
                )
            try:
                sent_msg = await asyncio.wait_for(send_coro, timeout=UPLOAD_TIMEOUT)
            except asyncio.TimeoutError:
                raise RuntimeError(f"Upload stalled for over {UPLOAD_TIMEOUT // 60} min — skipped.")
            finally:
                _stop_wait()
            try:  # now that the real upload time is known, fill it into the caption
                await sent_msg.edit_caption(build_caption(ul_seconds=time.time() - ul_start, **cap_args))
            except Exception as ce:
                logger.debug(f"caption edit failed: {ce}")
            if n == 1:  # a split upload has no single file_id, so only whole files are cached
                media = sent_msg.audio if mode == "a" else sent_msg.video
                ckey = cache_key_for(url, mode, value)
                if media and ckey:
                    cc_msg = None
                    if CACHE_CHANNEL_ID:
                        try:
                            cc_msg = (await client.copy_message(CACHE_CHANNEL_ID, sent_msg.chat.id, sent_msg.id)).id
                        except Exception as ce:
                            logger.warning(f"cache-channel copy failed (bot admin there?): {ce}")
                    cache_put(ckey, {
                        "file_id": media.file_id, "cc_msg": cc_msg, "title": title, "name": fname,
                        "size": psize, "label": label, "duration": pdur,
                        "meta": {k: info.get(k) for k in ("uploader", "uploader_url", "channel", "channel_url",
                                                          "view_count", "like_count", "comment_count",
                                                          "upload_date", "categories")},
                        "ts": time.time(),
                    })
            schedule_delete(sent_msg.chat.id, sent_msg.id)
            if BACKUP_CHANNELS or BACKUP_CHANNEL_IDS:  # copy in background, never delays the user
                asyncio.ensure_future(backup_to_linked_channels(client, sent_msg.chat.id, sent_msg.id), loop=loop)
            try:
                os.remove(part)  # free disk as we go
            except OSError:
                pass
        increment_downloads(uid)
        if not get_premium_status(uid)["is_premium"]:
            bump_daily_count(uid)
        log_event(
            f"📥 <b>Download Done</b>\n\n👤 {_utag(uid)}\n🎬 {esc(title)}\n"
            f"🎞 {label} • 📦 {human_size(size)}\n🔗 {esc(url)}"
        )
        return info
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def process_job(client: Client, chat_id: int, uid: int, req: dict, mode: str, value: str, job: dict):
    status = await client.send_message(chat_id, SC("⏳ <b>Queued... starting soon</b>\n\nUse <code>/cancel</code> to stop."))
    try:
        await run_one(client, chat_id, uid, req["url"], mode, value, req["vertical"],
                      req.get("clients"), req["title"], job, status)
        try:
            await status.delete()
        except Exception:
            pass
    except (asyncio.CancelledError, yt_dlp.utils.DownloadCancelled):
        await safe_edit(status, SC("🛑 <b>Download cancelled.</b>"))
    except TooBig as e:
        await safe_edit(status, SC("❌ <b>File too big</b>\n\n") + esc(str(e)))
        log_event(f"❌ <b>File Too Big</b>\n\n👤 {_utag(uid)}\n🔗 {esc(req['url'])}\n{esc(str(e)[:300])}")
    except YouTubeCooldown as e:
        await safe_edit(status, SC("🛑 <b>YouTube cooldown</b>\n\n") + esc(str(e)))
    except Exception as e:
        if job["cancel"].is_set() or "cancelled by user" in str(e):
            await safe_edit(status, SC("🛑 <b>Download cancelled.</b>"))
        else:
            logger.exception("job failed")
            log_event(f"❌ <b>Download Failed</b>\n\n👤 {_utag(uid)}\n🔗 {esc(req['url'])}\n<code>{esc(str(e)[:400])}</code>")
            await safe_edit(
                status,
                SC("❌ <b>Download failed</b>\n\n") + esc(friendly_error(str(e)))
                + SC("\n\nDobara link bhejke try karo."),
            )
    finally:
        ACTIVE.pop(uid, None)


async def process_playlist_job(client: Client, chat_id: int, uid: int, req: dict,
                               mode: str, value: str, job: dict):
    entries, total = req["entries"], len(req["entries"])
    sent, failed = 0, []
    cancelled = False
    limit_hit = False
    cooldown_hit = False
    prefetch: dict = {}  # url -> future of fetch_info for the upcoming video
    status = await client.send_message(
        chat_id, SC(f"📚 <b>Playlist started — {total} videos</b>\n\nUse <code>/cancel</code> to stop.")
    )
    try:
        for i, e in enumerate(entries, 1):
            if job["cancel"].is_set():
                cancelled = True
                break
            if (DAILY_FREE_LIMIT and not get_premium_status(uid)["is_premium"]
                    and get_daily_count(uid) >= DAILY_FREE_LIMIT):
                limit_hit = True
                break
            job["phase"] = "queue"  # /cancel during this phase cancels the task
            prefix = SC(f"📚 Playlist {i}/{total}\n\n")
            try:
                if await send_from_cache(client, chat_id, uid, e["url"], mode, value, status, prefix):
                    sent += 1  # cached: no fetch, no download, no upload wait
                    await asyncio.sleep(0.3)
                    continue
                fut = prefetch.pop(e["url"], None)
                if fut is None:
                    await safe_edit(status, prefix + SC("🔍 <b>Fetching video info...</b>"))
                    fut = loop.run_in_executor(None, fetch_info, e["url"])
                info, clients = await fut
                if i < total:  # start the NEXT video's fetch now; it overlaps this download+upload
                    nxt = entries[i]["url"]
                    if nxt not in prefetch and not cache_has(nxt, mode, value):
                        pf = loop.run_in_executor(None, fetch_info, nxt)
                        pf.add_done_callback(lambda f: f.exception())  # never "exception was never retrieved"
                        prefetch[nxt] = pf
                if info.get("is_live"):
                    raise ValueError("Live stream — skipped")
                await run_one(client, chat_id, uid, e["url"], mode, value, is_vertical(info),
                              clients, info.get("title") or e["title"], job, status, prefix)
                sent += 1
            except yt_dlp.utils.DownloadCancelled:
                cancelled = True
                break
            except TooBig as ex:
                failed.append((e["title"], "file too big"))
            except YouTubeCooldown:
                cooldown_hit = True
                break
            except Exception as ex:
                if job["cancel"].is_set() or "cancelled by user" in str(ex):
                    cancelled = True
                    break
                logger.warning(f"playlist item {e['id']} failed: {ex}")
                failed.append((e["title"], friendly_error(str(ex))[:80]))
                if yt_auth.cooldown_remaining() > 0:  # this item just hit a 429 -> stop, don't hammer
                    cooldown_hit = True
                    break
            await asyncio.sleep(1)  # be gentle with Telegram flood limits
    except asyncio.CancelledError:
        cancelled = True
    finally:
        ACTIVE.pop(uid, None)

    head = ("🛑 <b>Playlist cancelled.</b>" if cancelled else
            "⚠️ <b>Daily free limit reached — playlist roki gayi.</b>" if limit_hit else
            f"🛑 <b>YouTube 429 — playlist roki gayi.</b> {yt_auth.human_time_short(yt_auth.cooldown_remaining())} "
            "baad dobara try karo." if cooldown_hit else
            "✅ <b>Playlist done!</b>")
    text = SC(f"{head}\n\n📤 Sent: {sent}/{total}\n❌ Failed: {len(failed)}")
    if failed:
        lines = "\n".join(f"• {esc(t[:45])} — {esc(r)}" for t, r in failed[:15])
        more = f"\n…+{len(failed) - 15} more" if len(failed) > 15 else ""
        text += "\n\n" + lines + more
    try:
        await status.delete()
    except Exception:
        pass
    await client.send_message(chat_id, text)
    if limit_hit:
        await send_referral_prompt(client, chat_id)


# ---------------------------------------------------------------------
# 💎 Premium plans, daily limit & referrals  (ported from fbot)
# There is no payment gateway: a plan tap shows how to pay, "I've Paid" pings the
# admin(s), and the admin activates it with /addpremium <user_id> <days|lifetime>.
# ---------------------------------------------------------------------
PLAN_DAYS = dict(PLANS)


def _plan_label(days) -> str:
    return "Lifetime Access ♾️" if days is None else f"{days} Days"


def plan_summary(user_id: int) -> str:
    st = get_premium_status(user_id)
    if st["lifetime"]:
        return "Premium (Lifetime ♾️)"
    if st["is_premium"]:
        left = (st["expires_at"] - _now_utc()).days + 1
        return f"Premium ({left} day{'s' if left != 1 else ''} left)"
    if DAILY_FREE_LIMIT:
        return f"Free ({get_daily_count(user_id)}/{DAILY_FREE_LIMIT} today)"
    return "Free"


async def over_daily_limit(client: Client, user_id: int) -> bool:
    """True (and the referral/upgrade prompt is sent) if a free user used up today's quota."""
    if not DAILY_FREE_LIMIT or get_premium_status(user_id)["is_premium"]:
        return False
    if get_daily_count(user_id) < DAILY_FREE_LIMIT:
        return False
    await send_referral_prompt(client, user_id)
    return True


async def send_photo_or_text(client: Client, chat_id: int, src: str, text: str, markup=None):
    """Photo (URL or Telegram post link) with `text` as caption; plain message if no photo/it fails."""
    src = (src or "").strip()
    if src:
        try:
            tg = _TG_POST_RE.match(src)
            if tg:
                private_id, chan_id, username, msg_id = tg.groups()
                chat = int(f"-100{chan_id}") if private_id else username
                return await client.copy_message(chat_id, chat, int(msg_id), caption=text, reply_markup=markup)
            return await client.send_photo(chat_id, src, caption=text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"photo send failed ({src[:40]}): {e}")
    return await client.send_message(chat_id, text, reply_markup=markup,
                                     link_preview_options=LinkPreviewOptions(is_disabled=True))


# ---- plans ----
def plans_text() -> str:
    lines = "\n".join(f"• ₹{price} → {_plan_label(days)}" for price, days in PLANS) or "• Admin se contact karo"
    free = ""
    if DAILY_FREE_LIMIT or PLAYLIST_FREE_MAX:
        free = (f"🆓 Free: {DAILY_FREE_LIMIT or '∞'} downloads/day"
                f", playlist mein pehle {PLAYLIST_FREE_MAX or '∞'} videos\n\n")
    pay = (
        f"🔒 <b>Secure Payment:</b>\n⚡️ UPI ID: <code>{esc(UPI_ID)}</code>\n"
        "💡 Payment ke baad screenshot admin ko bhejo — instant activation.\n\n"
        if UPI_ID else "💡 Payment ke liye admin se contact karo.\n\n"
    )
    return (
        "💎 <b>Premium Membership Plans</b>\n"
        "✨ Unlimited access & priority!\n\n"
        "✅ Unlimited daily downloads\n"
        "✅ Poori playlist (no cap)\n"
        "✅ 🎯 Priority queue — tumhara kaam pehle\n\n"
        + free + lines + "\n\n" + pay + "👇 Plan pe tap karo — shuru ho jao!"
    )


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [[make_button(SC(f"💎 ₹{price} - {_plan_label(days)}"), f"plan_{price}", style=BTN_PRIMARY)]
            for price, days in PLANS]
    rows.append([make_button(SC("📸 Send Payment Proof"), url=POWERED_BY_URL, style=BTN_PRIMARY)])
    rows.append([make_button(SC("⬅️ Back"), "plans_back", style=BTN_DANGER)])
    return InlineKeyboardMarkup(rows)


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("💎 View Plans"), "show_plans", style=BTN_PRIMARY)],
        [make_button(SC("📞 Contact Admin"), url=POWERED_BY_URL, style=BTN_PRIMARY)],
    ])


async def send_plans(client: Client, chat_id: int):
    await send_photo_or_text(client, chat_id, PLANS_PHOTO_URL, SC(plans_text()), plans_keyboard())


@app.on_message((filters.command(["plans", "premium"]) | menu_text_filter(BTN_PLANS)) & filters.private)
async def plans_cmd(client: Client, m: Message):
    await send_plans(client, m.chat.id)


@app.on_message(filters.command("myplan") & filters.private)
async def myplan_cmd(client: Client, m: Message):
    uid = m.from_user.id
    register_user(uid, m.from_user.username, full_name(m.from_user))
    st = get_premium_status(uid)
    text = (
        "<b>📊 Your Status</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        f"Plan: <code>{plan_summary(uid)}</code>\n"
        f"Total Downloads: <code>{DB['users'].get(str(uid), {}).get('total_downloads', 0)}</code>\n"
    )
    if not st["is_premium"]:
        if DAILY_FREE_LIMIT:
            used = get_daily_count(uid)
            text += f"Today's downloads: {used}/{DAILY_FREE_LIMIT} ({max(0, DAILY_FREE_LIMIT - used)} left)\n"
        text += "\n💎 Premium lo — unlimited downloads ka maza lo!"
    await m.reply(SC(text), reply_markup=status_keyboard())


@app.on_callback_query(filters.regex(r"^show_plans$"))
async def show_plans_cb(client: Client, query):
    await query.answer()
    await send_plans(client, query.message.chat.id)


@app.on_callback_query(filters.regex(r"^plans_back$"))
async def plans_back_cb(client: Client, query):
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


def payment_keyboard(price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("✅ I've Paid"), f"paid_{price}", style=BTN_PRIMARY)],
        [make_button(SC("📸 Send Payment Proof"), url=POWERED_BY_URL, style=BTN_PRIMARY)],
        [make_button(SC("⬅️ Back"), "plans_back", style=BTN_DANGER)],
    ])


@app.on_callback_query(filters.regex(r"^plan_\d+$"))
async def plan_selected_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    if price not in PLAN_DAYS:
        return await query.answer(SC("Ye plan ab available nahi hai."), show_alert=True)
    how = (f"📱 Kisi bhi UPI app se pay karo (PhonePe / GPay / Paytm):\n<code>{esc(UPI_ID)}</code>\n\n"
           if UPI_ID else "📱 Payment details ke liye admin se contact karo.\n\n")
    qr = "🔗 §QR§\n\n" if QR_CODE_URL else ""
    text = (
        f"💳 <b>{_plan_label(PLAN_DAYS[price])}</b> ke liye Payment\n\n"
        f"Amount: ₹{price}\n\n" + how + qr +
        "Payment ke baad 'I've Paid' dabao aur screenshot admin ko bhejo."
    )
    text = SC(text)
    if QR_CODE_URL:  # inserted after SC() so "Scan to Pay" keeps normal letters
        text = text.replace("§QR§", f'{smallcaps("QR Code")}: <a href="{esc(QR_CODE_URL)}">Scan to Pay</a>')
    await query.answer()
    await send_photo_or_text(client, query.message.chat.id, PLANS_PHOTO_URL, text, payment_keyboard(price))


@app.on_callback_query(filters.regex(r"^paid_\d+$"))
async def paid_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    if price not in PLAN_DAYS:
        return await query.answer(SC("Ye plan ab available nahi hai."), show_alert=True)
    days = PLAN_DAYS[price]
    user = query.from_user
    name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    mention = f'<a href="tg://user?id={user.id}"><code>{esc(name)}</code></a>'
    log_event(f"🔔 <b>Payment Claim</b>\n\n👤 {_utag(user.id)}\nPlan: ₹{price} - {_plan_label(days)}")
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(
                admin_id,
                SC("🔔 <b>Payment Claim</b>\n\n"
                   f"User: {mention} (<code>{user.id}</code>)\n"
                   f"Plan: ₹{price} - {_plan_label(days)}\n\n"
                   f"Screenshot verify karke chalao:\n<code>/addpremium {user.id} {days or 'lifetime'}</code>"),
            )
        except Exception as e:
            logger.warning(f"failed to notify admin {admin_id}: {e}")
    await query.answer(SC("✅ Admin ko notify kar diya!"), show_alert=True)
    await query.message.reply(
        SC("✅ Aapka payment claim admin ko bhej diya gaya hai.\n"
           f'Jaldi verification ke liye screenshot bhi bhej do: <a href="{POWERED_BY_URL}">{esc(POWERED_BY)}</a>'),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


# ---- referrals ----
async def get_bot_username(client: Client) -> str:
    cached = getattr(client, "_cached_username", None)
    if cached:
        return cached
    me = await client.get_me()
    client._cached_username = me.username or ""
    return client._cached_username


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def referral_text(count: int) -> str:
    total = 0
    reward_lines = []
    for threshold, days in REFERRAL_REWARDS:
        total += days
        reward_lines.append(f"🎯 {threshold} referrals → 💎 {total} day{'s' if total != 1 else ''} premium")
    return (
        "🎁 <b>Refer & Earn Premium</b>\n\n"
        "⚡ Tumhari daily download limit khatam ho gayi hai ya premium expire ho gaya.\n\n"
        "💎 Premium free mein chahiye?\n"
        "👥 Apne unique referral link se friends ko invite karo.\n\n"
        "🏆 <b>Rewards</b>\n\n" + "\n".join(reward_lines) + "\n\n"
        f"📊 Your referrals: {count}/{REFERRAL_GOAL}\n\n"
        "🔗 Link share karo & premium kamao! Ya seedha /plans se khareed lo."
    )


def referral_keyboard(bot_username: str, user_id: int, count: int) -> InlineKeyboardMarkup:
    link = referral_link(bot_username, user_id)
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('🎬 Download YouTube videos & songs on Telegram — try this bot!')}"
    return InlineKeyboardMarkup([
        [make_button(SC("🔗 Get Referral Link"), "ref_getlink", style=BTN_PRIMARY)],
        [make_button(SC("📤 Share Referral Link"), url=share, style=BTN_PRIMARY)],
        [make_button(SC(f"👥 Referrals: {count}"), "ref_count", style=BTN_PRIMARY)],
        [make_button(SC("💎 Premium Rewards"), "ref_rewards", style=BTN_PRIMARY),
         make_button(SC("💎 Plans"), "show_plans", style=BTN_PRIMARY)],
        [make_button(SC("📞 Contact Admin"), url=POWERED_BY_URL, style=BTN_PRIMARY)],
    ])


async def send_referral_prompt(client: Client, chat_id: int):
    username = await get_bot_username(client)
    count = get_referral_count(chat_id)
    await send_photo_or_text(client, chat_id, REFERRAL_PHOTO_URL, SC(referral_text(count)),
                             referral_keyboard(username, chat_id, count))


@app.on_message(filters.command("referral") & filters.private)
async def referral_cmd(client: Client, m: Message):
    register_user(m.from_user.id, m.from_user.username, full_name(m.from_user))
    await send_referral_prompt(client, m.from_user.id)


@app.on_callback_query(filters.regex(r"^ref_getlink$"))
async def ref_getlink_cb(client: Client, query):
    link = referral_link(await get_bot_username(client), query.from_user.id)
    await query.answer()
    await client.send_message(
        query.from_user.id,
        SC(f"🔗 <b>Your referral link:</b>\n\n<code>{link}</code>\n\n"
           "Friends ke saath share karo — jab wo bot start karenge, tumhe credit milega."),
    )


@app.on_callback_query(filters.regex(r"^ref_count$"))
async def ref_count_cb(client: Client, query):
    await query.answer(f"👥 Your referrals: {get_referral_count(query.from_user.id)}/{REFERRAL_GOAL}", show_alert=True)


@app.on_callback_query(filters.regex(r"^ref_rewards$"))
async def ref_rewards_cb(client: Client, query):
    total, lines = 0, []
    for threshold, days in REFERRAL_REWARDS:
        total += days
        lines.append(f"🎯 {threshold} referrals → {total} day{'s' if total != 1 else ''} premium")
    await query.answer("\n".join(lines), show_alert=True)


async def process_referral(client: Client, m: Message):
    """Called for brand-new users only: /start ref_<id> credits the referrer."""
    if len(m.command) < 2 or not m.command[1].startswith("ref_"):
        return
    try:
        referrer_id = int(m.command[1][4:])
    except ValueError:
        return
    if not set_referrer(m.from_user.id, referrer_id):
        return
    count = increment_referral_count(referrer_id)
    try:
        await client.send_message(
            referrer_id, SC(f"🎁 Someone joined using your referral link!\n👥 Total referrals: {count}/{REFERRAL_GOAL}"))
    except Exception:
        pass
    claimed = get_referral_rewards_claimed(referrer_id)
    for threshold, days in REFERRAL_REWARDS:
        if count >= threshold and threshold not in claimed:
            _extend_premium(referrer_id, days)
            mark_referral_reward_claimed(referrer_id, threshold)
            try:
                await client.send_message(
                    referrer_id,
                    SC(f"🎉 <b>Referral reward!</b>\n\n👥 {threshold} referrals — 💎 {days} day(s) premium added!"))
            except Exception:
                pass


# ---- admin-only premium management ----
@app.on_message(filters.command("addpremium") & filters.private)
async def addpremium_cmd(client: Client, m: Message):
    if not is_admin(m.from_user.id):
        return
    usage = SC("⚠️ <b>Usage:</b> <code>/addpremium &lt;user_id&gt; &lt;days|lifetime&gt;</code>")
    if len(m.command) < 3:
        return await m.reply(usage)
    try:
        target = int(m.command[1])
    except ValueError:
        return await m.reply(usage)
    arg = m.command[2].lower()
    if arg in ("lifetime", "life", "forever"):
        days, note = None, "Lifetime ♾️"
    else:
        try:
            days = int(arg)
        except ValueError:
            return await m.reply(usage)
        if not 1 <= days <= 3650:
            return await m.reply(SC("⚠️ Days 1 se 3650 ke beech hone chahiye."))
        note = f"{days} day{'s' if days != 1 else ''}"
    set_premium(target, days)
    await m.reply(SC(f"✅ Premium granted to <code>{target}</code> — {note}.\nPlan: {plan_summary(target)}"))
    try:
        await client.send_message(target, SC(f"🎉 You've been given Premium ({note}) by the admin!"))
    except Exception as e:
        logger.warning(f"couldn't notify {target} about premium grant: {e}")


@app.on_message(filters.command("removepremium") & filters.private)
async def removepremium_cmd(client: Client, m: Message):
    if not is_admin(m.from_user.id):
        return
    usage = SC("⚠️ <b>Usage:</b> <code>/removepremium &lt;user_id&gt;</code>")
    if len(m.command) < 2:
        return await m.reply(usage)
    try:
        target = int(m.command[1])
    except ValueError:
        return await m.reply(usage)
    remove_premium(target)
    await m.reply(SC(f"✅ Premium removed for <code>{target}</code>."))


@app.on_message(filters.command("potstatus") & filters.private)
async def potstatus_cmd(client: Client, m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.reply(f"🔑 <b>PO-token provider</b>\n\n<code>{esc(pot_provider.get_status())}</code>\n\n"
                  f"Ready: {'✅' if pot_provider.is_ready() else '❌'}")


# ---------------------------------------------------------------------
# 🚫 Ban / Unban / 📣 Broadcast  (admin only, ported from fbot)
# ---------------------------------------------------------------------
def _is_banned_user(_, __, m) -> bool:
    u = getattr(m, "from_user", None)
    return bool(u) and u.id in BANNED and not is_admin(u.id)


banned_filter = filters.create(_is_banned_user)
_BAN_NOTICE_TS: dict[int, float] = {}


@app.on_message(filters.private & banned_filter, group=-1)
async def banned_message_block(client: Client, m: Message):
    """Runs before every other handler: a banned user gets one notice a minute, nothing else."""
    uid = m.from_user.id
    if time.time() - _BAN_NOTICE_TS.get(uid, 0) > 60:
        _BAN_NOTICE_TS[uid] = time.time()
        try:
            await m.reply(SC("🚫 Aapko is bot se ban kar diya gaya hai."))
        except Exception:
            pass
    raise StopPropagation


@app.on_callback_query(banned_filter, group=-1)
async def banned_callback_block(client: Client, query):
    try:
        await query.answer(SC("🚫 Aapko is bot se ban kar diya gaya hai."), show_alert=True)
    except Exception:
        pass
    raise StopPropagation


def _target_id(m: Message):
    """User id from `/cmd <id>` or from replying to a forwarded/user message."""
    if len(m.command) >= 2:
        try:
            return int(m.command[1])
        except ValueError:
            return None
    r = m.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        return r.from_user.id
    return None


@app.on_message(filters.command("ban") & filters.private & admin_only_early)
async def ban_cmd(client: Client, m: Message):
    tid = _target_id(m)
    if tid is None:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/ban &lt;user_id&gt;</code>"))
    if is_admin(tid):
        return await m.reply(SC("⚠️ Admin ko ban nahi kar sakte."))
    if tid in BANNED:
        return await m.reply(SC(f"⚠️ <code>{tid}</code> pehle se banned hai."))
    BANNED.append(tid)
    save_data()
    await m.reply(SC(f"🚫 <code>{tid}</code> ban ho gaya."))
    try:
        await client.send_message(tid, SC("🚫 Aapko is bot se ban kar diya gaya hai."))
    except Exception as e:
        logger.info(f"ban notice to {tid} failed: {e}")


@app.on_message(filters.command("unban") & filters.private & admin_only_early)
async def unban_cmd(client: Client, m: Message):
    tid = _target_id(m)
    if tid is None:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/unban &lt;user_id&gt;</code>"))
    if tid not in BANNED:
        return await m.reply(SC(f"⚠️ <code>{tid}</code> banned nahi hai."))
    BANNED.remove(tid)
    _BAN_NOTICE_TS.pop(tid, None)
    save_data()
    await m.reply(SC(f"✅ <code>{tid}</code> unban ho gaya."))
    try:
        await client.send_message(tid, SC("✅ Aapko unban kar diya gaya hai — ab bot use kar sakte ho."))
    except Exception as e:
        logger.info(f"unban notice to {tid} failed: {e}")


@app.on_message(filters.command("banned") & filters.private & admin_only_early)
async def banned_list_cmd(client: Client, m: Message):
    if not BANNED:
        return await m.reply(SC("✅ Koi banned user nahi hai."))
    lines = "\n".join(f"• <code>{i}</code>" for i in BANNED[:100])
    more = f"\n…+{len(BANNED) - 100} more" if len(BANNED) > 100 else ""
    await m.reply(SC(f"🚫 <b>Banned users ({len(BANNED)})</b>\n\n") + lines + more)


_BROADCAST_RUNNING = [False]


async def _broadcast_one(client: Client, cid: int, text, reply, from_chat_id: int) -> str:
    try:
        if text is not None:
            await client.send_message(cid, text)
        else:
            await client.copy_message(chat_id=cid, from_chat_id=from_chat_id, message_id=reply.id)
        return "ok"
    except FloodWait as fw:
        await asyncio.sleep(fw.value + 1)
        return await _broadcast_one(client, cid, text, reply, from_chat_id)
    except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
        return "blocked"
    except Exception as e:
        logger.info(f"broadcast to {cid} failed: {e}")
        return "failed"


async def _run_broadcast(client: Client, m: Message, text, reply):
    ids = [int(x) for x in DB["users"] if int(x) not in BANNED]
    total, res = len(ids), {"ok": 0, "blocked": 0, "failed": 0}
    status = await m.reply(SC(f"📣 <b>Broadcasting...</b>\n\n👥 Total: {total}"))
    try:
        for i, cid in enumerate(ids, 1):
            res[await _broadcast_one(client, cid, text, reply, m.chat.id)] += 1
            if i % 20 == 0 or i == total:
                await safe_edit(status, SC(
                    f"📣 <b>Broadcasting...</b>\n\n💫 Done: {i}/{total}\n✅ Success: {res['ok']}\n"
                    f"🚫 Blocked/deleted: {res['blocked']}\n❌ Failed: {res['failed']}"))
            await asyncio.sleep(0.05)  # stay under flood limits
    finally:
        _BROADCAST_RUNNING[0] = False
    await safe_edit(status, SC(
        f"📣 <b>Broadcast done.</b>\n\n👥 Total: {total}\n✅ Success: {res['ok']}\n"
        f"🚫 Blocked/deleted: {res['blocked']}\n❌ Failed: {res['failed']}"))


@app.on_message(filters.command("broadcast") & filters.private & admin_only_early)
async def broadcast_cmd(client: Client, m: Message):
    reply = m.reply_to_message
    text = None
    if len(m.command) >= 2:
        text = m.text.html.split(None, 1)[1] if m.text else None  # keeps bold/links the admin typed
    elif not reply:
        return await m.reply(SC(
            "⚠️ <b>Usage:</b> <code>/broadcast &lt;message&gt;</code>\n"
            "Ya kisi bhi message (photo/video/file) par reply karke sirf <code>/broadcast</code> likho."))
    if _BROADCAST_RUNNING[0]:
        return await m.reply(SC("⚠️ Ek broadcast pehle se chal raha hai — khatam hone do."))
    _BROADCAST_RUNNING[0] = True
    asyncio.ensure_future(_run_broadcast(client, m, text, reply), loop=loop)


@app.on_message(filters.command("clearcache") & filters.private & admin_only_early)
async def clearcache_cmd(client: Client, m: Message):
    n = len(FILE_CACHE)
    if len(m.command) >= 2:  # /clearcache <video id or link> -> just that video
        vid = yt_video_id(m.command[1]) or m.command[1]
        keys = [k for k in FILE_CACHE if k.startswith(f"{vid}::")]
        for k in keys:
            FILE_CACHE.pop(k, None)
        save_data()
        return await m.reply(SC(f"🗑️ {len(keys)} cached file(s) hata di (<code>{esc(vid)}</code>)."))
    FILE_CACHE.clear()
    save_data()
    await m.reply(SC(f"🗑️ Poora cache saaf — {n} files."))


@app.on_message(filters.command("cachestats") & filters.private & admin_only_early)
async def cachestats_cmd(client: Client, m: Message):
    total = sum(e.get("size", 0) for e in FILE_CACHE.values())
    await m.reply(SC(
        "⚡ <b>File Cache</b>\n\n"
        f"📦 Files: {len(FILE_CACHE)} / {FILE_CACHE_MAX}\n"
        f"💾 Total size (Telegram par): {human_size(total)}\n"
        f"📡 Cache channel: {('<code>' + str(CACHE_CHANNEL_ID) + '</code>') if CACHE_CHANNEL_ID else 'not set (file_id use hota hai)'}\n\n"
        "• <code>/clearcache</code> — sab saaf\n• <code>/clearcache &lt;link/id&gt;</code> — ek video"))


# ---------------------------------------------------------------------
# 📡 Backup channels — every delivered file is also copied there (ported from fbot)
# ---------------------------------------------------------------------
async def backup_to_linked_channels(client: Client, chat_id: int, message_id: int):
    """Best-effort copy of a delivered file into every linked backup channel (env ones +
    /set_channel_id ones). Failures are logged and ignored — never breaks the user's download."""
    for channel_id in set(BACKUP_CHANNEL_IDS) | set(BACKUP_CHANNELS):
        try:
            await client.copy_message(chat_id=channel_id, from_chat_id=chat_id, message_id=message_id)
        except FloodWait as fw:
            await asyncio.sleep(min(fw.value, 30))
        except Exception as e:
            logger.warning(f"Backup to {channel_id} failed: {e}")


@app.on_message(filters.command("set_channel_id") & filters.private & admin_only_early)
async def set_channel_id_cmd(client: Client, m: Message):
    if len(m.command) < 2:
        return await m.reply(SC(
            "⚠️ <b>Usage:</b> <code>/set_channel_id -100xxxxxxxxxx</code>\n"
            "<b>Example:</b> <code>/set_channel_id -1001234567890</code>\n\n"
            "ℹ️ ID <code>-100</code> se start honi chahiye, aur bot us channel/group mein admin hona chahiye.\n\n"
            "Ek se zyada channel link kar sakte ho — command dobara alag ID ke saath chalao. "
            "/channel_id se list dekho, /del_channel_id &lt;id&gt; se hatao (bina id ke sab hat jaayenge)."
        ))
    raw = m.command[1]
    if not raw.startswith("-100") or not raw.lstrip("-").isdigit():
        return await m.reply(SC("⚠️ ID <code>-100</code> se start honi chahiye, e.g. <code>-1001234567890</code>."))
    channel_id = int(raw)
    try:
        chat = await client.get_chat(channel_id)
        member = await client.get_chat_member(channel_id, "me")
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return await m.reply(SC("⚠️ Bot us chat mein hai par admin nahi hai. Pehle admin banao."))
    except Exception as e:
        return await m.reply(SC("⚠️ Chat verify nahi hua — pehle bot ko wahan add karo.\n")
                             + f"<code>{esc(str(e)[:300])}</code>")
    is_new = channel_id not in BACKUP_CHANNELS
    if is_new:
        BACKUP_CHANNELS.append(channel_id)
        save_data()
    title = getattr(chat, "title", None) or str(channel_id)
    await m.reply(SC(f"✅ {'Linked' if is_new else 'Already linked'}: <b>{esc(title)}</b> (<code>{channel_id}</code>).\n\n"
                     "Ab har download ki copy is channel mein bhi jaayegi."))


@app.on_message(filters.command("channel_id") & filters.private & admin_only_early)
async def channel_id_cmd(client: Client, m: Message):
    static_ids = [c for c in BACKUP_CHANNEL_IDS if c not in BACKUP_CHANNELS]
    if not BACKUP_CHANNELS and not static_ids:
        return await m.reply(SC("❌ Abhi koi backup channel linked nahi hai.\n\n"
                                "Link karne ke liye: <code>/set_channel_id -100xxxxxxxxxx</code>"))
    lines = []
    for cid in BACKUP_CHANNELS:
        try:
            title = getattr(await client.get_chat(cid), "title", None) or str(cid)
        except Exception:
            title = "(unreachable)"
        lines.append(f"• <b>{esc(title)}</b> — <code>{cid}</code>")
    for cid in static_ids:
        lines.append(f"• <code>{cid}</code> — env se (yahan se hata nahi sakte)")
    await m.reply(SC("🔗 <b>Linked Backup Channels</b>\n\n") + "\n".join(lines))


@app.on_message(filters.command("del_channel_id") & filters.private & admin_only_early)
async def del_channel_id_cmd(client: Client, m: Message):
    if len(m.command) < 2:
        n = len(BACKUP_CHANNELS)
        BACKUP_CHANNELS.clear()
        save_data()
        return await m.reply(SC(f"🗑️ Saare linked channels hata diye ({n})."))
    try:
        cid = int(m.command[1])
    except ValueError:
        return await m.reply(SC("⚠️ Channel ID number honi chahiye, e.g. <code>-1001234567890</code>."))
    if cid in BACKUP_CHANNELS:
        BACKUP_CHANNELS.remove(cid)
        save_data()
        await m.reply(SC(f"🗑️ Unlinked <code>{cid}</code>."))
    else:
        await m.reply(SC(f"⚠️ <code>{cid}</code> linked nahi tha (ya env se set hai)."))


# ---------------------------------------------------------------------
# 🍪 YouTube auth — cookies upload, live check, 429 cooldown (ported from YTtoTg)
# ---------------------------------------------------------------------
COOKIE_WINDOW_SECONDS = 15 * 60
COOKIE_WINDOW: dict[int, float] = {}  # chat_id -> time the upload window closes
_cookie_lock = asyncio.Lock()


def _admin_only(_, __, m: Message) -> bool:
    return bool(m.from_user) and is_admin(m.from_user.id)


admin_only = filters.create(_admin_only)


def auth_panel_text() -> str:
    left = yt_auth.cooldown_remaining()
    cd = (f"\n🛑 <b>YouTube cooldown:</b> {yt_auth.human_time_short(left)} baaki" if left else
          "\n🟢 <b>YouTube cooldown:</b> none")
    po = "✅ ready" if pot_provider.is_ready() else "❌ not running"
    return (
        "🍪 <b>YouTube Auth</b>\n\n"
        f"{yt_auth.auth_status_html()}\n"
        f"🔑 <b>PO-token:</b> {po}{cd}\n\n"
        "• <code>/cookies</code> — cookies.txt upload\n"
        "• <code>/authcheck</code> — live YouTube check\n"
        "• <code>/clearcooldown</code> — 429 cooldown hatao\n"
        "• <code>/ytdlpupdate</code> — yt-dlp upgrade"
    )


@app.on_message(filters.command("cookies") & filters.private & admin_only)
async def cookies_cmd(client: Client, m: Message):
    COOKIE_WINDOW[m.chat.id] = time.time() + COOKIE_WINDOW_SECONDS
    await m.reply(
        auth_panel_text() + "\n\n<b>3 steps</b>\n"
        "1️⃣ Chrome/Firefox mein <code>youtube.com</code> par login karo\n"
        "2️⃣ <b>Get cookies.txt LOCALLY</b> se Netscape format export karo\n"
        "3️⃣ Wo <code>.txt</code> ab yahan <b>File/Document</b> ke roop mein bhejo (15 min window)"
    )


@app.on_message(filters.document & filters.private & admin_only)
async def cookies_upload_handler(client: Client, m: Message):
    doc = m.document
    name = (doc.file_name or "").strip()
    window_open = COOKIE_WINDOW.get(m.chat.id, 0) > time.time()
    if not (window_open or "cookie" in name.casefold()):
        return  # some other file the admin sent — not ours
    if not name.casefold().endswith(".txt"):
        return await m.reply("❌ <b>Galat file type.</b> Netscape export <code>.txt</code> File/Document ke roop mein bhejo.")
    if (doc.file_size or 0) > yt_auth.MAX_COOKIE_FILE_BYTES:
        return await m.reply(f"❌ Cookie file bahut badi hai ({doc.file_size:,} bytes).")

    progress = await m.reply("⏳ <b>Cookies mil gayi</b> · download + validate ho rahi hai…")
    target = yt_auth.configured_cookie_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{m.chat.id}.{m.id}.upload")
    downloaded = tmp
    try:
        async with _cookie_lock:
            got = await asyncio.wait_for(client.download_media(m, file_name=str(tmp)), timeout=120)
            if not got:
                raise RuntimeError("Telegram ne file path nahi diya")
            downloaded = Path(got).resolve()
            # validated BEFORE os.replace -> a bad upload can never erase a working cookie file
            info = yt_auth.install_cookies_file(downloaded, target)
    except asyncio.TimeoutError:
        return await safe_edit(progress, "❌ Cookie download timeout ho gaya. Dobara bhejo.")
    except ValueError as e:
        return await safe_edit(progress, f"⚠️ <b>Cookies change nahi hui</b>\n{esc(str(e))}")
    except Exception as e:
        logger.exception("cookie upload failed")
        return await safe_edit(progress, f"❌ <b>Cookies save nahi hui</b>\n<code>{esc(str(e)[:180])}</code>")
    finally:
        for pth in {tmp, downloaded}:
            if pth != target:
                try:
                    pth.unlink(missing_ok=True)
                except OSError:
                    pass

    COOKIE_WINDOW.pop(m.chat.id, None)
    login = (f"✅ Login markers: <code>{info.auth_cookie_count}</code>" if info.has_login_cookies
             else "⚠️ Login marker nahi mila — agar downloads fail hon to login ke saath dobara export karo")
    text = (f"✅ <b>Cookies loaded</b>\n\n🍪 YouTube rows: <code>{info.youtube_cookie_count}</code>\n{login}\n"
            f"📦 Size: <code>{info.size_bytes:,}</code> bytes\n\n<i>Agli request se hi use hongi, restart nahi chahiye.</i>")
    if yt_auth.cooldown_remaining() > 0:
        await safe_edit(progress, text + "\n\n🌐 Cooldown active tha — live check chala raha hoon…")
        ok, detail = await _live_check()
        if ok:
            yt_auth.clear_cooldown()
        text += ("\n\n✅ Live check pass — cooldown hata diya.\n" + esc(detail) if ok else
                 "\n\n❌ Live check fail — cooldown abhi bhi active.\n" + esc(detail))
    else:
        text += "\n\nTest karne ke liye <code>/authcheck</code> chalao."
    await safe_edit(progress, text)


async def _live_check():
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, partial(yt_auth.probe_youtube_access, base_opts)), timeout=45)
    except asyncio.TimeoutError:
        return False, "YouTube ne 45 second mein jawab nahi diya."


@app.on_message(filters.command("authstatus") & filters.private & admin_only)
async def authstatus_cmd(client: Client, m: Message):
    await m.reply(auth_panel_text())


@app.on_message(filters.command("authcheck") & filters.private & admin_only)
async def authcheck_cmd(client: Client, m: Message):
    wait = await m.reply("🌐 <b>Live check</b> · YouTube se ek baar contact kar raha hoon…")
    ok, detail = await _live_check()
    if ok:
        yt_auth.clear_cooldown()
        await safe_edit(wait, f"✅ <b>Live check pass</b>\n{esc(detail)}")
    else:
        await safe_edit(
            wait,
            f"❌ <b>Live check fail</b>\n{esc(detail)}\n\nFresh <code>/cookies</code> upload karo. HTTP 429 ho to "
            "30–60 min ruko — baar-baar try karne se block lamba ho jata hai.",
        )


@app.on_message(filters.command("clearcooldown") & filters.private & admin_only)
async def clearcooldown_cmd(client: Client, m: Message):
    left = yt_auth.cooldown_remaining()
    yt_auth.clear_cooldown()
    await m.reply(f"✅ Cooldown hata diya (tha: {yt_auth.human_time_short(left)})." if left
                  else "ℹ️ Koi cooldown active nahi tha.")


def _upgrade_ytdlp() -> str:
    import sys
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"],
                       capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:] or "pip failed")
    v = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=30)
    return v.stdout.strip() or "unknown"


@app.on_message(filters.command("ytdlpupdate") & filters.private & admin_only)
async def ytdlpupdate_cmd(client: Client, m: Message):
    wait = await m.reply("⏳ <i>yt-dlp update ho raha hai…</i>")
    try:
        new = await loop.run_in_executor(None, _upgrade_ytdlp)
        await safe_edit(
            wait,
            f"✅ <b>yt-dlp installed:</b> <code>{esc(new)}</code>\n"
            f"Running: <code>{esc(getattr(yt_dlp.version, '__version__', '?'))}</code>\n\n"
            "Naya version use karne ke liye bot ko <b>restart</b> karo.",
        )
    except Exception as e:
        await safe_edit(wait, f"❌ Update fail: <code>{esc(str(e)[:250])}</code>")


# ---------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------
BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "How to use"),
    BotCommand("about", "About this bot"),
    BotCommand("search", "Search YouTube by name"),
    BotCommand("cancel", "Cancel active download"),
    BotCommand("stats", "Your statistics"),
    BotCommand("plans", "💎 View premium plans"),
    BotCommand("myplan", "💎 Your plan & today's usage"),
    BotCommand("referral", "🎁 Invite friends, earn premium"),
]


async def announce(text: str) -> bool:
    """Post a start/stop notice to LOG_CHANNEL. If that isn't set or the send fails, DM the admins
    the exact reason instead — so it's never silently missing again."""
    err = ""
    if not LOG_CHANNEL:
        err = "LOG_CHANNEL set nahi hai (env variable `LOG_CHANNEL` mein -100… ID ya @username daalo)."
    else:
        try:
            try:
                await app.get_chat(LOG_CHANNEL)  # warms the peer cache (session is in-memory)
            except Exception as e:
                logger.info(f"get_chat({LOG_CHANNEL}) failed: {e}")
            await app.send_message(LOG_CHANNEL, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return True
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.warning(f"LOG_CHANNEL '{LOG_CHANNEL}' par message nahi gaya (bot ko channel mein admin banao): {e}")
    for aid in ADMIN_IDS:
        try:
            await app.send_message(aid, f"{text}\n\n⚠️ <b>Log channel par nahi ja saka:</b>\n<code>{esc(err[:300])}</code>")
        except Exception:
            pass
    return False


ADMIN_COMMANDS = [
    BotCommand("admin", "👑 Admin panel"),
    BotCommand("ban", "🚫 Ban a user (/ban <id>)"),
    BotCommand("unban", "✅ Unban a user (/unban <id>)"),
    BotCommand("banned", "🚫 List banned users"),
    BotCommand("broadcast", "📣 Send message to all users"),
    BotCommand("cachestats", "⚡ File cache stats"),
    BotCommand("clearcache", "🗑 Clear file cache"),
    BotCommand("set_channel_id", "📡 Link a backup channel"),
    BotCommand("channel_id", "📡 List linked channels"),
    BotCommand("del_channel_id", "🗑 Unlink backup channel(s)"),
    BotCommand("addpremium", "💎 Give premium (/addpremium <id> <days>)"),
    BotCommand("removepremium", "💎 Remove premium"),
    BotCommand("cookies", "🍪 Upload YouTube cookies"),
    BotCommand("authcheck", "🔎 Live YouTube check"),
    BotCommand("potstatus", "🔑 PO-token status"),
]


async def set_admin_commands():
    """Admins get the public list + admin commands in their own menu; everyone else sees only the public list."""
    for aid in ADMIN_IDS:
        try:
            await app.set_bot_commands(BOT_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=aid))
        except Exception as e:  # admin hasn't pressed /start yet, etc.
            logger.info(f"admin command menu for {aid} not set: {e}")


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    yt_auth.bootstrap_cookies_from_env()
    ck = yt_auth.active_cookie_path()
    logger.info(f"YouTube cookies: {ck if ck else 'none (cookie-free mode)'}")
    start_keep_alive()
    pot_provider.start_background()
    await app.start()
    try:
        await app.set_bot_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"set_bot_commands failed: {e}")
    await set_admin_commands()
    me = await app.get_me()
    logger.info(f"Bot started as @{me.username}")
    await apply_env_force_channels(app)
    if AUTO_DELETE_SECONDS > 0:
        asyncio.ensure_future(auto_delete_worker(app), loop=loop)
        logger.info(f"Auto-delete ON: sent files removed after {AUTO_DELETE_SECONDS}s")
    await announce(f"🟢 <b>Bot started</b> — @{me.username}\n🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')}")

    stop_event = asyncio.Event()
    reason = ["unknown"]

    def _on_signal(sig):
        reason[0] = signal.Signals(sig).name  # SIGTERM = redeploy/restart/stop, SIGINT = Ctrl+C
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):  # e.g. Windows
            pass
    try:
        await stop_event.wait()  # run until told to stop
    finally:
        up = datetime.now() - BOT_START_TIME
        up_txt = f"{up.days}d " if up.days else ""
        up_txt += f"{up.seconds // 3600}h {up.seconds % 3600 // 60}m"
        try:  # a stopping host gives only a few seconds — don't hang on it
            await asyncio.wait_for(
                announce(f"🔴 <b>Bot stopped</b> — @{me.username}\n📌 Reason: <code>{reason[0]}</code>\n⏱ Uptime: {up_txt}"),
                timeout=8)
        except Exception:
            pass
        save_data()
        await app.stop()


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
