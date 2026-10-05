"""
YouTube Downloader Bot  —  Telegram bot (Pyrogram/kurigram + yt-dlp)

Send a YouTube link  ->  pick a quality (or MP3)  ->  get the file in chat.
Welcome / help / about texts follow the same small-caps + emoji style as the
original Faphouse bot.
"""
import asyncio
import copy
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import contextvars
import functools
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import plugin_guard
plugin_guard.check_and_repair()  # fix corrupted PO-token plugin BEFORE yt_dlp is imported

import autotranslate
import pot_provider
import premium_emoji
from lang import LANG_NAMES, LANGS, PICKER_TEXT, tr as _tr
import yt_api
import yt_auth
import yt_dlp
from dotenv import load_dotenv

# Load yt-dlp's plugins (bgutil PO-token provider) ONCE, here in the main thread, before any worker thread
# builds a YoutubeDL. fetch_info() starts several YoutubeDL instances in parallel; if they trigger the plugin
# load at the same moment, the bgutil providers get registered twice ("... already registered" at startup).
try:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _warm:
        _warm.get_info_extractor("Youtube")
except Exception as _e:  # never block startup
    print(f"[plugin-warmup] skipped: {_e}", flush=True)
from pyrogram import Client, StopPropagation, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import ChannelInvalid, FloodWait, InputUserDeactivated, MessageNotModified, PeerIdInvalid, UserIsBlocked, UserNotParticipant
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
API_HASH = os.getenv("API_HASH", "4fdcfab1c7f5e24ae69f3ce6bb234dec").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8609525656:AAF_EOQzfWIGO-fKtjI53fNp9BXgbjm1PHg").strip()
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


# ---- LOG CHANNEL: -100… ID, @username, public link YA private invite link (t.me/+xxxx) — sab chalta hai ----
# Private invite link se Telegram bot ko channel ki ID nahi milti, isliye bot khud ID seekhta hai:
# (1) bot ko us channel mein admin banao -> link match hone par apne aap set, nahi to admins ko "Set karo" button aata hai
# (2) ya channel mein `/setlog` post karo (bot admin ho). ID DB mein save hoti hai, restart par yaad rehti hai.
_LOG_CHANNEL_RAW = (os.getenv("LOG_CHANNEL") or os.getenv("LOG_CHANNEL_ID") or "-1004396123873").strip()
LOG_CHANNEL = _parse_chat_ref(_LOG_CHANNEL_RAW)
# Downloaded video/audio ki copy log channel mein bhi bhejo (band karne ke liye LOG_COPY_VIDEO=0)
LOG_COPY_VIDEO = os.getenv("LOG_COPY_VIDEO", "1").strip().lower() not in ("0", "false", "no", "off")
_inv = re.search(r"(?:(?:t|telegram)\.me/(?:\+|joinchat/)|^\+)([\w-]{8,})/?$", _LOG_CHANNEL_RAW)
LOG_INVITE_HASH = _inv.group(1) if (_inv and LOG_CHANNEL is None) else None  # set only while an invite link is pending
if LOG_CHANNEL is None and not LOG_INVITE_HASH:
    logger.warning(
        f"LOG_CHANNEL '{_LOG_CHANNEL_RAW}' samajh nahi aaya — -100… ID, @username ya t.me link do. Logging band rahegi."
    )
# ---- FORCE SUBSCRIBE: comma-separated @username / t.me links (bot ko un channels mein admin banao) ----
# Example: "@channel1,@channel2,-1001234567890"   (@username, t.me link ya -100… ID; /admin se bhi add/remove hota hai)
FORCE_SUB_RAW = os.getenv("FORCE_SUB", "-1003873749415")
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
# Video Trim: 1 = frame-accurate cuts (re-encodes just the trimmed section), 0 = faster but cuts snap to keyframes.
TRIM_FORCE_KEYFRAMES = os.getenv("TRIM_FORCE_KEYFRAMES", "1").strip() not in ("0", "false", "no")
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
# Seconds between edits of a progress card. Telegram FloodWaits edits when many users download at once.
EDIT_INTERVAL = max(3.0, float(os.getenv("PROGRESS_EDIT_INTERVAL", "8") or 8))
DL_RETRIES = max(0, int(os.getenv("DL_RETRIES", "2") or 0))
# A YouTube connection that stalls right after the first bytes used to sit for the full 30 s socket
# timeout before yt-dlp retried (progress frozen at 0.0%). Downloads now give up on a silent socket
# after DL_SOCKET_TIMEOUT seconds and retry at once; 10 MB range requests avoid long stalled streams.
DL_SOCKET_TIMEOUT = max(3, int(os.getenv("DL_SOCKET_TIMEOUT", "8") or 8))
DL_NET_OPTS = {"socket_timeout": DL_SOCKET_TIMEOUT, "http_chunk_size": 10 * 1024 * 1024}
# Seconds a user must wait between two link messages (0 = off)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "2") or 0)
# JSON file for users / force-join channels / banner / maintenance flag
DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")
# MongoDB (optional). If MONGO_URI is set, all bot data lives in MongoDB; otherwise the JSON file is used.
MONGO_URI = os.getenv("MONGO_URI", "").strip()
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
QR_CODE_URL = os.getenv("QR_CODE_URL", "https://iili.io/nch1Nup.jpg").strip()
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
    """HTML-escape dynamic data (titles, names, ...). Marked so the auto-translator leaves it untouched."""
    return autotranslate.protect(html.escape(str(s or ""), quote=False))


def esc_tr(s) -> str:
    """HTML-escape text that SHOULD be translated into the user's language (bot messages, error texts)."""
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


def hms_full(seconds) -> str:
    """Always HH:MM:SS (zero-padded) — used by Video Trim."""
    seconds = max(0, int(round(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


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
        a = f'<a href="{esc(author_url)}">{smallcaps(author)}</a>' if author_url else autotranslate.protect(smallcaps(author))
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
        f"🎬 <b>{autotranslate.protect(smallcaps(title))}</b>\n\n"
        "<blockquote>"
        f"📄 {smallcaps('File Name')}: {autotranslate.protect(smallcaps(name))}\n"
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


_save_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dbsave")
_save_pending = threading.Event()  # a queued save will pick up the latest state, so extra requests coalesce


def _snapshot_db() -> dict:
    """Deep copy of DB. Runs off the event loop, so retry if a handler mutates DB mid-copy."""
    for _ in range(8):
        try:
            return copy.deepcopy(DB)
        except RuntimeError:  # "dictionary changed size during iteration"
            time.sleep(0.01)
    return DB  # last resort: save the live dict


def _save_now():
    with _DB_LOCK:
        snap = _snapshot_db()
        if _MONGO is not None:
            try:
                _MONGO.save(snap)
                _last_save[0] = time.time()
            except Exception as e:
                logger.error(f"Error saving to MongoDB: {e}")
            return
        try:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
            _last_save[0] = time.time()
        except Exception as e:
            logger.error(f"Error saving {DATA_FILE}: {e}")


def _save_job():
    _save_pending.clear()  # anything changed from here on queues a fresh save
    _save_now()


def save_data(force: bool = True, wait: bool = False):
    """Atomic write. force=False throttles to once per 30 s (used for last_active).
    Called from the event loop it only queues the write (never blocks the bot);
    from other threads, or with wait=True (shutdown), it writes right away."""
    if not force and time.time() - _last_save[0] < 30:
        return
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if wait or not in_loop:
        return _save_now()
    if _save_pending.is_set():
        return
    _save_pending.set()
    _save_executor.submit(_save_job)


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
        bump_stat("new_users")
        DB["users"][uid] = {
            "username": username, "full_name": name, "join_date": now,
            "total_downloads": 0, "last_active": now, "verified": False,
            "new_logged": False,
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
    bump_stat("downloads")
    bump_stat_map("dl_users", str(user_id))
    save_data()


def set_verified(user_id: int, verified: bool = True):
    u = DB["users"].get(str(user_id))
    if u is not None:
        u["verified"] = verified
        save_data()


# ---------------------------------------------------------------------
# 🌐 Language: picked once (after the force-join gate), stored on the user record
# ---------------------------------------------------------------------
def user_lang(user_id):
    """Chosen language code, or None while the user has not picked one yet."""
    code = DB["users"].get(str(user_id), {}).get("lang")
    return code if code in LANG_NAMES else None


def set_user_lang(user_id, code: str):
    u = DB["users"].get(str(user_id))
    if u is not None and code in LANG_NAMES:
        u["lang"] = code
        save_data()


def T(user_id, key: str, default=None, **kw) -> str:
    """Text in the user's language. No language chosen yet -> `default` (the bot's original text),
    or English when no default is given."""
    code = user_lang(user_id)
    if code is None and default is not None:
        return default
    return autotranslate.protect(_tr(code or "en", key, **kw))


# ---------------------------------------------------------------------
# Daily stats / daily report / premium renewal reminders / custom maintenance text
# ---------------------------------------------------------------------
PREMIUM_REMINDER_HOURS = int(os.getenv("PREMIUM_REMINDER_HOURS", "48") or 0)  # 0 = reminders off
DAILY_REPORT_TIME = (os.getenv("DAILY_REPORT_TIME", "23:55") or "").strip()  # IST "HH:MM", "off" disables
_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_date() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def bump_stat(key: str, n: int = 1):
    """Per-day counters (IST day) for the daily report. Only the last 14 days are kept."""
    ds = DB.setdefault("daily_stats", {})
    day = ds.setdefault(_ist_date(), {})
    day[key] = day.get(key, 0) + n
    if len(ds) > 14:
        for k in sorted(ds)[:-14]:
            ds.pop(k, None)
    save_data(force=False)


def _parse_hhmm(text: str):
    try:
        hh, mm = text.strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:
        pass
    return None


def premium_expiring_count() -> int:
    """Paid/active plans (not lifetime) that end within the reminder window."""
    if PREMIUM_REMINDER_HOURS <= 0:
        return 0
    now, n = _now_utc(), 0
    for uid, u in DB["users"].items():
        if u.get("premium_lifetime") or is_admin(int(uid)):
            continue
        until = _parse_dt(u.get("premium_until"))
        if until and now < until <= now + timedelta(hours=PREMIUM_REMINDER_HOURS):
            n += 1
    return n


def daily_report_text(date_str=None) -> str:
    date_str = date_str or _ist_date()
    d = (DB.get("daily_stats") or {}).get(date_str, {})
    users, _downloads, active, uptime = get_stats()
    return (
        "<blockquote>" + f"📈 <b>Daily Report</b> — {date_str}\n\n"
        f"👥 New users: <b>{d.get('new_users', 0)}</b>\n"
        f"📥 Downloads: <b>{d.get('downloads', 0)}</b>\n"
        f"❌ Failures: <b>{d.get('failures', 0)}</b>\n"
        f"💎 Premium activated: <b>{d.get('premium_added', 0)}</b>\n"
        f"⏰ Renewal reminders sent: <b>{d.get('reminders', 0)}</b>\n\n"
        "📊 <b>Totals</b>\n"
        f"• Users: {users:,}\n• Premium: {premium_count():,}\n"
        f"• Expiring in {PREMIUM_REMINDER_HOURS}h: {premium_expiring_count()}\n"
        f"• Active today: {active}\n• Uptime: {uptime}" + "</blockquote>"
    )


def bump_stat_map(key: str, sub: str, n: int = 1):
    """Per-day nested counter, e.g. day['fail_reasons']['private'] / day['dl_users']['<uid>']."""
    ds = DB.setdefault("daily_stats", {})
    day = ds.setdefault(_ist_date(), {})
    mp = day.setdefault(key, {})
    mp[sub] = mp.get(sub, 0) + n
    for old_day in sorted(ds)[:-7]:  # per-user maps are big: keep them 7 days, totals stay 14
        for hk in ("dl_users", "req_users", "fail_users", "limit_users"):
            ds[old_day].pop(hk, None)
    save_data(force=False)


# ---- alerts / abuse / top videos config (all optional, env) -----------------------------------
SPIKE_WINDOW_MIN = int(os.getenv("SPIKE_WINDOW_MIN", "30") or 30)        # look-back window for the failure-rate alert
SPIKE_RATE = float(os.getenv("SPIKE_RATE", "0.4") or 0)                  # alert when failures/total >= this (0 = alerts off)
SPIKE_MIN_EVENTS = int(os.getenv("SPIKE_MIN_EVENTS", "10") or 10)        # ...and at least this many downloads in the window
SPIKE_COOLDOWN_MIN = int(os.getenv("SPIKE_COOLDOWN_MIN", "60") or 60)    # min minutes between two spike alerts
ABUSE_REQS = int(os.getenv("ABUSE_REQS", "100") or 0)                    # requests (links/searches) per user per day (0 = off)
ABUSE_FAILS = int(os.getenv("ABUSE_FAILS", "10") or 0)                   # failed downloads per user per day (0 = off)
TOP_VIDEOS_MAX = 300

_EVENTS = deque(maxlen=3000)   # (timestamp, ok, reason) of recent download results, for the spike alert
_LAST_SPIKE = [0.0]
_ABUSE_ALERTED: set = set()    # (ist_date, uid) already reported to admins

SPIKE_HINTS = {
    "rate_limit": "YouTube ne server IP rate-limit kiya hai — thodi der ruko, /authcheck se cookies dekho.",
    "bot_check": "Cookies expire/blocked lag rahi hain — /authcheck chalao, phir /cookies se nayi upload karo.",
    "forbidden": "yt-dlp purana ho sakta hai — /ytdlpupdate chalao aur /potstatus dekho.",
    "network": "Server ka internet/proxy check karo.",
    "ffmpeg": "ffmpeg ya disk space check karo.",
    "upload": "Telegram upload/flood errors — limits check karo.",
    "timeout": "Server slow/overloaded hai — MAX_CONCURRENT_DOWNLOADS aur network dekho.",
}


def record_request(uid: int):
    """A user sent a link / search (counted per day for abuse detection)."""
    bump_stat_map("req_users", str(uid))
    _check_abuse(uid)


def record_failure(uid: int, reason: str):
    """One failed download: daily totals, per-reason, per-user, spike window, abuse check."""
    bump_stat("failures")
    bump_stat_map("fail_reasons", reason)
    bump_stat_map("fail_users", str(uid))
    _EVENTS.append((time.time(), False, reason))
    _check_abuse(uid)


def record_download(uid: int, url: str, title, size, cache_hit: bool):
    """One delivered file: cache hit/miss, bytes served from cache, top videos, spike window."""
    _EVENTS.append((time.time(), True, ""))
    bump_stat("cache_hits" if cache_hit else "cache_miss")
    if cache_hit and size:
        bump_stat("cache_bytes", int(size))
    try:
        vid = yt_video_id(url)
    except Exception:
        vid = None
    if vid:
        tv = DB.setdefault("top_videos", {})
        e = tv.setdefault(vid, {"t": str(title or "")[:80], "n": 0})
        e["n"] = e.get("n", 0) + 1
        e["last"] = _ist_date()
        if title and not e.get("t"):
            e["t"] = str(title)[:80]
        if len(tv) > TOP_VIDEOS_MAX + 100:  # keep the list small (mutate in place)
            for k, _v in sorted(tv.items(), key=lambda x: x[1].get("n", 0))[: len(tv) - TOP_VIDEOS_MAX]:
                tv.pop(k, None)


def _check_abuse(uid: int):
    """Tell the admins once per day per user when someone crosses the request / failure limit."""
    if not (ABUSE_REQS or ABUSE_FAILS) or is_admin(uid) or uid in BANNED:
        return
    day = (DB.get("daily_stats") or {}).get(_ist_date(), {})
    k = str(uid)
    reqs = (day.get("req_users") or {}).get(k, 0)
    fails = (day.get("fail_users") or {}).get(k, 0)
    hit = (ABUSE_REQS and reqs >= ABUSE_REQS) or (ABUSE_FAILS and fails >= ABUSE_FAILS)
    key = (_ist_date(), uid)
    if not hit or key in _ABUSE_ALERTED:
        return
    if len(_ABUSE_ALERTED) > 2000:
        _ABUSE_ALERTED.clear()
    _ABUSE_ALERTED.add(key)
    text = (f"⚠️ <b>Suspicious user</b>\n\n👤 {_utag(uid)}\n🆔 <code>{uid}</code>\n"
            f"📨 Requests today: <b>{reqs}</b>\n❌ Failures today: <b>{fails}</b>")
    kb = InlineKeyboardMarkup([[make_button("🚫 Ban", f"adm|u|ban|{uid}", style=BTN_DANGER),
                                make_button("👤 User", f"adm|u|show|{uid}", style=BTN_PRIMARY)]])
    try:
        asyncio.ensure_future(_tell_admins(text, kb), loop=loop)
    except Exception as e:
        logger.warning(f"abuse alert failed: {e}")


async def spike_worker(client):
    """Every minute: if the failure rate in the last SPIKE_WINDOW_MIN minutes is too high, DM the admins once."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            ev = [e for e in list(_EVENTS) if e[0] >= now - SPIKE_WINDOW_MIN * 60]
            fails = [e for e in ev if not e[1]]
            if (len(ev) >= SPIKE_MIN_EVENTS and fails and len(fails) / len(ev) >= SPIKE_RATE
                    and now - _LAST_SPIKE[0] >= SPIKE_COOLDOWN_MIN * 60):
                _LAST_SPIKE[0] = now
                cnt = {}
                for e in fails:
                    cnt[e[2]] = cnt.get(e[2], 0) + 1
                top = sorted(cnt.items(), key=lambda x: x[1], reverse=True)[:3]
                lines = "\n".join(f"• {FAIL_LABELS.get(k, k)}: <b>{c}</b>" for k, c in top)
                hint = SPIKE_HINTS.get(top[0][0])
                await _tell_admins(
                    f"🚨 <b>Failure spike</b>\n\nLast {SPIKE_WINDOW_MIN} min: <b>{len(fails)}/{len(ev)}</b> downloads failed "
                    f"({len(fails) / len(ev) * 100:.0f}%)\n\n{lines}" + (f"\n\n💡 {hint}" if hint else ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"spike worker error: {e}")


def parse_duration(tok: str):
    """'30m' / '12h' / '7d' / '2w' -> seconds (max 365 days); None if not a duration."""
    m = re.fullmatch(r"(\d+)\s*([mhdw])", (tok or "").strip().lower())
    if not m:
        return None
    secs = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return secs if 60 <= secs <= 365 * 86400 else None


def _left_text(td) -> str:
    s_ = max(int(td.total_seconds()), 0)
    d, r = divmod(s_, 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h" if d else (f"{h}h {r // 60}m" if h else f"{r // 60}m")


async def tempban_worker(client):
    """Every 30 s: lift temporary bans that have run out (and tell the user)."""
    while True:
        try:
            tb = DB.setdefault("tempbans", {})
            now = _now_utc()
            for k, iso in list(tb.items()):
                until = _parse_dt(iso)
                if until and until > now:
                    continue
                tid = int(k)
                tb.pop(k, None)
                if tid in BANNED:
                    BANNED.remove(tid)
                _BAN_NOTICE_TS.pop(tid, None)
                (DB.get("ban_info") or {}).pop(k, None)
                save_data()
                log_event(f"⏳ <b>Temp-ban ended</b>\n\n👤 {_utag(tid)}")
                try:
                    await client.send_message(tid, SC("✅ Aapka temporary ban khatam ho gaya — ab bot use kar sakte ho."))
                except Exception as e:
                    logger.info(f"temp-ban end notice to {tid} failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"tempban worker error: {e}")
        await asyncio.sleep(30)


FAIL_LABELS = {
    "rate_limit": "⏳ YouTube 429 / rate-limit",
    "bot_check": "🤖 Bot-check (sign in)",
    "private": "🔒 Private video",
    "age": "🔞 Age-restricted",
    "unavailable": "🚫 Unavailable / removed",
    "live": "📡 Live stream",
    "too_big": "📦 File too big",
    "forbidden": "🧱 HTTP 403 / format blocked",
    "timeout": "⌛ Timeout / stalled",
    "network": "🌐 Network error",
    "ffmpeg": "🎞 ffmpeg / merge error",
    "upload": "📤 Telegram upload error",
    "other": "❓ Other",
}


def classify_failure(err) -> str:
    """Short reason key for a download error (same order of checks as friendly_error)."""
    err = str(err or "")
    low = err.lower()
    try:
        if yt_auth.is_rate_limit_error(err) or "rate-limit" in low or "429" in low:
            return "rate_limit"
    except Exception:
        pass
    if "sign in to confirm" in low or "not a bot" in low:
        return "bot_check"
    if "private video" in low:
        return "private"
    if "age" in low and "restrict" in low:
        return "age"
    if "unavailable" in low or "removed" in low:
        return "unavailable"
    if "live" in low and "not supported" in low:
        return "live"
    if "too big" in low or "file too large" in low:
        return "too_big"
    if "403" in low or "forbidden" in low or "requested format" in low:
        return "forbidden"
    if "timed out" in low or "timeout" in low or "stall" in low:
        return "timeout"
    if "ffmpeg" in low or "postprocess" in low or "merge" in low:
        return "ffmpeg"
    if "upload" in low or "file_parts" in low or "flood" in low:
        return "upload"
    if any(k in low for k in ("connection", "network", "urlopen", "ssl", "name resolution", "unreachable")):
        return "network"
    return "other"


def _days_back(n: int) -> list:
    now = datetime.now(_IST)
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def _bar(v, mx, w=10) -> str:
    if mx <= 0 or v <= 0:
        return "░" * w
    f = max(1, round(v / mx * w))
    return "█" * f + "░" * (w - f)


def _ulabel(uid) -> str:
    u = DB["users"].get(str(uid)) or {}
    name = ("@" + u["username"]) if u.get("username") else (u.get("full_name") or str(uid))
    return esc(name[:20]) + (" 🚫" if int(uid) in BANNED else "")


def _range_label(rng: str) -> str:
    return {"1": "Today", "7": "7 days", "14": "14 days", "all": "All-time"}.get(rng, rng)


def _cache_rate(data) -> str:
    h = sum(x.get("cache_hits", 0) for _, x in data)
    mi = sum(x.get("cache_miss", 0) for _, x in data)
    return f"{h / (h + mi) * 100:.0f}%" if (h + mi) else "—"


def dashboard_content(view: str = "ov", rng: str = "7"):
    """Admin dashboard. view: ov (daily downloads) | top (top users) | fail (failure reasons) | ban (banned users)."""
    ds = DB.get("daily_stats") or {}
    mb = make_button
    rows, text = [], ""

    if view == "ov":
        days = _days_back(7)
        data = [(d, ds.get(d, {})) for d in days]
        mx = max([x.get("downloads", 0) for _, x in data] + [1])
        lines = []
        for d, x in data:
            dl, fl = x.get("downloads", 0), x.get("failures", 0)
            lines.append(f"{datetime.strptime(d, '%Y-%m-%d').strftime('%d %b')} {_bar(dl, mx)} {dl:>4}  ❌{fl}")
        tdl = sum(x.get("downloads", 0) for _, x in data)
        tfl = sum(x.get("failures", 0) for _, x in data)
        tnew = sum(x.get("new_users", 0) for _, x in data)
        rate = f"{tdl / (tdl + tfl) * 100:.0f}%" if (tdl + tfl) else "—"
        users, total_dl, active, uptime = get_stats()
        text = (
            "📊 <b>Dashboard — Daily downloads</b> <i>(last 7 days, IST)</i>\n\n"
            "<pre>" + "\n".join(lines) + "</pre>\n"
            f"📥 <b>{tdl:,}</b> downloads · ❌ <b>{tfl:,}</b> failed · ✅ success <b>{rate}</b>\n"
            f"👥 New users: <b>{tnew:,}</b> · ⚡ Cache hit: <b>{_cache_rate(data)}</b>\n\n"
            f"🌍 Totals: {users:,} users · {total_dl:,} downloads · {active} active today · ⏱ {uptime}"
        )
        rng_row = None

    elif view == "top":
        if rng not in ("1", "7", "all"):
            rng = "7"
        if rng == "all":
            pairs = [(int(uid), u.get("total_downloads", 0)) for uid, u in DB["users"].items()]
        else:
            agg = {}
            for d in _days_back(int(rng)):
                for uid, n in (ds.get(d, {}).get("dl_users") or {}).items():
                    agg[uid] = agg.get(uid, 0) + n
            pairs = [(int(uid), n) for uid, n in agg.items()]
        pairs = sorted([x for x in pairs if x[1] > 0], key=lambda x: x[1], reverse=True)[:10]
        mx = pairs[0][1] if pairs else 1
        if pairs:
            body = "\n".join(f"{i}. {_ulabel(uid)} — <b>{n:,}</b> {_bar(n, mx, 6)}\n    <code>{uid}</code>"
                             for i, (uid, n) in enumerate(pairs, 1))
        else:
            body = "Abhi data nahi hai."
        text = f"🏆 <b>Dashboard — Top users</b> <i>({_range_label(rng)})</i>\n\n{body}"
        if rng != "all":
            text += "\n\n<i>Per-user daily data update ke baad se count hota hai.</i>"
        text += "\n\n<i>User kholne ke liye neeche tap karo (wahan Ban/Unban button hai).</i>"
        for uid, n in pairs[:6]:
            rows.append([mb(f"👤 {re.sub(r'<[^>]+>', '', _ulabel(uid))} · {n:,}", f"adm|u|show|{uid}", style=BTN_PRIMARY)])
        rng_row = [("1", "Today"), ("7", "7d"), ("all", "All-time")]

    elif view == "fail":
        if rng not in ("1", "7", "14"):
            rng = "7"
        agg, total_fail = {}, 0
        for d in _days_back(int(rng)):
            x = ds.get(d, {})
            total_fail += x.get("failures", 0)
            for k, n in (x.get("fail_reasons") or {}).items():
                agg[k] = agg.get(k, 0) + n
        tracked = sum(agg.values())
        if total_fail > tracked:  # failures counted before reason tracking existed
            agg["untracked"] = total_fail - tracked
        items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
        tot = sum(n for _, n in items)
        mx = items[0][1] if items else 1
        if items:
            body = "\n".join(
                f"{FAIL_LABELS.get(k, '🕘 Before tracking' if k == 'untracked' else k)}\n"
                f"    {_bar(n, mx, 8)} <b>{n}</b> ({n / tot * 100:.0f}%)"
                for k, n in items)
        else:
            body = "🎉 Koi failure nahi."
        text = f"❌ <b>Dashboard — Failure reasons</b> <i>({_range_label(rng)})</i>\n\nTotal: <b>{tot}</b>\n\n{body}"
        rng_row = [("1", "Today"), ("7", "7d"), ("14", "14d")]

    elif view == "sus":
        if rng not in ("1", "3"):
            rng = "1"
        if not (ABUSE_REQS or ABUSE_FAILS):
            text = "⚠️ <b>Dashboard — Suspicious users</b>\n\nAbuse detection off hai (<code>ABUSE_REQS</code> / <code>ABUSE_FAILS</code> = 0)."
        else:
            tot_r, tot_f, flagged = {}, {}, set()
            for d in _days_back(int(rng)):
                x = ds.get(d, {})
                rq, fl = x.get("req_users") or {}, x.get("fail_users") or {}
                for k in set(rq) | set(fl):
                    tot_r[k] = tot_r.get(k, 0) + rq.get(k, 0)
                    tot_f[k] = tot_f.get(k, 0) + fl.get(k, 0)
                    if (ABUSE_REQS and rq.get(k, 0) >= ABUSE_REQS) or (ABUSE_FAILS and fl.get(k, 0) >= ABUSE_FAILS):
                        flagged.add(k)
            cand = [k for k in flagged if not is_admin(int(k))]
            already = sum(1 for k in cand if int(k) in BANNED)
            cand = sorted([k for k in cand if int(k) not in BANNED], key=lambda k: tot_f.get(k, 0) * 5 + tot_r.get(k, 0), reverse=True)[:8]
            body = "\n".join(f"• {_ulabel(k)} — 📨 <b>{tot_r.get(k, 0)}</b> · ❌ <b>{tot_f.get(k, 0)}</b>\n    <code>{k}</code>" for k in cand) \
                or "✅ Koi suspicious user nahi."
            text = (f"⚠️ <b>Dashboard — Suspicious users</b> <i>({'Today' if rng == '1' else '3 days'})</i>\n\n{body}\n\n"
                    f"<i>Limit (per din): {ABUSE_REQS or '—'} requests ya {ABUSE_FAILS or '—'} failures.</i>"
                    + (f"\n<i>{already} flagged user pehle se banned hai.</i>" if already else ""))
            for i in range(0, len(cand), 2):
                rows.append([mb(f"🚫 Ban {re.sub(r'<[^>]+>', '', _ulabel(k))[:14]}", f"adm|u|ban|{k}", style=BTN_DANGER) for k in cand[i:i + 2]])
        rng_row = [("1", "Today"), ("3", "3 days")]

    elif view == "vid":
        tv = DB.get("top_videos") or {}
        top = sorted(tv.items(), key=lambda x: x[1].get("n", 0), reverse=True)[:8]
        hits = misses = served = 0
        for d in _days_back(7):
            x = ds.get(d, {})
            hits += x.get("cache_hits", 0)
            misses += x.get("cache_miss", 0)
            served += x.get("cache_bytes", 0)
        tot = hits + misses
        rate = f"{hits / tot * 100:.0f}%" if tot else "—"
        body = "\n".join(
            f"{i}. <a href=\"https://youtu.be/{vid}\">{esc((e.get('t') or vid)[:45])}</a> — <b>{e.get('n', 0)}</b>"
            for i, (vid, e) in enumerate(top, 1)) or "Abhi data nahi hai."
        tip = ""
        if FILE_CACHE_MAX and len(FILE_CACHE) >= FILE_CACHE_MAX * 0.9:
            tip = "\n\n💡 Cache lagbhag full hai — <code>FILE_CACHE_MAX</code> badhao."
        elif tot >= 20 and hits / tot < 0.2:
            tip = "\n\n💡 Hit rate kam hai (&lt;20%) — cache abhi zyada kaam nahi aa raha."
        text = (f"🎬 <b>Dashboard — Top videos &amp; cache</b>\n\n<b>Most downloaded</b> <i>(all-time)</i>\n{body}\n\n"
                f"⚡ <b>Cache (7 days)</b>\n• Hit rate: <b>{rate}</b> ({hits:,} hit / {misses:,} miss)\n"
                f"• Served from cache: <b>{human_size(served)}</b>\n• Cached files: <b>{len(FILE_CACHE):,}</b> / {FILE_CACHE_MAX:,}" + tip)
        rng_row = None

    elif view == "prem":
        now_ = _now_utc()
        win = PREMIUM_REMINDER_HOURS or 48
        timed = life = 0
        exp = []
        for uid_, _u in DB["users"].items():
            iu = int(uid_)
            if is_admin(iu):
                continue
            st = get_premium_status(iu)
            if not st["is_premium"]:
                continue
            if st["lifetime"]:
                life += 1
            else:
                timed += 1
                left = st["expires_at"] - now_
                if left <= timedelta(hours=win):
                    exp.append((left, iu))
        exp.sort(key=lambda x: x[0])
        lim = sorted(((k, n) for k, n in (ds.get(_ist_date(), {}).get("limit_users") or {}).items()
                      if not get_premium_status(int(k))["is_premium"]), key=lambda x: x[1], reverse=True)
        added7 = sales7 = sales_n7 = 0
        for d in _days_back(7):
            x = ds.get(d, {})
            added7 += x.get("premium_added", 0)
            sales7 += x.get("sales", 0)
            sales_n7 += x.get("sales_n", 0)
        exp_txt = "\n".join(f"• {_ulabel(u_)} — {_left_text(l_)} left" for l_, u_ in exp[:8]) or "Koi nahi."
        lim_txt = "\n".join(f"• {_ulabel(k)} — {n}x" for k, n in lim[:6]) or "Aaj kisi ne limit hit nahi ki."
        text = (f"💎 <b>Dashboard — Premium</b>\n\n"
                f"• Active (timed): <b>{timed}</b> · Lifetime: <b>{life}</b>\n"
                f"• Activated (7d): <b>{added7}</b>\n"
                f"• 💰 Sales (7d, est.): <b>₹{sales7:,}</b> from {sales_n7} <code>/addpremium</code> plan grants\n\n"
                f"⏰ <b>Expiring in {win}h</b> ({len(exp)})\n{exp_txt}\n\n"
                f"🎯 <b>Free users who hit the daily limit today</b> ({len(lim)})\n{lim_txt}\n\n"
                "<i>Sales = sirf wo grants jo /addpremium se kisi plan ke days se match karein.</i>")
        for _l, u_ in exp[:3]:
            rows.append([mb(f"💎 Renew {re.sub(r'<[^>]+>', '', _ulabel(u_))[:16]}", f"adm|u|show|{u_}", style=BTN_PRIMARY)])
        for k, _n in lim[:3]:
            rows.append([mb(f"🎯 {re.sub(r'<[^>]+>', '', _ulabel(k))[:18]}", f"adm|u|show|{k}", style=BTN_PRIMARY)])
        rng_row = None

    else:  # ban
        view, rng_row = "ban", None
        info = DB.get("ban_info") or {}
        if BANNED:
            lines = []
            for tid in BANNED[-15:][::-1]:
                r = (info.get(str(tid)) or {}).get("reason")
                until = _parse_dt((DB.get("tempbans") or {}).get(str(tid)))
                lines.append(f"• {_ulabel(tid)} <code>{tid}</code>"
                             + (f" ⏳ {_left_text(until - _now_utc())} left" if until else "")
                             + (f"\n    📝 {esc(r[:80])}" if r else ""))
            body = "\n".join(lines)
            for tid in BANNED[-8:][::-1]:
                rows.append([mb(f"✅ Unban {re.sub(r'<[^>]+>', '', _ulabel(tid))}", f"adm|u|unban|{tid}", style=BTN_PRIMARY)])
        else:
            body = "✅ Koi banned user nahi hai."
        text = (f"🚫 <b>Dashboard — Banned users ({len(BANNED)})</b>\n\n{body}\n\n"
                "<i>Commands:</i> <code>/ban &lt;id|@user&gt; [reason]</code> · <code>/tempban &lt;id|@user&gt; 24h [reason]</code> · <code>/unban &lt;id|@user&gt;</code>")

    if rng_row:
        rows.append([mb(("• " if k == rng else "") + lbl, f"adm|dash|{view}|{k}", style=BTN_PRIMARY) for k, lbl in rng_row])
    nav = [("ov", "📈 Overview"), ("top", "🏆 Top users"), ("fail", "❌ Failures"),
           ("sus", "⚠️ Suspicious"), ("vid", "🎬 Videos"), ("prem", "💎 Premium")]
    for i in (0, 3):
        rows.append([mb(lbl, f"adm|dash|{k}|{'1' if k == 'sus' else '7'}", style=BTN_PRIMARY) for k, lbl in nav[i:i + 3]])
    rows.append([mb("🚫 Banned", "adm|dash|ban|7", style=BTN_PRIMARY),
                 mb("🔄 Refresh", f"adm|dash|{view}|{rng}", style=BTN_PRIMARY),
                 mb("🔙 Panel", "adm|panel", style=BTN_PRIMARY)])
    return text, InlineKeyboardMarkup(rows)


async def daily_report_worker(client):
    """Once a day (DAILY_REPORT_TIME, IST) posts the report to the log channel (else DMs the admins)."""
    target = _parse_hhmm(DAILY_REPORT_TIME)
    if not target:
        return
    while True:
        try:
            now = datetime.now(_IST)
            today = now.strftime("%Y-%m-%d")
            if (now.hour, now.minute) >= target and DB.get("last_report_date") != today:
                DB["last_report_date"] = today
                save_data()
                text = daily_report_text(today)
                if LOG_CHANNEL:
                    await _send_log(text)
                else:
                    await _tell_admins(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"daily report failed: {e}")
        await asyncio.sleep(60)


def _fmt_left(rem: timedelta) -> str:
    hours = max(int(rem.total_seconds() // 3600), 0)
    if hours >= 24:
        d, h = divmod(hours, 24)
        return f"{d} din {h} ghante" if h else f"{d} din"
    return f"{hours} ghante" if hours >= 1 else "1 ghante se bhi kam"


def _reminder_due(u: dict, now):
    """expires_at if this user should get the renewal reminder now, else None."""
    if PREMIUM_REMINDER_HOURS <= 0 or u.get("premium_lifetime"):
        return None
    until = _parse_dt(u.get("premium_until"))
    if not until or until <= now or until - now > timedelta(hours=PREMIUM_REMINDER_HOURS):
        return None
    if u.get("expiry_reminded") == until.isoformat():  # already reminded for this expiry date
        return None
    set_at = _parse_dt(u.get("premium_set_at"))
    if set_at and now - set_at < timedelta(hours=12):  # just granted (e.g. 1-day referral reward) — don't nag yet
        return None
    return until


async def run_premium_reminders(client) -> int:
    now, sent = _now_utc(), 0
    for uid, u in list(DB["users"].items()):
        if is_admin(int(uid)):
            continue
        until = _reminder_due(u, now)
        if not until:
            continue
        text = SC(
            "⏰ <b>Premium khatam hone wala hai!</b>\n\n"
            f"💎 Aapka plan <b>{_fmt_left(until - now)}</b> mein expire hoga "
            f"({until.astimezone(_IST).strftime('%d %b %Y, %I:%M %p')} IST).\n"
            "🚀 Abhi renew karo — extend karne par bacha hua time add ho jaata hai, kuch waste nahi hota."
        )
        kb = InlineKeyboardMarkup([[make_button("💎 Renew Now", "show_plans", style=BTN_PRIMARY)]])
        try:
            await client.send_message(int(uid), text, reply_markup=kb)
            sent += 1
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            break  # not marked -> retried in the next round
        except Exception as e:  # blocked / deactivated: don't retry for this expiry date
            logger.info(f"renewal reminder to {uid} failed: {e}")
        u["expiry_reminded"] = until.isoformat()
        await asyncio.sleep(0.1)
    if sent:
        bump_stat("reminders", sent)
    save_data()
    return sent


async def premium_reminder_worker(client):
    await asyncio.sleep(30)
    while True:
        try:
            await run_premium_reminders(client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"premium reminder run failed: {e}")
        await asyncio.sleep(1800)


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
    else:
        u.pop("premium_parallel", None)  # old plan expired: don't carry its parallel count into a new grant
    u["premium_until"] = (base + timedelta(days=days)).isoformat()
    u["premium_set_at"] = _now_utc().isoformat()  # used to hold back the renewal reminder right after a grant
    save_data()


def set_premium(user_id: int, days, parallel=None):
    """days=None -> lifetime. A number extends an active plan instead of replacing it.
    parallel = simultaneous downloads this plan allows (an active better plan is never lowered)."""
    was_premium = get_premium_status(user_id)["is_premium"]
    bump_stat("premium_added")
    _set_premium_days(user_id, days)
    if parallel:
        u = _ensure_user(user_id)
        old = u.get("premium_parallel") or 0
        u["premium_parallel"] = max(old, int(parallel)) if was_premium else int(parallel)
        save_data()


def _set_premium_days(user_id: int, days):
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
        u.pop("premium_parallel", None)
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


def make_button(text, callback_data=None, url=None, style=None, icon=True):
    kw = {"text": text}
    if callback_data:
        kw["callback_data"] = callback_data
    if url:
        kw["url"] = url
    if BUTTON_STYLE_SUPPORTED and style is not None:
        kw["style"] = style
    # premium emoji as the button icon (leading emoji of the label); dropped again if this build can't do it
    label, icon_id = premium_emoji.split_button_icon(text) if icon else (text, None)
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
𝙄𝙣𝙫𝙞𝙩𝙚 𝙛𝙧𝙞𝙚𝙣𝙙𝙨 𝙖𝙣𝙙 𝙚𝙖𝙧𝙣 𝙛𝙧𝙚𝙚 𝙥𝙧𝙚𝙢𝙞𝙪𝙢.

🌐 "/language"
𝘾𝙝𝙖𝙣𝙜𝙚 𝙮𝙤𝙪𝙧 𝙡𝙖𝙣𝙜𝙪𝙖𝙜𝙚.</blockquote>
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
_MAINT_LINE = "━━━━━━━━━━━━━━━━━━"
_MAINT_OWNER = f'<a href="{POWERED_BY_URL}">{_bi(POWERED_BY)}</a>'  # clickable owner name
MAINTENANCE_TEXT = (
    f"🔧 {_bi('Bot Maintenance Mode Mein Hai')}\n\n"
    f"⏳ {_bi('Hamm Kuch Technical Updates Kar Rahe Hain.')} ✨\n"
    f"{_bi('Kripya Thodi Der Baad Dobara Try Karein.')}\n\n"
    f"{_MAINT_LINE}\n\n"
    f"👑 {_bi('Powered by')} {_MAINT_OWNER}\n"
    f"⚡ {_bi('Speed • Performance • Reliability')}\n\n"
    f"{_MAINT_LINE}\n\n"
    f"{_MAINT_OWNER}"
)


def maintenance_text() -> str:
    """Admin's custom message (set from the panel) or the default one."""
    custom = (DB.get("maintenance_text") or "").strip()
    return f"🔧 {custom}" if custom else MAINTENANCE_TEXT


def maintenance_alert_text() -> str:
    """Callback-query alerts are plain text and max 200 chars."""
    custom = (DB.get("maintenance_text") or "").strip()
    if custom:
        return html.unescape(re.sub(r"<[^>]+>", "", custom)).strip()[:190] or "Bot maintenance mein hai."
    return SC("Bot maintenance mein hai. Thodi der baad try karo.")


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

# Everything the bot sends is translated into the language the user picked (see autotranslate.py).
autotranslate.install(user_lang)


@app.on_message(filters.private & filters.text, group=-10)
async def _untranslate_menu_label(client, m):
    """A translated reply-keyboard label (e.g. 'मदद') arrives as that text -> map it back to the original
    label so the existing menu handlers keep matching. Never stops the update."""
    orig = autotranslate.REVERSE.get(autotranslate._norm_label(m.text or ""))
    if orig:
        m.text = orig


PENDING: dict[str, dict] = {}   # token -> request waiting for a quality pick
WAITING: dict[int, str] = {}    # admin_id -> action waiting for their next message
TRIM_WAIT: dict[int, str] = {}  # user_id -> PENDING token waiting for a "HH:MM:SS-HH:MM:SS" trim range
COOLDOWNS: dict[int, float] = {}  # user_id -> time of last accepted link
ACTIVE: dict[int, list] = {}    # user_id -> running jobs (each: cancel event / task / phase)
# Free users: 1 job at a time. Premium users (and admins): up to PREMIUM_MAX_PARALLEL at once.
PREMIUM_MAX_PARALLEL = max(1, int(os.getenv("PREMIUM_MAX_PARALLEL", "2") or 2))  # fallback: premium with no plan recorded (referral reward, old grants)
# Simultaneous downloads per plan, as "price:count" pairs (same prices as PLANS). 0 = unlimited.
PLAN_PARALLEL_RAW = os.getenv("PLAN_PARALLEL", "19:10,29:20,45:30,99:50,999:0")
FREE_PARALLEL = max(1, int(os.getenv("FREE_PARALLEL", "5") or 5))  # simultaneous downloads for free users
MAX_PARALLEL_CAP = 100000   # sanity ceiling for a finite count
UNLIMITED = 10 ** 6           # internal value for "unlimited"


def fmt_parallel(n: int) -> str:
    return "Unlimited ♾️" if n >= UNLIMITED else str(n)


def _parse_plan_parallel(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(","):
        if ":" not in part:
            continue
        price, n = (x.strip() for x in part.split(":", 1))
        if price.isdigit() and n.isdigit():
            out[int(price)] = UNLIMITED if int(n) == 0 else min(int(n), MAX_PARALLEL_CAP)
    return out


PLAN_PARALLEL = _parse_plan_parallel(PLAN_PARALLEL_RAW)


def max_parallel_jobs(uid: int) -> int:
    """Free = FREE_PARALLEL (5). Premium = what their plan includes (admins: the best plan)."""
    if not get_premium_status(uid)["is_premium"]:
        return FREE_PARALLEL
    if is_admin(uid):
        return max([PREMIUM_MAX_PARALLEL] + list(PLAN_PARALLEL.values()))
    n = (DB["users"].get(str(uid)) or {}).get("premium_parallel") or PREMIUM_MAX_PARALLEL
    return max(1, int(n))


def can_start_job(uid: int) -> bool:
    return len(ACTIVE.get(uid, ())) < max_parallel_jobs(uid)


def add_job(uid: int, job: dict):
    ACTIVE.setdefault(uid, []).append(job)


def remove_job(uid: int, job: dict):
    jobs = ACTIVE.get(uid)
    if not jobs:
        return
    try:
        jobs.remove(job)
    except ValueError:
        pass
    if not jobs:
        ACTIVE.pop(uid, None)


def busy_text(uid: int) -> str:
    n = max_parallel_jobs(uid)
    if get_premium_status(uid)["is_premium"] and n > 1:
        return f"Aapke plan mein ek saath max {fmt_parallel(n)} downloads allowed hain. Ek complete hone do, ya /cancel karo.\n\n💎 Zyada chahiye? /plans se bada plan lo."
    if get_premium_status(uid)["is_premium"]:
        return "Pehle wala download complete hone do, ya /cancel karo.\n\n💎 Ek saath zyada downloads ke liye /plans se bada plan lo."
    return f"Free mein ek saath max {fmt_parallel(n)} downloads allowed hain. Ek complete hone do, ya /cancel karo.\n\n💎 Zyada chahiye? /plans se premium lo."


def _utag(uid: int) -> str:
    info = DB["users"].get(str(uid), {})
    # sirf ek hi: username ho to @username (tap karke profile khulti hai), warna naam ka link
    if info.get("username"):
        who = f"@{info['username']}"
    else:
        who = f'<a href="tg://user?id={uid}">{esc(info.get("full_name") or str(uid))}</a>'
    return f'{who} (<code>{uid}</code>)'


_LOG_PEER_WARMED = False


async def _send_log(text: str):
    """Post to LOG_CHANNEL. The session is in-memory, so after every restart the channel's peer
    isn't cached — warm it with get_chat once (and retry on PeerIdInvalid) like fbot's flow."""
    global _LOG_PEER_WARMED
    if not LOG_CHANNEL:
        return
    if not text.lstrip().startswith("<blockquote"):
        text = f"<blockquote>{text}</blockquote>"
    opts = LinkPreviewOptions(is_disabled=True)
    try:
        if not _LOG_PEER_WARMED:
            try:
                await app.get_chat(LOG_CHANNEL)
                _LOG_PEER_WARMED = True
            except Exception as e:
                logger.info(f"get_chat({LOG_CHANNEL}) failed: {e}")
        try:
            await app.send_message(LOG_CHANNEL, text, link_preview_options=opts)
        except (PeerIdInvalid, ChannelInvalid):
            await app.get_chat(LOG_CHANNEL)
            _LOG_PEER_WARMED = True
            await app.send_message(LOG_CHANNEL, text, link_preview_options=opts)
    except Exception as e:
        logger.warning(f"log channel send failed ({LOG_CHANNEL}): {e} — bot ko channel mein admin banao & ID check karo")


def _bind_log_channel(chat_id: int):
    """Remember the numeric ID behind the private invite link (persisted in DB)."""
    global LOG_CHANNEL, _LOG_PEER_WARMED
    LOG_CHANNEL = chat_id
    _LOG_PEER_WARMED = False
    DB["log_channel"] = {"invite_hash": LOG_INVITE_HASH, "chat_id": chat_id}
    save_data()
    logger.info(f"LOG_CHANNEL bound to {chat_id} (from invite link)")


async def resolve_log_channel(client: Client):
    """Startup: when LOG_CHANNEL is a private invite link, restore the ID saved earlier (or try to resolve it)."""
    if LOG_CHANNEL or not LOG_INVITE_HASH:
        return
    saved = DB.get("log_channel") or {}
    if saved.get("invite_hash") == LOG_INVITE_HASH and saved.get("chat_id"):
        _bind_log_channel(int(saved["chat_id"]))
        return
    try:  # bots are usually refused here, but it costs nothing to try
        chat = await client.get_chat(f"https://t.me/+{LOG_INVITE_HASH}")
        if getattr(chat, "id", None):
            _bind_log_channel(chat.id)
            return
    except Exception as e:
        logger.info(f"invite link se channel resolve nahi hua (expected for bots): {e}")
    logger.warning("LOG_CHANNEL invite link pending — bot ko channel mein admin banao ya channel mein /setlog post karo")


async def _tell_admins(text: str, markup=None):
    for aid in ADMIN_IDS:
        try:
            await app.send_message(aid, text, reply_markup=markup)
        except Exception:
            pass


@app.on_chat_member_updated()
async def _log_channel_autodetect(client: Client, upd):
    """Bot just became admin somewhere while the log channel is still an unresolved invite link."""
    try:
        if LOG_CHANNEL or not LOG_INVITE_HASH:
            return
        new = upd.new_chat_member
        if not new or not new.user or not new.user.is_self:
            return
        if new.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
        chat = upd.chat
        title = esc(getattr(chat, "title", None) or str(chat.id))
        link = ""
        try:
            link = getattr(await client.get_chat(chat.id), "invite_link", None) or ""
        except Exception as e:
            logger.info(f"get_chat({chat.id}) after promotion failed: {e}")
        if LOG_INVITE_HASH in link:
            _bind_log_channel(chat.id)
            await _send_log("✅ <b>Log channel connected</b> — ab saare logs yahan aayenge.")
            await _tell_admins(f"✅ Log channel set: <b>{title}</b> (<code>{chat.id}</code>)")
        else:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Haan, yehi log channel hai", callback_data=f"logch|{chat.id}")]])
            await _tell_admins(
                f"📝 Bot ko <b>{title}</b> (<code>{chat.id}</code>) mein admin banaya gaya.\n"
                "Kya yehi tumhara log channel hai? (private link se main confirm nahi kar sakta)", kb)
    except Exception as e:
        logger.warning(f"log channel autodetect error: {e}")


@app.on_callback_query(filters.regex(r"^logch\|-?\d+$"))
async def _cb_set_log_channel(client: Client, cq):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("Sirf admin ye kar sakta hai", show_alert=True)
    if LOG_CHANNEL:
        return await cq.answer("Log channel pehle se set hai", show_alert=True)
    _bind_log_channel(int(cq.data.split("|", 1)[1]))
    await cq.answer("✅ Log channel set ho gaya")
    try:
        await cq.message.edit_text(f"✅ Log channel set: <code>{LOG_CHANNEL}</code>")
    except Exception:
        pass
    await _send_log("✅ <b>Log channel connected</b> — ab saare logs yahan aayenge.")


@app.on_message(filters.channel & filters.command("setlog"))
async def _setlog_in_channel(client: Client, m: Message):
    """Post /setlog inside the channel (bot must be admin) to bind it as the log channel."""
    if LOG_CHANNEL or not LOG_INVITE_HASH:
        return
    _bind_log_channel(m.chat.id)
    await _send_log("✅ <b>Log channel connected</b> — ab saare logs yahan aayenge.")
    await _tell_admins(f"✅ Log channel set: <b>{esc(m.chat.title or '')}</b> (<code>{m.chat.id}</code>)")


def log_event(text: str):
    """Fire-and-forget post to LOG_CHANNEL (no-op when it isn't configured).
    Every log (Download Done, Cache Hit, Failed, Payment Claim, 429, ...) goes out inside a
    <blockquote>; texts that already start with one are left as they are."""
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
        # yt-dlp's PO Token Guide (2026): "web" now serves SABR-only streams (no downloadable https formats ->
        # "Requested format is not available" / ffmpeg 255), so it is NOT used. "mweb" is the client the
        # guide recommends together with a PO-token provider; web_safari gives HLS; web_embedded and
        # android_vr need no token; tv works best with cookies.
        return [["mweb", "web_safari", "web_embedded", "android_vr", "tv", "default"],
                ["mweb"], ["web_safari"], ["web_embedded"], ["android_vr"], ["tv"]]
    # No PO token: android_vr https formats would 403, so leave that client out.
    return [[c for c in s if c != "android_vr"] for s in CLIENT_SETS if s != ["android_vr"]]


_UNSET = object()
# Cookie account chosen for the fetch/download that is running right now (set by _rotating), so all of
# its yt-dlp calls use ONE account. Outside such a call base_opts() just takes the next account.
_CUR_ACC: contextvars.ContextVar = contextvars.ContextVar("cur_cookie_account", default=_UNSET)


def _rotating(fn):
    """Run fn with one cookie account; if that account gets flagged (bot-check / 429) while another
    account is free, retry the whole call on the next account."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        tried: set = set()
        while True:
            acc = yt_auth.acquire(exclude=tried)
            tok = _CUR_ACC.set(acc)
            try:
                return fn(*a, **kw)
            except Exception:
                if (acc is not None and yt_auth.is_cooling(acc.name)
                        and len(tried) < yt_auth.COOKIE_MAX_ACCOUNTS):
                    tried.add(acc.name)
                    logger.info(f"cookie account {acc.name} flagged -> retrying with the next account")
                    continue
                raise
            finally:
                _CUR_ACC.reset(tok)
    return wrapper


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
    # Cookies are optional. Several accounts rotate (see yt_auth pool); re-checked on every call,
    # so a fresh /cookies upload works at once.
    acc = _CUR_ACC.get()
    if acc is _UNSET:
        acc = yt_auth.acquire()
    if acc is not None:
        opts["cookiefile"] = str(acc.path)
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
            f"rok raha hai.\n\n• Fresh/extra cookie accounts: <code>/cookies</code>\n• Check: <code>/authcheck</code>\n"
            f"• Manual clear: <code>/clearcooldown</code>")
    log_event(text)
    for aid in ADMIN_IDS:
        try:
            await app.send_message(aid, text)
        except Exception:
            pass


async def _alert_admins_account(name: str, reason: str, minutes: int):
    rows = yt_auth.pool_status()
    ready = sum(1 for r in rows if not r["left"])
    text = (f"🍪 <b>Cookie account resting</b>\n\n<code>{esc(name)}</code> → {esc(reason)}\n"
            f"Ye account ~{minutes} min aaram karega, baaki accounts kaam sambhal rahe hain.\n"
            f"✅ Ready: {ready}/{len(rows)}\n\n• Status: <code>/authstatus</code> • Test: <code>/authcheck</code>")
    log_event(text)
    for aid in ADMIN_IDS:
        try:
            await app.send_message(aid, text)
        except Exception:
            pass


def _rest_account(err: str, reason: str, minutes: int) -> bool:
    """Blame the cookie account named in the error. True = it now rests and another account can take over."""
    name = yt_auth.account_from_error(err)
    if not name:
        return False
    rotated, newly = yt_auth.mark_account(name, reason, minutes)
    if rotated and newly:
        logger.warning(f"cookie account {name} resting {minutes}m ({reason})")
        try:
            asyncio.run_coroutine_threadsafe(_alert_admins_account(name, reason, minutes), loop)
        except Exception:
            pass
    return rotated


def note_youtube_error(err: str) -> bool:
    """Call with every yt-dlp error text. An explicit 429 starts the shared cooldown (and DMs the
    admins once per incident) - unless the cookie account that hit it can be swapped for another one.
    Returns True for 429 / sign-in wall, where other clients can't help."""
    if yt_auth.is_rate_limit_error(err):
        if _rest_account(err, "HTTP 429", yt_auth.COOKIE_429_COOLDOWN_MIN):
            return True   # another account takes over; the global cooldown only starts if that one fails too
        if yt_auth.activate_cooldown():
            logger.warning("YouTube 429 -> cooldown started")
            try:
                asyncio.run_coroutine_threadsafe(_alert_admins_429(), loop)
            except Exception:
                pass
        return True
    if yt_auth.is_hard_youtube_block(err):  # sign-in wall / bot-check
        _rest_account(err, "bot-check", yt_auth.COOKIE_BOTCHECK_COOLDOWN_MIN)
        return True
    return False


def _err_with_diag(e: Exception, opts: dict) -> str:
    diag = getattr(opts.get("logger"), "context", lambda: "")()
    msg = f"{e} | {diag}" if diag else str(e)
    ck = opts.get("cookiefile")
    if ck:  # tag which cookie account made the call, so note_youtube_error() can blame the right one
        msg += f" [ck:{yt_auth.account_name_for_path(ck)}]"
    return msg


def _max_height(info) -> int:
    return max((f.get("height") or 0 for f in info.get("formats", [])), default=0)


_INFO_CACHE: dict = {}  # url -> (ts, info, clients)
INFO_CACHE_TTL = 600    # seconds; repeat links / playlist re-taps answer instantly
FETCH_PARALLEL = 3      # client sets tried at the same time
FETCH_OK_HEIGHT = 720   # once this is reached and the main sets answered, stop waiting
FETCH_GRACE = 3         # seconds to keep waiting for slower client sets once FETCH_OK_HEIGHT is reached
INFO_CACHE_MAX = 200    # hard cap on cached videos


def _fetch_one(url: str, clients):
    opts = base_opts(clients)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False), None
    except Exception as e:
        return None, _err_with_diag(e, opts)


@_rotating
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
    multi = None  # (info, clients) of the best set that carries 2+ audio languages
    t0 = time.time()
    ex = cf.ThreadPoolExecutor(max_workers=FETCH_PARALLEL)
    try:
        pending = {ex.submit(contextvars.copy_context().run, _fetch_one, url, cs): cs for cs in sets}  # same cookie account in every thread
        done_n = 0
        while pending:
            timeout = FETCH_GRACE if (best is not None and _max_height(best) >= FETCH_OK_HEIGHT) else None
            done, _ = cf.wait(list(pending), timeout=timeout, return_when=cf.FIRST_COMPLETED)
            if not done:  # have an answer and the grace window is over: stop waiting
                logger.info(f"[timing] fetch_info stop-wait at {time.time() - t0:.1f}s (best={_max_height(best)}p)")
                break
            stop = False
            for fut in done:
                clients = pending.pop(fut)
                info, err = fut.result()
                done_n += 1
                if info is None:
                    last_err = RuntimeError(err)
                    logger.info(f"[timing] {time.time() - t0:.1f}s client set {clients} failed: {err[:160]}")
                    if note_youtube_error(err):  # 429 / sign-in wall: more clients only make it worse
                        blocked = stop = True
                        break
                    continue
                h = _max_height(info)
                n_at = len(audio_tracks(info))
                logger.info(f"[timing] {time.time() - t0:.1f}s {info.get('id')}: clients={clients} max_height={h} audio_langs={n_at}")
                if n_at >= 2 and (multi is None or h > _max_height(multi[0])):
                    multi = (info, clients)  # a client set that exposes several audio languages
                if best is None or h > _max_height(best):
                    best, best_clients = info, clients
                if h >= GOOD_ENOUGH_HEIGHT:
                    stop = True
                    break
                # two sets answered and we already have a decent ladder: don't wait for the rest
                if done_n >= 2 and _max_height(best) >= FETCH_OK_HEIGHT:
                    stop = True
                    break
            if stop:
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    logger.info(f"[timing] fetch_info total {time.time() - t0:.1f}s")
    if best is None:
        raise last_err or RuntimeError("Could not fetch video info.")
    # The tallest set often has ONE audio language only (android_vr / tv / web_safari don't list dubs).
    # If another set that answered does list several, use it so the audio-track picker can appear —
    # unless it would cost real resolution.
    if multi is not None and not audio_tracks(best) and _max_height(multi[0]) >= min(_max_height(best), 720):
        logger.info(f"[audio] using client set {multi[1]} for its audio languages")
        best, best_clients = multi
    yt_api.enrich_info(best)  # metadata from the official API (no-op without YT_API_KEY)
    _INFO_CACHE[url] = (time.time(), best, best_clients)
    while len(_INFO_CACHE) > INFO_CACHE_MAX:  # evict oldest first
        _INFO_CACHE.pop(min(_INFO_CACHE, key=lambda k: _INFO_CACHE[k][0]), None)
    return best, best_clients


def _audio_formats(info) -> list:
    return [f for f in info.get("formats") or []
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and f.get("language")]


AUDIO_PROBE = os.getenv("AUDIO_PROBE", "1").strip() not in ("0", "false", "no")


@_rotating
def probe_audio_tracks(url: str):
    """The client set that won fetch_info often lists ONE audio language only (YouTube exposes dubbed /
    multi-language tracks to some clients only). Ask a few other clients in parallel and return the first
    info that really carries 2+ languages, or None."""
    import concurrent.futures as cf
    sets = ([["mweb"]] if pot_provider.is_ready() else []) + [["tv"], ["web_safari"], ["web_embedded"]]
    ex = cf.ThreadPoolExecutor(max_workers=len(sets))
    try:
        futs = [ex.submit(contextvars.copy_context().run, _fetch_one, url, cs) for cs in sets]
        try:
            for fut in cf.as_completed(futs, timeout=45):
                info, err = fut.result()
                n = len(audio_tracks(info)) if info else 0
                logger.info(f"[audio] probe {url}: {n} language(s)" + (f" err={err[:100]}" if err else ""))
                if n >= 2:
                    return info
        except cf.TimeoutError:
            pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return None


def fetch_playlist(url: str) -> dict:
    """Fast flat listing of a playlist (no per-video extraction). API first, yt-dlp fallback."""
    _api = yt_api.playlist(url, PLAYLIST_MAX)
    if _api and _api["entries"]:
        logger.info(f"playlist via API: {len(_api['entries'])} videos")
        return _api
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


def audio_tracks(info) -> list[dict]:
    """Distinct audio languages of a video: [{"code", "label", "default"}], default/original first.
    Returns [] unless the video really has 2+ languages — then the bot skips the audio-track step
    and downloads straight after the quality tap."""
    tracks: dict[str, dict] = {}
    for f in info.get("formats") or []:
        if f.get("vcodec") != "none" or f.get("acodec") in (None, "none"):
            continue
        code = f.get("language")
        if not code:
            continue
        note = str(f.get("format_note") or "")
        low = note.lower()
        if "descriptive" in low:  # audio-description tracks share the language code; skip them
            continue
        label = note.split(",")[0].strip()
        label = re.sub(r"\s*\(?\b(original|default)\b\)?", "", label, flags=re.I).strip() or code
        is_def = ("original" in low or "default" in low
                  or (f.get("language_preference") or 0) > 0)
        t = tracks.setdefault(code, {"code": code, "label": label, "default": False})
        t["default"] = t["default"] or is_def
    if len(tracks) < 2:
        return []
    return sorted(tracks.values(), key=lambda t: (not t["default"], t["label"].lower()))


def _audio_spec(alang) -> tuple[str, str]:
    """(m4a-preferred, any-container) audio selectors, pinned to a language when one was picked."""
    if alang:
        return f"bestaudio[ext=m4a][language={alang}]", f"bestaudio[language={alang}]"
    return "bestaudio[ext=m4a]", "bestaudio"


def format_selector(height: int, vertical: bool, alang=None) -> str:
    k = "width" if vertical else "height"
    base = (
        f"bestvideo[{k}<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[{k}<={height}]+bestaudio/best[{k}<={height}]/best"
    )
    if not alang:
        return base
    a_m4a, a_any = _audio_spec(alang)
    # the chosen language first; if this client's streams do not carry it, fall back to the normal pick
    return (f"bestvideo[{k}<={height}][ext=mp4]+{a_m4a}/"
            f"bestvideo[{k}<={height}]+{a_any}/" + base)


def strict_selector(height: int, vertical: bool, alang=None) -> str:
    """Only streams that really land in (previous ladder step, height] — no silent fallback."""
    k = "width" if vertical else "height"
    prev = max((x for x in LADDER if x < height), default=0)
    pre = ""
    if alang:
        _, a_any = _audio_spec(alang)
        pre = f"bestvideo[{k}<={height}][{k}>{prev}]+{a_any}/"
    return (pre + f"bestvideo[{k}<={height}][{k}>{prev}]+bestaudio/"
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


def _download_impl(url, mode, value, vertical, workdir, hook, clients=None, trim=None, alang=None):
    """mode: 'v' (value = height) or 'a' (value = mp3 bitrate)."""
    check_cooldown()
    # Trim goes through ffmpeg (FFmpegFD), which can't use yt-dlp's 10 MB http chunking and needs a
    # roomier network timeout than the 8s normal downloads use (it seeks into the middle of the stream).
    NET = {"socket_timeout": max(DL_SOCKET_TIMEOUT, 30)} if trim else DL_NET_OPTS
    _t0 = time.time()
    _seen = {"first": False}

    def _timed_hook(d):
        # [timing] lines in `docker logs` show where the start-up wait goes
        if not _seen["first"] and d.get("status") == "downloading":
            _seen["first"] = True
            logger.info(f"[timing] first bytes {time.time() - _t0:.1f}s after download_blocking started "
                        f"(clients={clients}, mode={mode}, value={value})")
        hook(d)

    opts = {
        **base_opts(clients),
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "progress_hooks": [_timed_hook],
        "writethumbnail": True,
        **({} if trim else {"concurrent_fragment_downloads": CONCURRENT_FRAGMENTS}),
        **NET,
    }
    if trim:  # (start_s, end_s): yt-dlp fetches ONLY that section via ffmpeg, not the whole video
        opts["download_ranges"] = yt_dlp.utils.download_range_func([], [(trim[0], trim[1])])
        opts["force_keyframes_at_cuts"] = TRIM_FORCE_KEYFRAMES  # frame-accurate cut (re-encodes the section)
    if mode == "a":
        opts["format"] = f"bestaudio[language={alang}]/bestaudio/best" if alang else "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(value)}
        ]
    else:
        opts["merge_output_format"] = "mp4"

    info = None
    # FAST PATH: the quality picker already fetched this video's info (cached 10 min). Reuse it instead of
    # asking YouTube again (page + player + JS challenge + PO token = many seconds before the first byte).
    # Any problem here just falls through to the normal slow path below.
    _hit = _INFO_CACHE.get(url)
    if _hit and time.time() - _hit[0] <= INFO_CACHE_TTL:
        try:
            if mode == "a":
                _fmt = opts["format"]
            elif int(value) >= 720:
                _fmt = strict_selector(int(value), vertical, alang)
            else:
                _fmt = format_selector(int(value), vertical, alang)
            with yt_dlp.YoutubeDL({**opts, "format": _fmt}) as ydl:
                info = ydl.process_ie_result(copy.deepcopy(_hit[1]), download=True)
            logger.info(f"download: reused cached video info (fast start) [timing] {time.time() - _t0:.1f}s")
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as e:
            info = None
            logger.info(f"cached-info download failed after {time.time() - _t0:.1f}s, using full path: {str(e)[:140]}")
            if note_youtube_error(_err_with_diag(e, opts)):
                raise
    if info is None:
        logger.info(f"[timing] no cached info -> slow path at {time.time() - _t0:.1f}s (url={url})")
    if info is None and mode == "v" and int(value) >= 720:
        # Try to really get the requested height: strict selector across the client sets
        # (web+PO token first). If no client has it, fall through to the normal selector.
        tried = []
        for cs in ([clients] if clients else []) + client_sets():
            if cs in tried:
                continue
            tried.append(cs)
            o = {**opts, **base_opts(cs), **NET, "format": strict_selector(int(value), vertical, alang)}
            try:
                info = _download_with_retry(o, url)
                break
            except yt_dlp.utils.DownloadCancelled:
                raise
            except Exception as e:
                logger.info(f"strict {value}p via {cs} failed: {str(e)[:140]}")
                if note_youtube_error(_err_with_diag(e, o)):
                    break
    if info is None:
        fmt = format_selector(int(value), vertical, alang) if mode == "v" else opts["format"]
        last_err = None
        tried_fb = []
        for cs in ([clients] if clients else []) + client_sets():
            if cs in tried_fb:
                continue
            tried_fb.append(cs)
            o = {**opts, **base_opts(cs), **NET, "format": fmt}
            try:
                info = _download_with_retry(o, url)
                break
            except yt_dlp.utils.DownloadCancelled:
                raise
            except Exception as e:
                last_err = e
                logger.info(f"fallback via {cs} failed: {str(e)[:140]}")
                if note_youtube_error(_err_with_diag(e, o)):
                    break
        if info is None:
            raise last_err

    vid = info.get("id")
    ext = "mp3" if mode == "a" else "mp4"
    path = os.path.join(workdir, f"{vid}.{ext}")
    if not os.path.isfile(path):  # e.g. merge fell back to mkv/webm
        skip = (".part", ".ytdl", ".jpg", ".jpeg", ".png", ".webp")
        files = [
            p for p in glob.glob(os.path.join(workdir, f"{vid}*" if trim else f"{vid}.*"))
            if not p.lower().endswith(skip)
        ]
        if not files:
            raise RuntimeError("Download finished but output file was not found.")
        path = max(files, key=os.path.getsize)
    thumb = make_thumb(workdir, vid, path if mode == "v" else None)
    return path, thumb, info


def _cut_local(path: str, trim, workdir: str) -> str:
    """Fallback trim: cut an already-downloaded full file with ffmpeg. Returns the clipped file path."""
    start, end = trim
    base, ext = os.path.splitext(path)
    out = f"{base}_trim{ext}"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", path,
           "-t", str(end - start)]
    if ext.lower() == ".mp3" or not TRIM_FORCE_KEYFRAMES:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"ffmpeg trim failed: {(r.stderr or '')[-300:]}")
    try:
        os.remove(path)
    except OSError:
        pass
    return out


@_rotating
def download_blocking(url, mode, value, vertical, workdir, hook, clients=None, trim=None, alang=None):
    """Normal download. With trim: first try yt-dlp's section download (only that clip is fetched);
    if that fails for any reason except cancel / 429 / sign-in wall, download the full file and cut it
    locally with ffmpeg, so a trim request still succeeds."""
    if not trim:
        return _download_impl(url, mode, value, vertical, workdir, hook, clients, None, alang)
    try:
        return _download_impl(url, mode, value, vertical, workdir, hook, clients, trim, alang)
    except (yt_dlp.utils.DownloadCancelled, YouTubeCooldown):
        raise
    except Exception as e:
        full = str(e)
        if yt_auth.is_rate_limit_error(full) or yt_auth.is_hard_youtube_block(full):
            raise
        logger.warning(f"trim section download failed ({full[:200]}) -> full download + local ffmpeg cut")
        for f in glob.glob(os.path.join(workdir, "*")):  # drop partial section leftovers
            try:
                os.remove(f)
            except OSError:
                pass
        path, thumb, info = _download_impl(url, mode, value, vertical, workdir, hook, clients, None, alang)
        path = _cut_local(path, trim, workdir)
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
    err = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", err or "")
    err = re.sub(r"\s*\[ck:[A-Za-z0-9_\-]+\]", "", err).strip()
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


    if not await check_subscription(client, u.id):
        return await send_photo_or_text(client, m.chat.id, FORCE_SUB_PHOTO_URL, ACCESS_DENIED_TEXT, subscription_keyboard())
    if not user_lang(u.id):  # joined, but no language yet -> ask once, welcome comes after the pick
        return await send_language_picker(client, m.chat.id)
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
    if WAITING.pop(m.from_user.id, None) or TRIM_WAIT.pop(m.from_user.id, None):  # pending admin / trim input
        return await m.reply(SC("<b>✅ Operation cancelled!</b>"))
    jobs = list(ACTIVE.get(m.from_user.id, ()))
    if not jobs:
        return await m.reply(SC("<b>⚠️ No active download to cancel.</b>"))
    for job in jobs:  # /cancel stops every download this user has running
        job["cancel"].set()
        if job["phase"] != "download" and job["task"]:  # queue / upload phase -> cancel the asyncio task
            job["task"].cancel()
    await m.reply(SC(f"<b>🛑 Cancelling your active download{'s' if len(jobs) > 1 else ''}...</b>"))


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
    # Pehle env wali list parse karo — jo purane env-channels ab FORCE_SUB mein nahi hain unhe DB se hata do
    wanted = set()
    for raw in re.split(r"[,\s]+", FORCE_SUB_RAW.strip()):
        t, i = extract_channel_info(raw) if raw else (None, None)
        if t:
            wanted.add((t, str(i).lower()))
    stale = [c for c in FORCE_CHANNELS if c.get("env") and (
        c.get("type", "public"),
        str(c.get("identifier") or c.get("chat_id") or c.get("invite_hash")).lower()) not in wanted]
    for c in stale:
        FORCE_CHANNELS.remove(c)
        added += 1
        logger.info(f"FORCE_SUB: purana env channel hata diya -> {ch_label(c)}")
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
    # Same channel do baar (ek @username se, ek -100 ID se) ho to duplicate hata do
    pub = {str(c.get("identifier")).lower() for c in FORCE_CHANNELS if c.get("type", "public") == "public"}
    seen_ids = set()
    for c in list(FORCE_CHANNELS):
        if c.get("type") == "id":
            link = (c.get("invite_link") or "").rstrip("/").split("/")[-1].lower()
            if c.get("chat_id") in seen_ids or link in pub:
                FORCE_CHANNELS.remove(c)
                added += 1
                logger.info(f"FORCE_SUB: duplicate channel hata diya -> {ch_label(c)}")
            else:
                seen_ids.add(c.get("chat_id"))
    if added:
        save_data()
        reset_verifications()
        logger.info(f"FORCE_SUB: {added} change(s) applied")


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
    rows.append([make_button(SC("✅ Verify Join"), "vsub", style=BTN_PRIMARY)])
    rows.append([make_button(SC("🔄 Refresh"), "vsub", style=BTN_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def log_new_user_once(user_id: int):
    """Send the #NewUser log exactly once, the first time a brand-new user is through the force-join gate."""
    rec = DB["users"].get(str(user_id))
    if not rec or rec.get("new_logged") is not False:  # old users (no flag) or already logged
        return
    rec["new_logged"] = True
    save_data()
    from types import SimpleNamespace
    log_event(new_user_log_text(SimpleNamespace(id=user_id, first_name=rec.get("full_name") or "", last_name=None)))


async def check_subscription(client: Client, user_id: int) -> bool:
    """True if the user joined every force channel (or none are configured).
    Private invite-link channels can't be checked, so they are trusted on Verify."""
    if not FORCE_CHANNELS or is_verified(user_id):
        log_new_user_once(user_id)
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
    log_new_user_once(user_id)
    return True


async def gate_message(client: Client, m: Message) -> bool:
    """Register user, then enforce maintenance mode + force-join. True = may proceed."""
    u = m.from_user
    register_user(u.id, u.username, full_name(u))
    if DB["maintenance_mode"] and not is_admin(u.id):
        await m.reply(maintenance_text())
        return False
    if not await check_subscription(client, u.id):
        await send_photo_or_text(client, m.chat.id, FORCE_SUB_PHOTO_URL, ACCESS_DENIED_TEXT, subscription_keyboard())
        return False
    return True


async def gate_query(client: Client, query) -> bool:
    u = query.from_user
    register_user(u.id, u.username, full_name(u))
    if DB["maintenance_mode"] and not is_admin(u.id):
        await query.answer(maintenance_alert_text(), show_alert=True)
        return False
    if not await check_subscription(client, u.id):
        await query.answer(SC("Pehle required channels join karo — /start dabao."), show_alert=True)
        return False
    return True


def language_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in LANGS:
        row.append(make_button(label, f"lg|{code}", style=BTN_PRIMARY, icon=False))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([make_button("❌ Close", "lgx", style=BTN_DANGER, icon=False)])
    return InlineKeyboardMarkup(rows)


@app.on_callback_query(filters.regex(r"^lgx$"))
async def language_close_cb(client: Client, query):
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


async def send_language_picker(client: Client, chat_id: int):
    await client.send_message(chat_id, PICKER_TEXT, reply_markup=language_keyboard())


@app.on_message(filters.command("language") & filters.private)
async def language_cmd(client: Client, m: Message):
    """Change the language later (the picker is otherwise shown only once)."""
    if not await gate_message(client, m):
        return
    await send_language_picker(client, m.chat.id)


@app.on_callback_query(filters.regex(r"^lg\|[a-z]{2,3}$"))
async def language_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    code = query.data.split("|")[1]
    if code not in LANG_NAMES:
        return await query.answer()
    u = query.from_user
    register_user(u.id, u.username, full_name(u))
    first_time = not user_lang(u.id)
    set_user_lang(u.id, code)
    msg = autotranslate.protect(_tr(code, "lang_set", name=LANG_NAMES[code]))
    await query.answer(msg)
    try:
        await query.message.delete()
    except Exception:
        pass
    if first_time:  # first pick -> continue with the normal welcome
        await send_welcome(client, query.message.chat.id, u.first_name, u.id)
    else:
        await client.send_message(query.message.chat.id, msg)


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
        if not user_lang(u.id):
            await send_language_picker(client, query.message.chat.id)
        else:
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


def _find_user(q: str):
    """User id (int) from '123456', '@name', 'name' or a t.me/name link; None if unknown."""
    q = (q or "").strip()
    if not q:
        return None
    if q.lstrip("-").isdigit():
        return int(q) if q in DB["users"] else None
    name = re.sub(r"^(?:https?://)?(?:t|telegram)\.me/", "", q).lstrip("@").strip().lower()
    for uid, u in DB["users"].items():
        if (u.get("username") or "").lower() == name:
            return int(uid)
    return None


def user_card(uid: int):
    u = DB["users"].get(str(uid)) or {}
    banned = uid in BANNED
    st = get_premium_status(uid)
    text = (
        "👤 <b>User Info</b>\n\n"
        f"• Name: <a href=\"tg://user?id={uid}\">{esc(u.get('full_name') or 'Unknown')}</a>\n"
        f"• Username: {('@' + esc(u['username'])) if u.get('username') else '—'}\n"
        f"• ID: <code>{uid}</code>\n"
        f"• Joined: {_fmt_dt(u.get('join_date'))}\n"
        f"• Last active: {_fmt_dt(u.get('last_active'))}\n"
        f"• Downloads: {u.get('total_downloads', 0)}\n"
        f"• Verified: {'✅' if u.get('verified') else '❌'}\n"
        f"• Plan: {esc_tr(plan_summary(uid))}\n"
        f"• Referrals: {get_referral_count(uid)}\n"
        f"• Status: {'🚫 Banned' if banned else '🟢 Active'}"
    )
    mb, rows = make_button, []
    if not is_admin(uid):
        rows.append([mb("✅ Unban" if banned else "🚫 Ban", f"adm|u|{'unban' if banned else 'ban'}|{uid}",
                        style=BTN_PRIMARY if banned else BTN_DANGER)])
    rows.append([mb("💎 +7 Days", f"adm|u|prem|{uid}|7", style=BTN_PRIMARY),
                 mb("💎 +30 Days", f"adm|u|prem|{uid}|30", style=BTN_PRIMARY),
                 mb("♾️ Lifetime", f"adm|u|life|{uid}", style=BTN_PRIMARY)])
    if st["is_premium"] and not is_admin(uid):
        rows.append([mb("❌ Remove Premium", f"adm|u|rmprem|{uid}", style=BTN_DANGER)])
    rows.append([mb("🔄 Refresh", f"adm|u|show|{uid}", style=BTN_PRIMARY), mb("🔙 Panel", "adm|panel", style=BTN_PRIMARY)])
    return text, InlineKeyboardMarkup(rows)


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
        f"• Users: {users:,}\n• Verified: {verified:,}\n• Premium: {premium_count():,} (⏰ expiring: {premium_expiring_count()})\n• Downloads: {downloads:,}\n"
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
        [mb("🔧 Maintenance", "adm|maint", style=BTN_PRIMARY), mb("➕ Add Channel", "adm|addch", style=BTN_PRIMARY)],
        [mb("➖ Remove Channel", "adm|rmch", style=BTN_PRIMARY), mb("📋 Channels List", "adm|chlist", style=BTN_PRIMARY)],
        [mb("👥 Users List", "adm|users", style=BTN_PRIMARY), mb("🖼️ Set Banner", "adm|banner", style=BTN_PRIMARY)],
        [mb("🔄 Reset Verifications", "adm|reset", style=BTN_PRIMARY), mb("📊 Export Users", "adm|export", style=BTN_PRIMARY)],
        [mb("🔍 Find User", "adm|usearch", style=BTN_PRIMARY), mb("📈 Today's Report", "adm|report", style=BTN_PRIMARY)],
        [mb("📝 Maint. Message", "adm|mtext", style=BTN_PRIMARY), mb("🍪 YouTube Auth", "adm|auth", style=BTN_PRIMARY)],
        [mb("📊 Dashboard", "adm|dash|ov|7", style=BTN_PRIMARY), mb("🚫 Banned Users", "adm|dash|ban|7", style=BTN_PRIMARY)],
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
    st = BTN_DANGER if "cancel" in label.lower() else BTN_PRIMARY
    return InlineKeyboardMarkup([[make_button(label, "adm|panel", style=st)]])


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
            rows.append([make_button(f"❌ Remove {name}", f"adm|rm|{i}", style=BTN_PRIMARY)])
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

    elif act == "report":
        await safe_edit(msg, daily_report_text(), _back_kb())

    elif act == "usearch":
        WAITING[uid] = "user_search"
        await safe_edit(
            msg,
            "🔍 <b>Find User</b>\n\nUser ki <b>ID</b> ya <b>@username</b> bhejo.\n"
            "(Sirf wahi users milenge jinhone bot start kiya hai.)\n\nType /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    elif act == "dash":
        view = parts[2] if len(parts) > 2 else "ov"
        rng = parts[3] if len(parts) > 3 else "7"
        text, kb = dashboard_content(view, rng)
        await safe_edit(msg, text[:4000], kb)

    elif act == "u":
        sub, tid = parts[2], int(parts[3])
        if str(tid) not in DB["users"]:
            return await query.answer("User nahi mila!", show_alert=True)
        note = None
        if sub == "ban":
            if is_admin(tid):
                return await query.answer("Admin ko ban nahi kar sakte!", show_alert=True)
            if tid not in BANNED:
                BANNED.append(tid)
                save_data()
                try:
                    await client.send_message(tid, SC("🚫 Aapko is bot se ban kar diya gaya hai."))
                except Exception as e:
                    logger.info(f"ban notice to {tid} failed: {e}")
            note = "User banned!"
        elif sub == "unban":
            if tid in BANNED:
                BANNED.remove(tid)
                _BAN_NOTICE_TS.pop(tid, None)
                (DB.get("ban_info") or {}).pop(str(tid), None)
                (DB.get("tempbans") or {}).pop(str(tid), None)
                save_data()
                try:
                    await client.send_message(tid, SC("✅ Aapko unban kar diya gaya hai — ab bot use kar sakte ho."))
                except Exception as e:
                    logger.info(f"unban notice to {tid} failed: {e}")
            note = "User unbanned!"
        elif sub in ("prem", "life"):
            days = int(parts[4]) if sub == "prem" else None
            set_premium(tid, days, parallel_for_days(days))
            label = f"{days} days" if days else "Lifetime ♾️"
            try:
                await client.send_message(tid, SC(f"🎉 You've been given Premium ({label}) by the admin!\n⚡ Ek saath {fmt_parallel(max_parallel_jobs(tid))} downloads chala sakte ho."))
            except Exception as e:
                logger.warning(f"couldn't notify {tid} about premium grant: {e}")
            note = f"Premium granted: {label}"
        elif sub == "rmprem":
            remove_premium(tid)
            note = "Premium removed!"
        text, kb = user_card(tid)
        await safe_edit(msg, text, kb)
        if note:
            await query.answer(note)

    elif act == "mtext":
        custom = (DB.get("maintenance_text") or "").strip()
        await safe_edit(
            msg,
            "📝 <b>Maintenance Message</b>\n\n<b>Abhi users ko ye dikhta hai:</b>\n"
            f"{maintenance_text()}\n\n"
            + ("✏️ Custom message set hai." if custom else "ℹ️ Default message chal raha hai."),
            InlineKeyboardMarkup([
                [make_button("✏️ Set New", "adm|mtext_set", style=BTN_PRIMARY),
                 make_button("♻️ Reset Default", "adm|mtext_reset", style=BTN_PRIMARY)],
                [make_button("🔙 Back", "adm|panel", style=BTN_PRIMARY)],
            ]),
        )

    elif act == "mtext_set":
        WAITING[uid] = "maint_text"
        await safe_edit(
            msg,
            "📝 <b>New Maintenance Message</b>\n\nWo text bhejo jo maintenance mein users ko dikhana hai.\n"
            "• Telegram formatting (bold, italic) chalegi\n• Max 800 characters\n"
            "• Example: <i>Server upgrade chal raha hai, 2 ghante mein wapas aayenge</i>\n\nType /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    elif act == "mtext_reset":
        DB["maintenance_text"] = None
        save_data()
        await query.answer("Default message restore ho gaya!")
        await safe_edit(
            msg,
            f"📝 <b>Maintenance Message</b>\n\n<b>Abhi users ko ye dikhta hai:</b>\n{maintenance_text()}\n\nℹ️ Default message chal raha hai.",
            InlineKeyboardMarkup([
                [make_button("✏️ Set New", "adm|mtext_set", style=BTN_PRIMARY),
                 make_button("♻️ Reset Default", "adm|mtext_reset", style=BTN_PRIMARY)],
                [make_button("🔙 Back", "adm|panel", style=BTN_PRIMARY)],
            ]),
        )

    elif act == "bcast":
        WAITING[uid] = "bcast"
        await safe_edit(
            msg,
            "📢 <b>Broadcast Message</b>\n\nSend me the message to broadcast to all users:\n"
            "• Text, photo, video, document or audio\n• Telegram formatting is kept\n\n"
            f"Total users: {len(DB['users']):,}\n\nType /cancel to cancel.",
            _back_kb("🔙 Cancel"),
        )

    try:  # some branches already answered above; a 2nd answer must not raise
        await query.answer()
    except Exception:
        pass


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

    elif action == "user_search":
        tid = _find_user(m.text or "")
        if tid is None:
            await m.reply("❌ <b>User nahi mila!</b>\n\nSahi ID ya @username bhejo (user ne bot start kiya hona chahiye).\nType /cancel to cancel.")
            return m.stop_propagation()  # keep waiting
        WAITING.pop(uid, None)
        text, kb = user_card(tid)
        await m.reply(text, reply_markup=kb)

    elif action == "maint_text":
        raw = m.text
        if not raw or not str(raw).strip():
            await m.reply("❌ <b>Invalid input!</b>\n\nSirf text bhejo.")
            return m.stop_propagation()  # keep waiting
        new_html = (getattr(raw, "html", None) or str(raw)).strip()
        if len(str(raw)) > 800:
            await m.reply("❌ <b>Bahut lamba hai!</b>\n\nMax 800 characters.")
            return m.stop_propagation()  # keep waiting
        DB["maintenance_text"] = new_html
        save_data()
        WAITING.pop(uid, None)
        await m.reply(f"✅ <b>Maintenance message updated!</b>\n\nUsers ko ab ye dikhega:\n{maintenance_text()}")
        await send_panel(client, m.chat.id)

    elif action == "bcast":
        WAITING.pop(uid, None)  # clear first: a 2nd message during a long broadcast must not start another one
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
SEARCH_CACHE: dict[str, dict] = {}  # key -> {query, results, exhausted, user_id, qkey, exclude}
# Per user + per query: video ids already shown, so searching the same thing again gives NEW results.
SEARCH_SEEN: dict[tuple, dict] = {}  # (uid, qkey) -> {"ids": set, "ts": float}
SEARCH_SEEN_HOURS = float(os.getenv("SEARCH_SEEN_HOURS", "24") or 24)  # 0 = don't remember
SEARCH_SEEN_MAX = 3000


def _qkey(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def _seen_ids(uid: int, qkey: str) -> set:
    if SEARCH_SEEN_HOURS <= 0:
        return set()
    e = SEARCH_SEEN.get((uid, qkey))
    if not e:
        return set()
    if time.time() - e["ts"] > SEARCH_SEEN_HOURS * 3600:
        SEARCH_SEEN.pop((uid, qkey), None)
        return set()
    return set(e["ids"])


def _mark_seen(uid: int, qkey: str, ids):
    if SEARCH_SEEN_HOURS <= 0 or not ids:
        return
    e = SEARCH_SEEN.setdefault((uid, qkey), {"ids": set(), "ts": time.time()})
    e["ids"].update(ids)
    e["ts"] = time.time()
    while len(SEARCH_SEEN) > SEARCH_SEEN_MAX:  # keep memory bounded (oldest first)
        SEARCH_SEEN.pop(next(iter(SEARCH_SEEN)), None)
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
    """Flat metadata-only search (fast, no per-video fetch). Blocking. API first, yt-dlp fallback."""
    _api = yt_api.search(query, count)
    if _api:
        return [{"id": r["id"], "title": r["title"][:70], "uploader": r["uploader"],
                 "duration": hms(r["duration_s"]) if r["duration_s"] > 0 else ""} for r in _api]
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
        row.append(make_button("".join(d + "\ufe0f\u20e3" for d in str(i + 1)), f"ys|{i}|{key}", style=BTN_PRIMARY))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(make_button("◀️ Prev", f"yp|{page - 1}|{key}", style=BTN_PRIMARY))
    nav.append(make_button(f"📄 Page {page + 1}", "ys|n|x", style=BTN_PRIMARY))
    if not exhausted or end < len(results):
        nav.append(make_button("Next ▶️", f"yp|{page + 1}|{key}", style=BTN_PRIMARY))
    rows.append(nav)
    rows.append([make_button(SC("❌ Close"), f"x|{key}", style=BTN_DANGER)])
    return InlineKeyboardMarkup(rows)


async def do_search(m: Message, uid: int, query: str):
    status = await m.reply(SC("🔍 <b>Searching YouTube...</b>"))
    qkey = _qkey(query)
    exclude = _seen_ids(uid, qkey)  # results this user already got for this same search
    want = min(SEARCH_CHUNK_SIZE + len(exclude), SEARCH_MAX)
    try:
        raw = await loop.run_in_executor(None, search_youtube, query, want)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Search failed</b>\n\n") + esc_tr(friendly_error(str(e))))
    results = [r for r in raw if r["id"] not in exclude]
    if not results:
        if raw and exclude:  # everything was shown before -> start over next time
            SEARCH_SEEN.pop((uid, qkey), None)
            return await safe_edit(status, SC(
                "✅ <b>Is search ke saare results aap pehle dekh chuke ho.</b>\n\n"
                "Naya keyword try karo, ya yahi dobara search karo — shuru se dikhaunga."))
        return await safe_edit(status, SC("❌ <b>No results for:</b> ") + f"<i>{esc(query)}</i>")
    key = uuid.uuid4().hex[:12]
    exhausted = len(raw) < want
    SEARCH_CACHE[key] = {"query": query, "results": results, "exhausted": exhausted, "user_id": uid,
                         "qkey": qkey, "exclude": exclude}
    while len(SEARCH_CACHE) > 500:  # keep memory bounded
        SEARCH_CACHE.pop(next(iter(SEARCH_CACHE)), None)
    _mark_seen(uid, qkey, [r["id"] for r in results[:SEARCH_PAGE_SIZE]])  # page 1 is what they see now
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
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
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
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    results, exhausted = cached["results"], cached["exhausted"]

    if not exhausted and page * SEARCH_PAGE_SIZE >= len(results):
        await query.answer(SC("⏳ Loading more results..."))  # a callback can be answered only once
        exclude = cached.get("exclude") or set()
        want = min(len(results) + len(exclude) + SEARCH_CHUNK_SIZE, SEARCH_MAX)
        try:
            more = await loop.run_in_executor(None, search_youtube, cached["query"], want)
        except Exception as e:
            logger.warning(f"search 'load more' failed: {e}")
            return
        seen, added = {r["id"] for r in results}, 0
        for r in more:
            if r["id"] not in seen and r["id"] not in exclude:
                results.append(r)
                seen.add(r["id"])
                added += 1
        if added == 0 or want >= SEARCH_MAX or len(more) < want:
            exhausted = cached["exhausted"] = True
        if page * SEARCH_PAGE_SIZE >= len(results):  # nothing new -> stay on the last page
            page = max(0, (len(results) - 1) // SEARCH_PAGE_SIZE)
    else:
        await query.answer()
    _mark_seen(cached["user_id"], cached.get("qkey") or _qkey(cached["query"]),
               [r["id"] for r in results[page * SEARCH_PAGE_SIZE:(page + 1) * SEARCH_PAGE_SIZE]])
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
    record_request(uid)

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


def video_menu_caption(info: dict, options, uid=None) -> str:
    """Quality-picker card: title + blockquote details (by, duration, views, likes, uploaded) + qualities."""
    title = str(info.get("title") or "video")
    if len(title) > 150:
        title = title[:147] + "…"
    author = info.get("uploader") or info.get("channel")
    author_url = info.get("uploader_url") or info.get("channel_url")
    lines = []
    if author:
        a = f'<a href="{esc(author_url)}">{esc(author)}</a>' if author_url else esc(author)
        lines.append(f"👤 {smallcaps('By')}: {a}")
    dur = info.get("duration")
    lines.append(f"⏱ {smallcaps('Duration')}: {hms(dur) if dur else smallcaps('Unknown')}")
    if info.get("view_count"):
        lines.append(f"👁 {smallcaps('Views')}: {info['view_count']:,}")
    if info.get("like_count"):
        lines.append(f"👍 {smallcaps('Likes')}: {info['like_count']:,}")
    if info.get("comment_count"):
        lines.append(f"💬 {smallcaps('Comments')}: {info['comment_count']:,}")
    cats = info.get("categories") or []
    if cats:
        lines.append(f"🏷️ {smallcaps('Category')}: {smallcaps(', '.join(cats[:2]))}")
    ud = str(info.get("upload_date") or "")
    if len(ud) == 8 and ud.isdigit():
        lines.append(f"📅 {smallcaps('Uploaded')}: {ud[:4]}-{ud[4:6]}-{ud[6:]}")
    q_lines = []
    for h, label in options:
        name = {2160: "4K", 1440: "2K"}.get(h, f"{h}p")
        q_lines.append(f"✅ {name}")
    q_lines.append("🎵 MP3 (audio)")
    return (
        f"🎬 <b>{esc(title)}</b>\n\n"
        "<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"
        f"{T(uid, 'avail', smallcaps('Available qualities:'))}\n"
        "<blockquote>" + "\n".join(q_lines) + "</blockquote>\n\n"
        f"👇 {T(uid, 'tap', smallcaps('Tap a quality below to download:'))}"
    )


def quality_rows(token: str, heights) -> list:
    """Video quality buttons (2 per row) followed by the 4 MP3 buttons."""
    rows, row = [], []
    for h in heights:
        name = {2160: "4K", 1440: "2K"}.get(h, f"{h}p")
        row.append(make_button(f"🎬 {name}", f"q|{token}|v|{h}", style=BTN_PRIMARY))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        make_button("🎵 MP3 64k", f"q|{token}|a|64", style=BTN_PRIMARY),
        make_button("🎵 MP3 128k", f"q|{token}|a|128", style=BTN_PRIMARY),
    ])
    rows.append([
        make_button("🎵 MP3 256k", f"q|{token}|a|256", style=BTN_PRIMARY),
        make_button("🎵 MP3 320k", f"q|{token}|a|320", style=BTN_PRIMARY),
    ])
    return rows


async def _audio_probe_task(token: str, info: dict, url: str):
    """Background: find the audio languages of a video whose fetched info shows only one."""
    try:
        pinfo = await loop.run_in_executor(None, probe_audio_tracks, url)
    except Exception as e:
        logger.info(f"[audio] probe failed: {str(e)[:120]}")
        return
    req = PENDING.get(token)
    if not pinfo or req is None:
        return
    have = {f.get("format_id") for f in info.get("formats") or []}
    for f in _audio_formats(pinfo):  # add the language streams to the info the download will reuse
        if f.get("format_id") not in have:
            info.setdefault("formats", []).append(f)
    tracks = audio_tracks(info)
    if len(tracks) >= 2:
        req["atracks"] = tracks
        logger.info(f"[audio] {url}: {len(tracks)} audio languages found by probe")


async def show_video_menu(m: Message, uid: int, url: str):
    """Fetch one video's info and show the quality picker (links + search results)."""
    status = await m.reply(T(uid, "fetching", SC("🔍 <b>Fetching video info...</b>")))
    try:
        info, clients = await loop.run_in_executor(None, fetch_info, url)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Failed to fetch video</b>\n\n") + esc_tr(friendly_error(str(e))))

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
        "duration": int(info.get("duration") or 0),
        "qopts": [h for h, _ in quality_options(info)],
        "atracks": audio_tracks(info),  # [] = single audio track -> download right after the quality tap
    }

    if AUDIO_PROBE and not PENDING[token]["atracks"]:
        PENDING[token]["probe"] = asyncio.ensure_future(_audio_probe_task(token, info, url), loop=loop)

    rows = quality_rows(token, PENDING[token]["qopts"])
    if PENDING[token]["duration"] > 1:  # ✂️ Video Trim sits right under the video qualities
        rows.insert(len(rows) - 2, [make_button(T(uid, "trim_btn"), f"tr|{token}", style=BTN_PRIMARY)])
    if PENDING[token]["playlist_url"]:
        rows.append([make_button(SC("📚 Full Playlist"), f"pl|{token}", style=BTN_PRIMARY)])
    rows.append([make_button(SC("❌ Close"), f"x|{token}", style=BTN_DANGER)])

    caption = video_menu_caption(info, quality_options(info), uid)
    markup = InlineKeyboardMarkup(rows)
    thumb_url = info.get("thumbnail")
    try:
        if not thumb_url:
            raise ValueError("no thumbnail")
        sent = await m.reply_photo(thumb_url, caption=caption, reply_markup=markup)
        PENDING[token]["menu_ref"] = (sent.chat.id, sent.id)
        await status.delete()
    except Exception:
        await safe_edit(status, caption, markup)
        PENDING[token]["menu_ref"] = (status.chat.id, status.id)


PL_LADDER = [(144, "144p"), (240, "240p"), (360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p"), (1440, "2K"), (2160, "4K")]


def _cleanup_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > 3600]:
        PENDING.pop(k, None)


async def send_playlist_menu(client: Client, status: Message, pl_url: str, uid: int):
    try:
        pl = await loop.run_in_executor(None, fetch_playlist, pl_url)
    except Exception as e:
        return await safe_edit(status, SC("❌ <b>Failed to fetch playlist</b>\n\n") + esc_tr(friendly_error(str(e))))
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
        if not can_start_job(uid):
            return await safe_edit(status, SC("⚠️ <b>" + esc_tr(busy_text(uid)).replace("/cancel", "<code>/cancel</code>") + "</b>"))
        if await over_daily_limit(client, uid):
            return await safe_edit(status, SC("⚠️ <b>Aaj ki free limit khatam (ya baaki downloads abhi chal rahe hain)!</b>"))
        await safe_edit(status, SC(f"📚 <b>{esc(pl['title'])}</b>\n\n🎞 Videos: {len(pl['entries'])}\n🎬 Quality: {default_name} (default)\n\n") + trim_note)
        return start_playlist(client, status.chat.id, uid, req, "v", str(default_q))

    PENDING[token] = req
    rows = [[make_button(f"⭐ Start • {default_name} (default)", f"pq|{token}|v|{default_q}", style=BTN_PRIMARY)]]
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
        make_button("🎵 MP3 64k", f"pq|{token}|a|64", style=BTN_PRIMARY),
        make_button("🎵 MP3 128k", f"pq|{token}|a|128", style=BTN_PRIMARY),
    ])
    rows.append([
        make_button("🎵 MP3 256k", f"pq|{token}|a|256", style=BTN_PRIMARY),
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
    add_job(uid, job)
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
        return await query.answer(T(query.from_user.id, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    if req["user_id"] != query.from_user.id:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
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
        return await query.answer(T(query.from_user.id, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    uid = query.from_user.id
    if req["user_id"] != uid:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    if not can_start_job(uid):
        return await query.answer(busy_text(uid), show_alert=True)
    if await over_daily_limit(client, uid):
        return await query.answer(SC("⚠️ Aaj ki free limit khatam (ya baaki downloads abhi chal rahe hain)!"), show_alert=True)
    if not can_start_job(uid):  # re-check: another tap may have started a job during the await
        return await query.answer(busy_text(uid), show_alert=True)
    PENDING.pop(token, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()
    start_playlist(client, query.message.chat.id, uid, req, mode, value)


# ---------------------------------------------------------------------
# ✂️ Video Trim:  [Video Trim] -> user sends "HH:MM:SS-HH:MM:SS" -> pick quality -> only that clip is downloaded
# ---------------------------------------------------------------------
_TRIM_RE = re.compile(r"^\s*(\d+(?::\d{1,2}){1,2})\s*(?:-|–|—|to)\s*(\d+(?::\d{1,2}){1,2})\s*$", re.I)


def parse_clock(txt: str):
    """'1:02:03' / '02:03' -> seconds, or None if invalid."""
    parts = [int(x) for x in txt.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    else:
        h, (m, s) = 0, parts
    if (len(parts) == 3 and m > 59) or s > 59:
        return None
    return h * 3600 + m * 60 + s


def parse_trim_range(text: str):
    """-> (start_s, end_s) or None when the text is not a valid range string."""
    mt = _TRIM_RE.match(text or "")
    if not mt:
        return None
    a, b = parse_clock(mt.group(1)), parse_clock(mt.group(2))
    return None if a is None or b is None else (a, b)


def trim_prompt_text(duration: int, uid=None) -> str:
    return (
        f"<b>{T(uid, 'trim_btn')}</b>\n\n"
        f"{T(uid, 'trim_dur')}: 00:00:00 - {hms_full(duration)}\n\n"
        f"{T(uid, 'trim_ask')}\n"
        "<code>HH:MM:SS-HH:MM:SS</code>\n\n"
        f"{T(uid, 'trim_ex')}: <code>00:00:00-{hms_full(duration)}</code>"
    )


@app.on_callback_query(filters.regex(r"^tr\|"))
async def trim_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    if not req or not req.get("duration"):
        return await query.answer(T(query.from_user.id, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    if req["user_id"] != query.from_user.id:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    await query.answer()
    TRIM_WAIT[query.from_user.id] = token
    old = req.pop("trim_prompt", None)  # tapped twice -> drop the previous prompt
    if old:
        try:
            await client.delete_messages(*old)
        except Exception:
            pass
    sent = await query.message.reply(
        trim_prompt_text(req["duration"], query.from_user.id),
        reply_markup=InlineKeyboardMarkup([[make_button(T(query.from_user.id, "cancel_btn", SC("❌ Cancel")), f"trx|{token}", style=BTN_DANGER)]]),
    )
    req["trim_prompt"] = (sent.chat.id, sent.id)


@app.on_callback_query(filters.regex(r"^trx\|"))
async def trim_cancel_cb(client: Client, query):
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    if req and req["user_id"] != query.from_user.id:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    if TRIM_WAIT.get(query.from_user.id) == token:
        TRIM_WAIT.pop(query.from_user.id, None)
    if req:
        req.pop("trim_prompt", None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


def _trim_wait_filter(_, __, m: Message) -> bool:
    uid = m.from_user.id if m.from_user else None
    if uid not in TRIM_WAIT or not m.text:
        return False
    if TRIM_WAIT[uid] not in PENDING:  # menu expired
        TRIM_WAIT.pop(uid, None)
        return False
    return not (m.text.startswith("/") or _is_menu_text(m.text))  # commands / menu buttons work as usual


@app.on_message(filters.private & filters.create(_trim_wait_filter), group=-1)
async def trim_range_handler(client: Client, m: Message):
    uid = m.from_user.id
    token = TRIM_WAIT[uid]
    req = PENDING[token]
    if not await gate_message(client, m):
        return m.stop_propagation()
    dur = req["duration"]
    rng = parse_trim_range(m.text)
    err = None
    if not rng:
        err = T(uid, "trim_fmt") + " <code>HH:MM:SS-HH:MM:SS</code>"
    else:
        start, end = rng
        if end > dur and end - dur <= 2:  # rounding slack on the video length
            end = dur
        if start >= end:
            err = T(uid, "trim_order")
        elif end > dur:
            err = T(uid, "trim_long", dur=hms_full(dur))
    if err:
        await m.reply(err + f"\n\n{T(uid, 'trim_ex')}: <code>00:00:00-{hms_full(dur)}</code>")
        return m.stop_propagation()  # keep waiting for a valid range

    TRIM_WAIT.pop(uid, None)
    req["trim"] = (start, end)
    for ref in (req.pop("trim_prompt", None), req.pop("menu_ref", None)):  # tidy: prompt + old full-video menu
        if ref:
            try:
                await client.delete_messages(*ref)
            except Exception:
                pass
    rows = quality_rows(token, req.get("qopts") or [])
    rows.append([make_button(SC("❌ Close"), f"x|{token}", style=BTN_DANGER)])
    await m.reply(
        f"<b>{T(uid, 'trim_btn')}</b>\n\n"
        f"🎬 <b>{esc(req['title'][:100])}</b>\n"
        f"⏱ <code>{hms_full(start)}-{hms_full(end)}</code> ({hms_full(end - start)})\n\n"
        f"👇 {T(uid, 'trim_pick')}",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return m.stop_propagation()


@app.on_callback_query(filters.regex(r"^x\|"))
async def close_menu_cb(client, query):
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    if req and req["user_id"] != query.from_user.id:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    PENDING.pop(token, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


async def edit_menu_message(msg: Message, text: str, markup):
    """Edit a picker message in place — caption if it is a photo card, text otherwise."""
    try:
        if msg.photo:
            await msg.edit_caption(text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup,
                                link_preview_options=LinkPreviewOptions(is_disabled=True))
        return True
    except MessageNotModified:
        return True
    except Exception as e:
        logger.debug(f"menu edit failed: {e}")
        return False


def audio_rows(token: str, tracks: list) -> list:
    rows, row = [], []
    for i, t in enumerate(tracks):
        mark = "⭐ " if t["default"] else ""
        row.append(make_button(f"🔊 {mark}{t['label'][:24]}", f"al|{token}|{i}", style=BTN_PRIMARY))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([make_button(SC("⬅️ Back"), f"ab|{token}", style=BTN_PRIMARY),
                 make_button(SC("❌ Close"), f"x|{token}", style=BTN_DANGER)])
    return rows


async def _begin_download(client: Client, query, token: str, req: dict, mode: str, value: str):
    """Reserve a job slot and start the download. Shared by the quality tap (single audio track)
    and the audio-language tap (multi-audio videos)."""
    uid = query.from_user.id
    if not can_start_job(uid):
        return await query.answer(busy_text(uid), show_alert=True)
    # Reserve the slot BEFORE any await, so a double-tap can't start extra jobs.
    job = {"cancel": threading.Event(), "task": None, "phase": "queue"}
    add_job(uid, job)
    PENDING.pop(token, None)
    try:
        limited = await over_daily_limit(client, uid, in_flight=len(ACTIVE.get(uid, ())) - 1)  # own job is already added
    except Exception:
        remove_job(uid, job)
        raise
    if limited:
        remove_job(uid, job)
        PENDING[token] = req  # let the user retry this request later
        return await query.answer(SC("⚠️ Aaj ki free limit khatam (ya baaki downloads abhi chal rahe hain)!"), show_alert=True)

    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()

    job["task"] = asyncio.ensure_future(
        process_job(client, query.message.chat.id, uid, req, mode, value, job), loop=loop
    )


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
        return await query.answer(T(query.from_user.id, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    uid = query.from_user.id
    if req["user_id"] != uid:
        return await query.answer(T(query.from_user.id, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)

    # Video has several audio languages -> ask which one AFTER the quality tap.
    # Single-track video -> no extra step, download starts right away.
    _probe = req.get("probe")
    if mode == "v" and not req.get("atracks") and _probe is not None and not _probe.done():
        try:  # the background audio-language check is still running: give it a moment
            await asyncio.wait_for(asyncio.shield(_probe), 20)
        except Exception:
            pass
    tracks = (req.get("atracks") or []) if mode == "v" else []  # MP3 -> never ask, download directly
    if tracks:
        req["pending_q"] = (mode, value)
        # remember the current card so "Back" can restore it
        _src = query.message.caption or query.message.text
        req["menu_backup"] = ((_src.html if _src else ""), query.message.reply_markup)
        name = f"{value}p" if mode == "v" else f"MP3 {value}k"
        text = (f"🔊 <b>{smallcaps('Select audio language')}</b>\n\n"
                f"🎬 <b>{esc(req['title'][:100])}</b>\n"
                f"🎞 {smallcaps('Quality')}: <b>{esc(name)}</b>\n\n"
                f"👇 {smallcaps('This video has multiple audio tracks. Pick one:')}")
        if await edit_menu_message(query.message, text, InlineKeyboardMarkup(audio_rows(token, tracks))):
            return await query.answer()
        # could not edit the card -> fall back to the default track instead of failing
    await _begin_download(client, query, token, req, mode, value)


@app.on_callback_query(filters.regex(r"^al\|"))
async def audio_lang_cb(client: Client, query):
    if not await gate_query(client, query):
        return
    try:
        _, token, idx = query.data.split("|")
        idx = int(idx)
    except ValueError:
        return await query.answer()
    req = PENDING.get(token)
    uid = query.from_user.id
    if not req or not req.get("pending_q"):
        return await query.answer(T(uid, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    if req["user_id"] != uid:
        return await query.answer(T(uid, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    tracks = req.get("atracks") or []
    if not 0 <= idx < len(tracks):
        return await query.answer()
    req["alang"], req["alabel"] = tracks[idx]["code"], tracks[idx]["label"]
    mode, value = req["pending_q"]
    await _begin_download(client, query, token, req, mode, value)


@app.on_callback_query(filters.regex(r"^ab\|"))
async def audio_back_cb(client: Client, query):
    """Back from the audio-language step to the quality list."""
    token = query.data.split("|")[1]
    req = PENDING.get(token)
    uid = query.from_user.id
    if not req:
        return await query.answer(T(uid, "expired", SC("Link expire ho gaya — dobara bhejo.")), show_alert=True)
    if req["user_id"] != uid:
        return await query.answer(T(uid, "not_yours", SC("Ye aapka request nahi hai.")), show_alert=True)
    backup = req.pop("menu_backup", None)
    req.pop("pending_q", None)
    req.pop("alang", None)
    req.pop("alabel", None)
    if backup:
        await edit_menu_message(query.message, backup[0], backup[1])
    await query.answer()


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


def cache_key_for(url: str, mode: str, value, alang=None) -> str | None:
    vid = yt_video_id(url)
    if not vid:
        return None
    return f"{vid}::{mode}{value}" + (f"::{alang}" if alang else "")


def cache_has(url: str, mode: str, value, alang=None) -> bool:
    k = cache_key_for(url, mode, value, alang)
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


async def send_from_cache(client, chat_id, uid, url, mode, value, status=None, prefix="", alang=None) -> bool:
    """True if the file was delivered from cache. False = not cached / stale → caller downloads normally."""
    key = cache_key_for(url, mode, value, alang)
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
    record_download(uid, url, ent.get("title"), ent.get("size"), True)
    if not get_premium_status(uid)["is_premium"]:
        bump_daily_count(uid)
    log_event(f"⚡ <b>Cache Hit</b>\n\n👤 {_utag(uid)}\n🎬 {esc(ent['title'])}\n"
              f"🎞 {ent['label']} • 📦 {human_size(ent['size'])}\n🔗 {esc(url)}")
    if BACKUP_CHANNELS or BACKUP_CHANNEL_IDS or (LOG_CHANNEL and LOG_COPY_VIDEO):
        asyncio.ensure_future(backup_to_linked_channels(client, sent.chat.id, sent.id), loop=loop)
    return True


async def run_one(client, chat_id, uid, url, mode, value, vertical, clients,
                  title, job, status, prefix="", trim=None, alang=None, alabel=None):
    """Download one video/audio and upload it. Raises on failure. trim = (start_s, end_s) or None."""
    if not trim and await send_from_cache(client, chat_id, uid, url, mode, value, status, prefix, alang):
        return {}
    check_cooldown()  # a 429 is per server IP: don't wait in the queue just to hit it again
    label = f"{value}p" if mode == "v" else f"MP3 {value}k"
    if alang:
        label += f" 🔊 {alabel or alang}"
    if trim:
        label += f" ✂️ {hms_full(trim[0])}-{hms_full(trim[1])}"
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
        if now - last_edit[0] < EDIT_INTERVAL:
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
            await asyncio.sleep(max(2.0, EDIT_INTERVAL / 2) if time.time() - w0 < 10 else EDIT_INTERVAL)
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
        if now - up_last[0] < EDIT_INTERVAL and current != total:
            return
        up_last[0] = now
        el = now - up_state["t0"]
        sp = current / el if el > 0 else 0
        eta = (total - current) / sp if sp > 0 and total else 0
        await safe_edit(status, progress_text(prefix, False, title, label, current, total, sp, el, eta,
                                              tag=part_tag[0]))

    u_name, u_username, dl_seconds = "", "", 0.0

    async def _load_user():  # runs while the download is starting, not before it
        try:
            _u = await client.get_users(uid)
            return " ".join(x for x in (_u.first_name, _u.last_name) if x), (_u.username or "")
        except Exception:
            return "", ""

    user_fut = asyncio.ensure_future(_load_user(), loop=loop)

    try:
        async with download_semaphore(priority=get_premium_status(uid)["is_premium"]):
            job["phase"] = "download"
            dl_start = time.time()
            prog["t0"] = dl_start  # elapsed counts from the very start, so it never jumps back
            # the real progress card appears NOW (0%), not after YouTube answers
            await safe_edit(status, progress_text(prefix, True, title, label, 0, 0, 0, 0, 0))
            dl_fut = loop.run_in_executor(
                None, partial(download_blocking, url, mode, value, vertical, workdir, hook, clients, trim, alang)
            )
            while True:
                done_set, _ = await asyncio.wait({dl_fut}, timeout=3)
                if done_set:
                    break
                now_t = time.time()
                if not prog["started"] and now_t - last_edit[0] >= EDIT_INTERVAL:  # still contacting YouTube: keep the card alive
                    last_edit[0] = now_t
                    await safe_edit(status, progress_text(prefix, True, title, label, 0, 0, 0, now_t - dl_start, 0))
                if now_t - dl_start > DL_HARD_TIMEOUT:
                    prog["abort"] = "timeout"
                    raise RuntimeError(f"Download timed out after {int(now_t - dl_start) // 60} min — skipped.")
                if prog["started"] and not prog["finished"] and now_t - prog["ts"] > (max(DL_STALL_TIMEOUT, 300) if trim else DL_STALL_TIMEOUT):
                    prog["abort"] = "stalled"
                    raise RuntimeError(f"Download stalled ({DL_STALL_TIMEOUT}s no data) — skipped.")
            path, thumb, info = dl_fut.result()
            dl_seconds = time.time() - dl_start
        u_name, u_username = await user_fut

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
            if n > 1 or trim:  # a trimmed file's real length/size differs from the full video's metadata
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
            if n == 1 and not trim:  # a split upload has no single file_id; trimmed clips are never cached
                media = sent_msg.audio if mode == "a" else sent_msg.video
                ckey = cache_key_for(url, mode, value, alang)
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
            if BACKUP_CHANNELS or BACKUP_CHANNEL_IDS or (LOG_CHANNEL and LOG_COPY_VIDEO):  # copy in background, never delays the user
                asyncio.ensure_future(backup_to_linked_channels(client, sent_msg.chat.id, sent_msg.id), loop=loop)
            try:
                os.remove(part)  # free disk as we go
            except OSError:
                pass
        increment_downloads(uid)
        record_download(uid, url, title, size, False)
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
                      req.get("clients"), req["title"], job, status, trim=req.get("trim"),
                      alang=req.get("alang"), alabel=req.get("alabel"))
        try:
            await status.delete()
        except Exception:
            pass
    except (asyncio.CancelledError, yt_dlp.utils.DownloadCancelled):
        await safe_edit(status, T(uid, "dl_cancelled", SC("🛑 <b>Download cancelled.</b>")))
    except TooBig as e:
        await safe_edit(status, SC("❌ <b>File too big</b>\n\n") + esc(str(e)))
        record_failure(uid, "too_big")
        log_event(f"❌ <b>File Too Big</b>\n\n👤 {_utag(uid)}\n🔗 {esc(req['url'])}\n{esc(str(e)[:300])}")
    except YouTubeCooldown as e:
        await safe_edit(status, SC("🛑 <b>YouTube cooldown</b>\n\n") + esc(str(e)))
    except Exception as e:
        if job["cancel"].is_set() or "cancelled by user" in str(e):
            await safe_edit(status, T(uid, "dl_cancelled", SC("🛑 <b>Download cancelled.</b>")))
        else:
            logger.exception("job failed")
            record_failure(uid, classify_failure(str(e)))
            log_event(f"❌ <b>Download Failed</b>\n\n👤 {_utag(uid)}\n🔗 {esc(req['url'])}\n<code>{esc(str(e)[:400])}</code>")
            await safe_edit(
                status,
                SC("❌ <b>Download failed</b>\n\n") + esc_tr(friendly_error(str(e)))
                + SC("\n\nDobara link bhejke try karo."),
            )
    finally:
        remove_job(uid, job)


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
                record_failure(uid, "too_big")
            except YouTubeCooldown:
                cooldown_hit = True
                break
            except Exception as ex:
                if job["cancel"].is_set() or "cancelled by user" in str(ex):
                    cancelled = True
                    break
                logger.warning(f"playlist item {e['id']} failed: {ex}")
                failed.append((e["title"], friendly_error(str(ex))[:80]))
                record_failure(uid, classify_failure(str(ex)))
                if yt_auth.cooldown_remaining() > 0:  # this item just hit a 429 -> stop, don't hammer
                    cooldown_hit = True
                    break
            await asyncio.sleep(1)  # be gentle with Telegram flood limits
    except asyncio.CancelledError:
        cancelled = True
    finally:
        remove_job(uid, job)

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


def parallel_for_price(price: int) -> int:
    return PLAN_PARALLEL.get(price, PREMIUM_MAX_PARALLEL)


def parallel_for_days(days) -> int:
    """Which plan does a manual /addpremium grant correspond to? Exact match first,
    otherwise the longest plan that is not longer than the grant, else the fallback."""
    for price, d in PLANS:
        if d == days:
            return parallel_for_price(price)
    if days is not None:
        best = None
        for price, d in PLANS:
            if d is not None and d <= days and (best is None or d > best[0]):
                best = (d, price)
        if best:
            return parallel_for_price(best[1])
    return PREMIUM_MAX_PARALLEL


def plan_summary(user_id: int) -> str:
    st = get_premium_status(user_id)
    par = f" · ⚡ {fmt_parallel(max_parallel_jobs(user_id))} parallel" if st["is_premium"] else ""
    if st["lifetime"]:
        return "Premium (Lifetime ♾️)" + par
    if st["is_premium"]:
        left = (st["expires_at"] - _now_utc()).days + 1
        return f"Premium ({left} day{'s' if left != 1 else ''} left)" + par
    if DAILY_FREE_LIMIT:
        return f"Free ({get_daily_count(user_id)}/{DAILY_FREE_LIMIT} today)"
    return "Free"


async def over_daily_limit(client: Client, user_id: int, in_flight=None) -> bool:
    """True if a free user can't start another download today. Downloads still running count
    against the quota too (it is only bumped when a file is delivered), otherwise 5 parallel
    jobs could overshoot the daily limit. The referral/upgrade prompt is sent only when the
    quota is really used up. in_flight = jobs already running (default: all of the user's)."""
    if not DAILY_FREE_LIMIT or get_premium_status(user_id)["is_premium"]:
        return False
    used = get_daily_count(user_id)
    if in_flight is None:
        in_flight = len(ACTIVE.get(user_id, ()))
    if used + in_flight < DAILY_FREE_LIMIT:
        return False
    if used >= DAILY_FREE_LIMIT:
        bump_stat_map("limit_users", str(user_id))
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
    lines = "\n".join(f"• ₹{price} → {_plan_label(days)} · ⚡ {fmt_parallel(parallel_for_price(price))} downloads ek saath"
                      for price, days in PLANS) or "• Admin se contact karo"
    free = ""
    if DAILY_FREE_LIMIT or PLAYLIST_FREE_MAX:
        free = (f"🆓 Free: {DAILY_FREE_LIMIT or '∞'} downloads/day"
                f", ek saath max {FREE_PARALLEL}"
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
        "✅ ⚡ Plan ke hisab se multiple downloads ek saath\n"
        "✅ 🎯 Priority queue — tumhara kaam pehle\n\n"
        + free + lines + "\n\n" + pay + "👇 Plan pe tap karo — shuru ho jao!"
    )


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = [[make_button(SC(f"💎 ₹{price} - {_plan_label(days)}"), f"plan_{price}", style=BTN_PRIMARY)]
            for price, days in PLANS]
    rows.append([make_button(SC("📸 Send Payment Proof"), url=POWERED_BY_URL, style=BTN_PRIMARY)])
    rows.append([make_button(SC("⬅️ Back"), "plans_back", style=BTN_PRIMARY)])
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
        [make_button(SC("⬅️ Back"), "plans_back", style=BTN_PRIMARY)],
    ])


@app.on_callback_query(filters.regex(r"^plan_\d+$"))
async def plan_selected_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    if price not in PLAN_DAYS:
        return await query.answer(SC("Ye plan ab available nahi hai."), show_alert=True)
    how = (f"📱 Kisi bhi UPI app se pay karo (PhonePe / GPay / Paytm):\n<code>{esc(UPI_ID)}</code>\n\n"
           if UPI_ID else "📱 Payment details ke liye admin se contact karo.\n\n")
    qr = "🔗 §§§\n\n" if QR_CODE_URL else ""
    text = (
        f"💳 <b>{_plan_label(PLAN_DAYS[price])}</b> ke liye Payment\n\n"
        f"Amount: ₹{price}\n\n" + how + qr +
        "Payment ke baad 'I've Paid' dabao aur screenshot admin ko bhejo."
    )
    text = SC(text)
    if QR_CODE_URL:  # inserted after SC() so "Scan to Pay" keeps normal letters
        text = text.replace("§§§", f'{smallcaps("QR Code")}: <a href="{esc(QR_CODE_URL)}">Scan to Pay</a>')
    await query.answer()
    await send_photo_or_text(client, query.message.chat.id, PLANS_PHOTO_URL, text, payment_keyboard(price))


PAID_CLAIM_COOLDOWN = int(os.getenv("PAID_CLAIM_COOLDOWN", "300") or 300)  # seconds between "I've Paid" claims per user
_PAID_TS: dict[int, float] = {}


@app.on_callback_query(filters.regex(r"^paid_\d+$"))
async def paid_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    if price not in PLAN_DAYS:
        return await query.answer(SC("Ye plan ab available nahi hai."), show_alert=True)
    days = PLAN_DAYS[price]
    user = query.from_user
    _now = time.time()
    if _now - _PAID_TS.get(user.id, 0) < PAID_CLAIM_COOLDOWN:
        return await query.answer(SC("⏳ Claim already bheja ja chuka hai. Thodi der baad dobara try karo."), show_alert=True)
    _PAID_TS[user.id] = _now
    if len(_PAID_TS) > 2000:  # keep the dict small
        for k in [k for k, t in _PAID_TS.items() if _now - t > PAID_CLAIM_COOLDOWN]:
            _PAID_TS.pop(k, None)
    name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    mention = f'<a href="tg://user?id={user.id}"><code>{esc(name)}</code></a>'
    log_event(f"🔔 <b>Payment Claim</b>\n\n👤 {_utag(user.id)}\nPlan: ₹{price} - {_plan_label(days)}")
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(
                admin_id,
                "<blockquote>" + SC("🔔 <b>Payment Claim</b>\n\n"
                   f"User: {mention} (<code>{user.id}</code>)\n"
                   f"Plan: ₹{price} - {_plan_label(days)}\n\n"
                   f"Screenshot verify karke chalao:\n<code>/addpremium {user.id} {days or 'lifetime'}</code>") + "</blockquote>",
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
    # icon=False: emoji stays as plain text in the label, so buttons stay compact
    return InlineKeyboardMarkup([
        [make_button(SC("🔗 Get Link"), "ref_getlink", style=BTN_PRIMARY, icon=False)],
        [make_button(SC("📤 Share Link"), url=share, style=BTN_PRIMARY, icon=False)],
        [make_button(SC(f"👥 Referrals: {count}"), "ref_count", style=BTN_PRIMARY, icon=False)],
        [make_button(SC("💎 Rewards"), "ref_rewards", style=BTN_PRIMARY, icon=False),
         make_button(SC("💎 Plans"), "show_plans", style=BTN_PRIMARY, icon=False)],
        [make_button(SC("📞 Contact Admin"), url=POWERED_BY_URL, style=BTN_PRIMARY, icon=False)],
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


REFERRAL_DAILY_CAP = int(os.getenv("REFERRAL_DAILY_CAP", "30") or 30)  # max credited referrals per referrer per 24h (0 = off)


def _referral_capped(referrer_id: int) -> bool:
    if not REFERRAL_DAILY_CAP:
        return False
    u = DB["users"].get(str(referrer_id)) or {}
    cutoff = time.time() - 86400
    return len([t for t in u.get("ref_times", []) if t > cutoff]) >= REFERRAL_DAILY_CAP


def _referral_note(referrer_id: int):
    u = DB["users"].get(str(referrer_id))
    if u is None:
        return
    cutoff = time.time() - 86400
    u["ref_times"] = [t for t in u.get("ref_times", []) if t > cutoff][-REFERRAL_DAILY_CAP * 2 or None:] + [time.time()]


async def process_referral(client: Client, m: Message):
    """Called for brand-new users only: /start ref_<id> credits the referrer."""
    if len(m.command) < 2 or not m.command[1].startswith("ref_"):
        return
    try:
        referrer_id = int(m.command[1][4:])
    except ValueError:
        return
    if _referral_capped(referrer_id):  # farming guard: too many joins for one referrer in 24h
        logger.info(f"referral cap hit for {referrer_id}; join not credited")
        return
    if not set_referrer(m.from_user.id, referrer_id):
        return
    _referral_note(referrer_id)
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
    usage = SC("⚠️ <b>Usage:</b> <code>/addpremium &lt;user_id&gt; &lt;days|lifetime&gt; [parallel|0=unlimited]</code>")
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
    if len(m.command) >= 4:  # optional manual override of the parallel-download count
        try:
            parallel = int(m.command[3])
        except ValueError:
            return await m.reply(usage)
        if not 0 <= parallel <= MAX_PARALLEL_CAP:
            return await m.reply(SC(f"⚠️ Parallel 0 (unlimited) ya 1 se {MAX_PARALLEL_CAP} ke beech hona chahiye."))
        if parallel == 0:
            parallel = UNLIMITED
    else:
        parallel = parallel_for_days(days)
    set_premium(target, days, parallel)
    price = next((pr for pr, d in PLANS if d == days), None)  # plan-matched grant = a sale (estimate)
    if price:
        bump_stat("sales", price)
        bump_stat("sales_n")
    await m.reply(SC(f"✅ Premium granted to <code>{target}</code> — {note}.\nPlan: {plan_summary(target)}"))
    try:
        await client.send_message(target, SC(f"🎉 You've been given Premium ({note}) by the admin!\n⚡ Ek saath {fmt_parallel(max_parallel_jobs(target))} downloads chala sakte ho."))
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


def _in_maintenance(_, __, m) -> bool:
    u = getattr(m, "from_user", None)
    return bool(u) and bool(DB.get("maintenance_mode")) and not is_admin(u.id)


maintenance_filter = filters.create(_in_maintenance)


@app.on_message(filters.private & maintenance_filter, group=-1)
async def maintenance_message_block(client: Client, m: Message):
    """Runs before every other handler: during maintenance non-admins get the maintenance notice on every message, nothing else."""
    u = m.from_user
    register_user(u.id, u.username, full_name(u))  # count new users even during maintenance
    try:
        await m.reply(maintenance_text())
    except Exception:
        pass
    raise StopPropagation


@app.on_callback_query(maintenance_filter, group=-1)
async def maintenance_callback_block(client: Client, query):
    u = query.from_user
    register_user(u.id, u.username, full_name(u))
    try:
        await query.answer(maintenance_alert_text(), show_alert=True)
    except Exception:
        pass
    raise StopPropagation


def _needs_language(_, __, upd) -> bool:
    """True for a normal user who has not picked a language yet (admins, banned and maintenance users
    are handled elsewhere)."""
    u = getattr(upd, "from_user", None)
    if not u or u.is_bot or is_admin(u.id) or u.id in BANNED or DB.get("maintenance_mode"):
        return False
    return not user_lang(u.id)


nolang_filter = filters.create(_needs_language)


@app.on_message(filters.private & nolang_filter & ~filters.command(["start", "language"]), group=-1)
async def nolang_message_block(client: Client, m: Message):
    """No language picked yet: every command / button / link shows the force-join screen (if not joined)
    or the language picker, and nothing else runs until a language is chosen."""
    if await gate_message(client, m):
        await send_language_picker(client, m.chat.id)
    raise StopPropagation


@app.on_callback_query(nolang_filter & ~filters.regex(r"^(lg\|[a-z]{2,3}|lgx|vsub)$"), group=-1)
async def nolang_callback_block(client: Client, query):
    if await gate_query(client, query):
        try:
            await query.answer("🌐 Pehle language chuno / Choose your language first")
        except Exception:
            pass
        if query.message:
            await send_language_picker(client, query.message.chat.id)
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


def _target_and_reason(m: Message):
    """(user_id, reason) from `/cmd <id|@username> [reason]` or from replying to a user's message."""
    args = m.command[1:]
    if args:
        tok = args[0]
        tid = int(tok) if tok.lstrip("-").isdigit() else _find_user(tok)
        if tid is not None:
            return tid, " ".join(args[1:]).strip()
    r = m.reply_to_message
    if r and r.from_user and not r.from_user.is_bot:
        return r.from_user.id, " ".join(args).strip()
    return None, ""


@app.on_message(filters.command("dashboard") & filters.private & admin_only_early)
async def dashboard_cmd(client: Client, m: Message):
    text, kb = dashboard_content("ov", "7")
    await m.reply(text, reply_markup=kb)


@app.on_message(filters.command("ban") & filters.private & admin_only_early)
async def ban_cmd(client: Client, m: Message):
    tid, reason = _target_and_reason(m)
    if tid is None:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/ban &lt;user_id|@username&gt; [reason]</code>"))
    if is_admin(tid):
        return await m.reply(SC("⚠️ Admin ko ban nahi kar sakte."))
    if tid in BANNED:
        return await m.reply(SC(f"⚠️ <code>{tid}</code> pehle se banned hai."))
    BANNED.append(tid)
    (DB.get("tempbans") or {}).pop(str(tid), None)  # a permanent ban replaces a temp one
    DB.setdefault("ban_info", {})[str(tid)] = {
        "reason": reason[:200], "at": datetime.now().isoformat(), "by": m.from_user.id}
    save_data()
    await m.reply(SC(f"🚫 <code>{tid}</code> ban ho gaya.") + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    log_event(f"🚫 <b>User banned</b>\n\n👤 {_utag(tid)}" + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    try:
        await client.send_message(tid, SC("🚫 Aapko is bot se ban kar diya gaya hai.")
                                  + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    except Exception as e:
        logger.info(f"ban notice to {tid} failed: {e}")


@app.on_message(filters.command("tempban") & filters.private & admin_only_early)
async def tempban_cmd(client: Client, m: Message):
    usage = SC("⚠️ <b>Usage:</b> <code>/tempban &lt;user_id|@username&gt; &lt;30m|12h|7d&gt; [reason]</code>\n"
               "Ya kisi user ke message par reply karke: <code>/tempban 24h [reason]</code>")
    args = m.command[1:]
    r = m.reply_to_message
    tid = secs = None
    if args and parse_duration(args[0]) and r and r.from_user and not r.from_user.is_bot:
        tid, secs, reason = r.from_user.id, parse_duration(args[0]), " ".join(args[1:]).strip()
    elif len(args) >= 2:
        tok = args[0]
        tid = int(tok) if tok.lstrip("-").isdigit() else _find_user(tok)
        secs, reason = parse_duration(args[1]), " ".join(args[2:]).strip()
    if tid is None or not secs:
        return await m.reply(usage)
    if is_admin(tid):
        return await m.reply(SC("⚠️ Admin ko ban nahi kar sakte."))
    tb = DB.setdefault("tempbans", {})
    if tid in BANNED and str(tid) not in tb:
        return await m.reply(SC(f"⚠️ <code>{tid}</code> pehle se permanent banned hai (/unban karke dobara try karo)."))
    until = _now_utc() + timedelta(seconds=secs)
    if tid not in BANNED:
        BANNED.append(tid)
    tb[str(tid)] = until.isoformat()
    DB.setdefault("ban_info", {})[str(tid)] = {
        "reason": reason[:200], "at": datetime.now().isoformat(), "by": m.from_user.id, "until": until.isoformat()}
    save_data()
    left = _left_text(timedelta(seconds=secs))
    await m.reply(SC(f"⏳ <code>{tid}</code> {left} ke liye ban ho gaya.") + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    log_event(f"⏳ <b>User temp-banned</b> ({left})\n\n👤 {_utag(tid)}" + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    try:
        await client.send_message(tid, SC(f"🚫 Aapko {left} ke liye bot se ban kiya gaya hai.")
                                  + (f"\n📝 {esc(reason[:200])}" if reason else ""))
    except Exception as e:
        logger.info(f"temp-ban notice to {tid} failed: {e}")


@app.on_message(filters.command("unban") & filters.private & admin_only_early)
async def unban_cmd(client: Client, m: Message):
    tid, _ = _target_and_reason(m)
    if tid is None:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/unban &lt;user_id|@username&gt;</code>"))
    if tid not in BANNED:
        return await m.reply(SC(f"⚠️ <code>{tid}</code> banned nahi hai."))
    BANNED.remove(tid)
    _BAN_NOTICE_TS.pop(tid, None)
    (DB.get("ban_info") or {}).pop(str(tid), None)
    (DB.get("tempbans") or {}).pop(str(tid), None)
    save_data()
    await m.reply(SC(f"✅ <code>{tid}</code> unban ho gaya."))
    log_event(f"✅ <b>User unbanned</b>\n\n👤 {_utag(tid)}")
    try:
        await client.send_message(tid, SC("✅ Aapko unban kar diya gaya hai — ab bot use kar sakte ho."))
    except Exception as e:
        logger.info(f"unban notice to {tid} failed: {e}")


@app.on_message(filters.command("banned") & filters.private & admin_only_early)
async def banned_list_cmd(client: Client, m: Message):
    if not BANNED:
        return await m.reply(SC("✅ Koi banned user nahi hai."))
    info = DB.get("ban_info") or {}
    lines = "\n".join(
        f"• <code>{i}</code>" + (f" — {esc((info.get(str(i)) or {}).get('reason', '')[:60])}" if (info.get(str(i)) or {}).get("reason") else "")
        for i in BANNED[:100])
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
    /set_channel_id ones) and into LOG_CHANNEL. Failures are logged and ignored — never breaks
    the user's download."""
    targets = set(BACKUP_CHANNEL_IDS) | set(BACKUP_CHANNELS)
    if LOG_CHANNEL and LOG_COPY_VIDEO:
        targets.add(LOG_CHANNEL)
    for channel_id in targets:
        try:
            try:
                await client.copy_message(chat_id=channel_id, from_chat_id=chat_id, message_id=message_id)
            except (PeerIdInvalid, ChannelInvalid):  # in-memory session: warm the channel peer, retry once
                await client.get_chat(channel_id)
                await client.copy_message(chat_id=channel_id, from_chat_id=chat_id, message_id=message_id)
        except FloodWait as fw:
            await asyncio.sleep(min(fw.value, 30))
        except Exception as e:
            logger.warning(f"Backup to {channel_id} failed: {e} — bot ko channel mein admin banao (Post Messages)")


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
        "• <code>/cookies</code> — cookies.txt upload (har naya account pool mein judta hai)\n"
        "• <code>/delcookies</code> — account / sab cookies delete karo\n"
        "• <code>/authcheck</code> — har account ka live YouTube check\n"
        "• <code>/clearcooldown</code> — 429 cooldown + resting accounts hatao\n"
        "• <code>/ytdlpupdate</code> — yt-dlp upgrade"
    )


@app.on_message(filters.command("cookies") & filters.private & admin_only)
async def cookies_cmd(client: Client, m: Message):
    COOKIE_WINDOW[m.chat.id] = time.time() + COOKIE_WINDOW_SECONDS
    await m.reply(
        auth_panel_text() + "\n\n<b>3 steps</b>\n"
        "1️⃣ Chrome/Firefox mein <code>youtube.com</code> par login karo\n"
        "2️⃣ <b>Get cookies.txt LOCALLY</b> se Netscape format export karo\n"
        "3️⃣ Wo <code>.txt</code> ab yahan <b>File/Document</b> ke roop mein bhejo (15 min window)\n\n"
        f"🔄 <b>Multiple accounts:</b> alag Google account se dobara export karke bhejo — wo pool mein judega "
        f"(max {yt_auth.COOKIE_MAX_ACCOUNTS}). Wahi account dobara bhejoge to uski cookies refresh ho jayengi."
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
    pool_dir = yt_auth._pool_dir()
    pool_dir.mkdir(parents=True, exist_ok=True)
    tmp = pool_dir / f".{m.chat.id}.{m.id}.upload"
    downloaded = tmp
    try:
        async with _cookie_lock:
            got = await asyncio.wait_for(client.download_media(m, file_name=str(tmp)), timeout=120)
            if not got:
                raise RuntimeError("Telegram ne file path nahi diya")
            downloaded = Path(got).resolve()
            # validated BEFORE os.replace -> a bad upload can never erase a working cookie file
            info, acc_name, action = yt_auth.add_account(downloaded)
    except asyncio.TimeoutError:
        return await safe_edit(progress, "❌ Cookie download timeout ho gaya. Dobara bhejo.")
    except ValueError as e:
        return await safe_edit(progress, f"⚠️ <b>Cookies change nahi hui</b>\n{esc(str(e))}")
    except Exception as e:
        logger.exception("cookie upload failed")
        return await safe_edit(progress, f"❌ <b>Cookies save nahi hui</b>\n<code>{esc(str(e)[:180])}</code>")
    finally:
        for pth in {tmp, downloaded}:
            try:
                pth.unlink(missing_ok=True)
            except OSError:
                pass

    COOKIE_WINDOW.pop(m.chat.id, None)
    total = len(yt_auth.list_accounts())
    login = (f"✅ Login markers: <code>{info.auth_cookie_count}</code>" if info.has_login_cookies
             else "⚠️ Login marker nahi mila — agar downloads fail hon to login ke saath dobara export karo")
    head = (f"🔄 <b>Account refreshed</b> · <code>{esc(acc_name)}</code>" if action == "refreshed"
            else f"✅ <b>Account added</b> · <code>{esc(acc_name)}</code>")
    text = (f"{head}\n\n🍪 YouTube rows: <code>{info.youtube_cookie_count}</code>\n{login}\n"
            f"📦 Size: <code>{info.size_bytes:,}</code> bytes\n"
            f"👥 Pool: <code>{total}</code> account{'s' if total != 1 else ''}"
            + ("" if total > 1 else " — rotation ke liye ek aur account upload karo")
            + "\n\n<i>Agli request se hi use hongi, restart nahi chahiye.</i>")
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


@app.on_message(filters.command("delcookies") & filters.private & admin_only)
async def delcookies_cmd(client: Client, m: Message):
    accts = yt_auth.list_accounts()
    if not accts:
        return await m.reply("ℹ️ Koi cookies set nahi hain — delete karne ko kuch nahi.")
    rows, row = [], []
    for a in accts:
        row.append(make_button(f"🗑 {a.name}", f"dc|{a.name}", style=BTN_DANGER))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if len(accts) > 1:
        rows.append([make_button("🗑 Sab delete", "dc|all", style=BTN_DANGER)])
    rows.append([make_button("Cancel", "dc|no", style=BTN_PRIMARY)])
    await m.reply(
        "🗑 <b>Kaunsa cookie account delete karna hai?</b>\n\n"
        f"{yt_auth.auth_status_html()}\n\n"
        "Sab delete karne par bot cookie-free mode mein chalega (age-restricted / bot-check wale videos fail ho sakte hain).",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@app.on_callback_query(filters.regex(r"^dc\|"))
async def delcookies_cb(client: Client, query):
    if not is_admin(query.from_user.id):
        return await query.answer("Sirf admin.", show_alert=True)
    what = query.data.split("|", 1)[1]
    if what == "no":
        await safe_edit(query.message, "❎ Cancel kiya — cookies waise hi hain.")
    elif what == "all":  # one extra confirm before wiping the whole pool
        await safe_edit(
            query.message, "⚠️ <b>Poora cookie pool delete karna hai?</b>",
            InlineKeyboardMarkup([[make_button("🗑 Haan, sab delete", "dc|allyes", style=BTN_DANGER),
                                   make_button("Cancel", "dc|no", style=BTN_PRIMARY)]]))
    else:
        if what == "allyes":
            async with _cookie_lock:
                removed = yt_auth.delete_cookies()
        elif what in {a.name for a in yt_auth.list_accounts()}:
            async with _cookie_lock:
                removed = yt_auth.delete_cookies(what)
        else:
            removed = []
        left = len(yt_auth.list_accounts())
        if removed:
            text = (f"✅ <b>Delete ho gaya:</b> <code>{esc(', '.join(removed))}</code>\n\n"
                    + (f"👥 Pool mein ab <code>{left}</code> account{'s' if left != 1 else ''} bache hain."
                       if left else "Agli request se bot cookie-free mode mein chalega. Naye file ke liye <code>/cookies</code> use karo."))
            if any(c.strip() for _, c, _ in yt_auth._env_seed_sources()):
                text += ("\n\nℹ️ <code>COOKIES_CONTENT*</code> env set hai — delete kiye hue account restart par wapas load "
                         "nahi honge. Permanent hatane ke liye env variable bhi hata do.")
        else:
            text = "ℹ️ Wo cookie account mila hi nahi (pehle hi delete ho chuka hoga)."
        await safe_edit(query.message, text)
    await query.answer()


@app.on_message(filters.command("authstatus") & filters.private & admin_only)
async def authstatus_cmd(client: Client, m: Message):
    await m.reply(auth_panel_text())


def _probe_account_blocking(acc):
    """One live check pinned to a single cookie account (acc=None -> cookie-free). Blocking."""
    tok = _CUR_ACC.set(acc)
    try:
        return yt_auth.probe_youtube_access(base_opts)
    finally:
        _CUR_ACC.reset(tok)


@app.on_message(filters.command("authcheck") & filters.private & admin_only)
async def authcheck_cmd(client: Client, m: Message):
    accts = yt_auth.list_accounts()
    wait = await m.reply("🌐 <b>Live check</b> · YouTube se contact kar raha hoon…")
    if len(accts) <= 1:
        ok, detail = await _live_check()
        if ok:
            yt_auth.clear_cooldown()
            return await safe_edit(wait, f"✅ <b>Live check pass</b>\n{esc(detail)}")
        return await safe_edit(
            wait,
            f"❌ <b>Live check fail</b>\n{esc(detail)}\n\nFresh <code>/cookies</code> upload karo. HTTP 429 ho to "
            "30–60 min ruko — baar-baar try karne se block lamba ho jata hai.",
        )
    lines, any_ok = [], False
    for i, a in enumerate(accts, 1):
        await safe_edit(wait, f"🌐 <b>Live check</b> · account {i}/{len(accts)} (<code>{esc(a.name)}</code>)…\n\n"
                        + "\n".join(lines))
        try:
            ok, detail = await asyncio.wait_for(
                loop.run_in_executor(None, _probe_account_blocking, a), timeout=45)
        except asyncio.TimeoutError:
            ok, detail = False, "45 second mein jawab nahi aaya."
        if ok:
            any_ok = True
            yt_auth.reset_account(a.name)  # works again -> stop resting it
        lines.append(f"{'✅' if ok else '❌'} <code>{esc(a.name)}</code> — {esc(detail)}")
    if any_ok:
        yt_auth.clear_cooldown()
    good = sum(1 for ln in lines if ln.startswith("✅"))
    tail = ("" if good == len(accts) else
            "\n\n❌ wale accounts ke liye fresh <code>/cookies</code> upload karo (ya <code>/delcookies</code> se hatao). "
            "HTTP 429 ho to 30–60 min ruko.")
    await safe_edit(wait, f"🔎 <b>Live check · {good}/{len(accts)} accounts OK</b>\n\n" + "\n".join(lines) + tail)


@app.on_message(filters.command("clearcooldown") & filters.private & admin_only)
async def clearcooldown_cmd(client: Client, m: Message):
    left = yt_auth.cooldown_remaining()
    yt_auth.clear_cooldown()
    woke = yt_auth.reset_all_accounts()
    parts = []
    if left:
        parts.append(f"✅ Cooldown hata diya (tha: {yt_auth.human_time_short(left)}).")
    if woke:
        parts.append(f"🍪 {woke} resting cookie account{'s' if woke != 1 else ''} wapas active.")
    await m.reply(" ".join(parts) or "ℹ️ Koi cooldown active nahi tha.")


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
    BotCommand("language", "🌐 Change language"),
    BotCommand("cancel", "Cancel active download"),
    BotCommand("stats", "Your statistics"),
    BotCommand("plans", "💎 View premium plans"),
    BotCommand("myplan", "💎 Your plan & today's usage"),
    BotCommand("referral", "🎁 Invite friends, earn premium"),
]


IST = timezone(timedelta(hours=5, minutes=30))
USER_LIMIT = int(os.getenv("USER_LIMIT", "0") or 0)  # sirf display ke liye ("8 / 200"); 0 = sirf count dikhao


def ist_now() -> str:
    return datetime.now(IST).strftime("%I:%M %p IST")


def _dev_tag() -> str:
    handle = (POWERED_BY_URL or "").rstrip("/").split("/")[-1].lstrip("@")
    if handle:
        return f'<a href="{POWERED_BY_URL}">@{smallcaps(handle)}</a>'
    return smallcaps(POWERED_BY)


def start_log_text(bot_username: str) -> str:
    users = len(DB["users"])
    users_txt = f"{users} / {USER_LIMIT}" if USER_LIMIT else str(users)
    return ("<blockquote>" + f"🚀 {smallcaps('Bot successfully started!')}\n\n"
            f"⭐️ {smallcaps('Bot:')} @{bot_username}\n"
            f"👥 {smallcaps('Users:')} {users_txt}\n"
            f"⌛ {smallcaps('Time:')} {ist_now()}\n\n"
            f"👑 {smallcaps('Developed by')} {_dev_tag()}" + "</blockquote>")


def new_user_log_text(u) -> str:
    return ("<blockquote>" + f"{smallcaps(os.getenv('BOT_TITLE', 'YouTube Downloader Bot'))} ❤️‍🩹:\n"
            f"👥 #{smallcaps('NewUser')}\n"
            f"⭐️ {smallcaps('User:')} <a href=\"tg://user?id={u.id}\">{esc(full_name(u))}</a>\n"
            f"ℹ️ {smallcaps('ID:')} <code>{u.id}</code>\n"
            f"⌛ {smallcaps('Time:')} {ist_now()}" + "</blockquote>")


OFFLINE_TEXT = (
    "<blockquote>"
    "⛔️ 𝘽𝙊𝙏 𝙄𝙎 𝙂𝙊𝙄𝙉𝙂 𝙊𝙁𝙁𝙇𝙄𝙉𝙀\n\n"
    "🚫 𝙏𝙝𝙞𝙨 𝙗𝙤𝙩 𝙞𝙨 𝙩𝙚𝙢𝙥𝙤𝙧𝙖𝙧𝙞𝙡𝙮 𝙤𝙛𝙛𝙡𝙞𝙣𝙚.\n\n"
    "🛠️ 𝙒𝙚'𝙧𝙚 𝙥𝙚𝙧𝙛𝙤𝙧𝙢𝙞𝙣𝙜 𝙨𝙤𝙢𝙚 𝙢𝙖𝙞𝙣𝙩𝙚𝙣𝙖𝙣𝙘𝙚 & 𝙪𝙥𝙙𝙖𝙩𝙚𝙨.\n\n"
    "⏳ 𝙋𝙡𝙚𝙖𝙨𝙚 𝙬𝙖𝙞𝙩 𝙪𝙣𝙩𝙞𝙡 𝙩𝙝𝙚 𝙗𝙤𝙩 𝙞𝙨 𝙗𝙖𝙘𝙠 𝙤𝙣𝙡𝙞𝙣𝙚.\n\n"
    "🔔 𝙎𝙩𝙖𝙮 𝙩𝙪𝙣𝙚𝙙 𝙛𝙤𝙧 𝙛𝙪𝙧𝙩𝙝𝙚𝙧 𝙪𝙥𝙙𝙖𝙩𝙚𝙨.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "⚡ 𝙒𝙚'𝙡𝙡 𝙗𝙚 𝙗𝙖𝙘𝙠 𝙨𝙤𝙤𝙣!"
    "</blockquote>"
)


async def announce(text: str) -> bool:
    """Post a start/stop notice to LOG_CHANNEL. If that isn't set or the send fails, DM the admins
    the exact reason instead — so it's never silently missing again."""
    err = ""
    if not LOG_CHANNEL:
        if LOG_INVITE_HASH:
            err = ("Private invite link se channel ID nahi mil saki. Bot ko us channel mein admin banao "
                   "(apne aap detect hoga) ya channel mein /setlog post karo.")
        else:
            err = "LOG_CHANNEL set nahi hai (env variable `LOG_CHANNEL` mein -100… ID, @username ya t.me link daalo)."
    else:
        try:
            try:
                await app.get_chat(LOG_CHANNEL)  # warms the peer cache (session is in-memory)
            except Exception as e:
                logger.info(f"get_chat({LOG_CHANNEL}) failed: {e}")
            log_text = text if text.lstrip().startswith("<blockquote") else f"<blockquote>{text}</blockquote>"
            await app.send_message(LOG_CHANNEL, log_text, link_preview_options=LinkPreviewOptions(is_disabled=True))
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
    BotCommand("dashboard", "📊 Dashboard: downloads, top users, failures"),
    BotCommand("ban", "🚫 Ban a user (/ban <id>)"),
    BotCommand("tempban", "⏳ Temp-ban (/tempban <id> 24h reason)"),
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
    BotCommand("delcookies", "🗑 Delete YouTube cookies"),
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
    _accts = yt_auth.list_accounts()
    logger.info(f"YouTube cookies: {len(_accts)} account(s) {[a.name for a in _accts]}" if _accts
                else "YouTube cookies: none (cookie-free mode)")
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
    await resolve_log_channel(app)
    if AUTO_DELETE_SECONDS > 0:
        asyncio.ensure_future(auto_delete_worker(app), loop=loop)
        logger.info(f"Auto-delete ON: sent files removed after {AUTO_DELETE_SECONDS}s")
    if _parse_hhmm(DAILY_REPORT_TIME):
        asyncio.ensure_future(daily_report_worker(app), loop=loop)
        logger.info(f"Daily report ON at {DAILY_REPORT_TIME} IST")
    if SPIKE_RATE > 0:
        asyncio.ensure_future(spike_worker(app), loop=loop)
        logger.info(f"Failure-spike alert ON (>= {SPIKE_RATE:.0%} of >= {SPIKE_MIN_EVENTS} downloads in {SPIKE_WINDOW_MIN} min)")
    asyncio.ensure_future(tempban_worker(app), loop=loop)
    if PREMIUM_REMINDER_HOURS > 0:
        asyncio.ensure_future(premium_reminder_worker(app), loop=loop)
        logger.info(f"Premium renewal reminder ON ({PREMIUM_REMINDER_HOURS}h before expiry)")
    await announce(start_log_text(me.username))

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
        logger.info(f"Bot stopping — reason: {reason[0]}, uptime: {up_txt}")
        try:  # a stopping host gives only a few seconds — don't hang on it
            await asyncio.wait_for(
                announce(OFFLINE_TEXT),
                timeout=8)
        except Exception:
            pass
        save_data(wait=True)
        await app.stop()


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
