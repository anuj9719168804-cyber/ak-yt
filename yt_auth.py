"""
YouTube auth layer for the bot (ported from YTtoTg core/auth.py + core/downloader.py).

What it gives bot.py:
  * cookies.txt validation + atomic install (a bad upload never replaces a working file)
  * active_cookie_path()  — re-checked on every call, so new cookies work without a restart
  * COOKIES_CONTENT env   — seed the cookie file on hosts with no file upload (Koyeb/Render/Railway)
  * error classifiers     — tell a real HTTP 429 / bot-check apart from a normal "video unavailable"
  * global 429 cooldown   — a 429 is per server IP, not per video: stop hammering YouTube for a while
  * probe_youtube_access  — one explicit live check (/authcheck)

Pure stdlib, no import of bot.py (no circular imports). Everything configurable via env.
"""
from __future__ import annotations

import html
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("ytbot")

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent
# YT_COOKIES (old setting) still works: it is simply the cookie file path.
YT_COOKIES = os.getenv("YT_COOKIES", "").strip()
# Optional: whole Netscape cookies.txt text in an env var (used only if no valid file exists yet).
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
# How long to pause YouTube requests after an HTTP 429 (minutes).
YOUTUBE_COOLDOWN_MINUTES = max(1, int(os.getenv("YOUTUBE_COOLDOWN_MINUTES", "30") or 30))
MAX_COOKIE_FILE_BYTES = 5 * 1024 * 1024

_AUTH_COOKIE_NAMES = {
    "APISID", "HSID", "LOGIN_INFO", "SAPISID", "SID", "SSID",
    "__SECURE-1PAPISID", "__SECURE-1PSID", "__SECURE-3PAPISID", "__SECURE-3PSID",
}


def _data_dir() -> Path:
    """Folder of DATA_FILE — that is the persistent volume on most hosts."""
    data_file = os.getenv("DATA_FILE", "bot_data.json")
    parent = Path(data_file).expanduser().parent
    if not parent.is_absolute():
        parent = _BASE_DIR / parent
    return parent.resolve()


# ---------------------------------------------------------------------
# Cookies: validate / install / locate
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class CookieFileInfo:
    """Non-sensitive metadata about a Netscape cookie file (values are never exposed)."""
    path: Path
    cookie_count: int
    youtube_cookie_count: int
    auth_cookie_count: int
    expired_cookie_count: int
    domain_count: int
    size_bytes: int

    @property
    def has_login_cookies(self) -> bool:
        return self.auth_cookie_count > 0


def configured_cookie_path() -> Path:
    """Where /cookies installs an uploaded file (returned even before it exists)."""
    if YT_COOKIES:
        p = Path(YT_COOKIES).expanduser()
        if not p.is_absolute():
            p = _BASE_DIR / p
        p = p.resolve()
        if p.is_dir() or YT_COOKIES.endswith(("/", "\\")):
            return p / "cookies.txt"
        return p
    return _data_dir() / "cookies.txt"


def inspect_cookies_file(path: Path) -> tuple[Optional[CookieFileInfo], str]:
    """Parse a Netscape cookies file. Returns (info, "") or (None, error message)."""
    try:
        size = path.stat().st_size
        if size <= 0:
            return None, "Cookie file khali hai. YouTube se dobara export karo."
        if size > MAX_COOKIE_FILE_BYTES:
            return None, f"Cookie file bahut badi hai ({size:,} bytes). Max {MAX_COOKIE_FILE_BYTES // (1024 * 1024)} MB."
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return None, "File UTF-8 text nahi hai. cookies.txt ke roop mein dobara export karo."
    except OSError as exc:
        return None, f"File read nahi ho payi: {exc}"

    if "\x00" in text:
        return None, "File mein binary data hai, browser cookies nahi."

    lines = text.splitlines()
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if not first.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        return None, ("Ye Netscape-format cookies.txt nahi hai. YouTube khula rakhke "
                      "'Get cookies.txt LOCALLY' extension se export karo.")

    now = int(time.time())
    total = yt = auth = expired = 0
    domains: set[str] = set()
    for n, raw in enumerate(lines, start=1):
        line = raw.strip("\r")
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        row = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
        cols = row.split("\t", 6)
        if len(cols) != 7:
            return None, f"Line {n} par cookie row galat format mein hai."
        domain, incl_sub, cpath, secure, expires, name, _value = cols
        if not domain or not cpath or incl_sub.upper() not in {"TRUE", "FALSE"} \
                or secure.upper() not in {"TRUE", "FALSE"}:
            return None, f"Line {n} par cookie fields galat hain."
        try:
            exp = int(expires)
        except ValueError:
            return None, f"Line {n} par expiry timestamp galat hai."
        d = domain.lstrip(".").casefold()
        total += 1
        domains.add(d)
        if d == "youtube.com" or d.endswith(".youtube.com"):
            yt += 1
            if name.upper() in _AUTH_COOKIE_NAMES:
                auth += 1
        if 0 < exp <= now:
            expired += 1

    if total == 0:
        return None, "File mein koi cookie row nahi hai."
    if yt == 0:
        return None, "Koi youtube.com cookie nahi mili. YouTube mein login rakhke current site ki cookies export karo."
    return CookieFileInfo(path, total, yt, auth, expired, len(domains), size), ""


def install_cookies_file(source: Path, destination: Optional[Path] = None) -> CookieFileInfo:
    """Validate *source*, then atomically move it into place (chmod 600).

    Raises ValueError on bad content and leaves the current cookie file untouched.
    Keep *source* in the destination folder so os.replace stays atomic.
    """
    info, error = inspect_cookies_file(source)
    if info is None:
        raise ValueError(error)
    target = (destination or configured_cookie_path()).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.chmod(0o600)
    except OSError:
        pass
    os.replace(source, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return CookieFileInfo(target, info.cookie_count, info.youtube_cookie_count, info.auth_cookie_count,
                          info.expired_cookie_count, info.domain_count, info.size_bytes)


def active_cookie_path() -> Optional[Path]:
    """First existing *valid* cookie file, checked on every call (no restart needed)."""
    candidates = [configured_cookie_path()]
    default = (_data_dir() / "cookies.txt").resolve()
    if default not in candidates:
        candidates.append(default)
    for p in candidates:
        if p.is_file() and inspect_cookies_file(p)[0] is not None:
            return p
    return None


def bootstrap_cookies_from_env() -> Optional[str]:
    """Seed the cookie file from COOKIES_CONTENT — only when no valid file is active yet."""
    if not COOKIES_CONTENT.strip() or active_cookie_path() is not None:
        return None
    target = configured_cookie_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.envseed.tmp")
    try:
        tmp.write_text(COOKIES_CONTENT, encoding="utf-8")
        info = install_cookies_file(tmp, target)
        logger.info(f"Cookies seeded from COOKIES_CONTENT -> {info.path}")
        return str(info.path)
    except (ValueError, OSError) as exc:
        logger.error(f"COOKIES_CONTENT rejected: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def auth_status_html() -> str:
    """Zero-network, structural status for the admin panel (HTML parse mode)."""
    active = active_cookie_path()
    if active:
        info, _ = inspect_cookies_file(active)
        login = "login cookies mili" if info.has_login_cookies else "login marker nahi mila"
        lines = [
            "✅ <b>YouTube cookies active</b>",
            f"├ <code>{info.youtube_cookie_count}</code> YouTube rows · {login}",
            f"├ <code>{info.size_bytes:,}</code> bytes · <code>{info.expired_cookie_count}</code> expired rows",
            f"└ <code>{html.escape(str(active))}</code>",
        ]
        if not info.has_login_cookies:
            lines.append("⚠️ Login ke saath dobara export karo — ye file sirf guest cookies ki ho sakti hai.")
        return "\n".join(lines)
    target = configured_cookie_path()
    if target.is_file():
        _, err = inspect_cookies_file(target)
        return ("❌ <b>Cookie file mili par invalid hai</b>\n" + html.escape(err)
                + f"\n📁 <code>{html.escape(str(target))}</code>")
    return ("⚠️ <b>Koi YouTube cookies set nahi hain</b>\n"
            f"📁 Upload target: <code>{html.escape(str(target))}</code>")


# ---------------------------------------------------------------------
# Error classification — a 429 or bot-check is NOT the same as "video unavailable"
# ---------------------------------------------------------------------
_RATE_LIMIT_MARKERS = ("HTTP Error 429", "Too Many Requests", "status code 429")
_HARD_BLOCK_MARKERS = (
    "Sign in to confirm you", "not a robot", "Sign in to prove",
    "confirm you're not a bot", "Only images are available",
)
_BOT_MARKERS = _HARD_BLOCK_MARKERS + (
    "n challenge solving failed", "challenge solving failed", "have a supported JavaScript runtime",
)


def _has(err: str, markers) -> bool:
    low = (err or "").casefold()
    return any(m.casefold() in low for m in markers)


def is_rate_limit_error(err: str) -> bool:
    """True only for an explicit YouTube HTTP 429."""
    return _has(err, _RATE_LIMIT_MARKERS)


def is_hard_youtube_block(err: str) -> bool:
    """429 / sign-in wall: trying other player clients will not help, only make it worse."""
    return is_rate_limit_error(err) or _has(err, _HARD_BLOCK_MARKERS)


def is_bot_detection_error(err: str) -> bool:
    return is_rate_limit_error(err) or _has(err, _BOT_MARKERS)


class DiagLogger:
    """yt-dlp logger that keeps the last few warnings/errors.

    yt-dlp often logs the real cause (HTTP 429, "only images are available") as a
    WARNING and then raises only a generic "format not available". Keeping the
    warnings lets us classify the real failure.
    """

    def __init__(self):
        self._items: list[str] = []
        self._lock = threading.Lock()

    def _keep(self, msg):
        clean = " ".join(str(msg).split())[:600]
        if clean:
            with self._lock:
                self._items.append(clean)
                del self._items[:-12]

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self._keep(msg)

    def error(self, msg):
        self._keep(msg)

    def context(self) -> str:
        with self._lock:
            return " | ".join(self._items)


# ---------------------------------------------------------------------
# Global YouTube cooldown (a 429 hits the whole server IP, not one video)
# ---------------------------------------------------------------------
_cd_lock = threading.Lock()
_cd_until = 0.0


def activate_cooldown(seconds: Optional[float] = None) -> bool:
    """Start/extend the cooldown. Returns True only for a NEW incident (alert admins once)."""
    global _cd_until
    now = time.time()
    with _cd_lock:
        is_new = _cd_until <= now
        _cd_until = max(_cd_until, now + (seconds or YOUTUBE_COOLDOWN_MINUTES * 60))
        return is_new


def cooldown_remaining() -> int:
    with _cd_lock:
        return max(0, int(_cd_until - time.time()))


def clear_cooldown() -> None:
    global _cd_until
    with _cd_lock:
        _cd_until = 0.0


def human_time_short(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s" if m < 5 else f"{m}m"
    return f"{s}s"


# ---------------------------------------------------------------------
# Live check (explicit — /authcheck only; one request, no download)
# ---------------------------------------------------------------------
def probe_youtube_access(make_opts: Callable[[], dict]) -> tuple[bool, str]:
    """One real metadata extraction to verify server -> YouTube works. Blocking.

    make_opts: returns yt-dlp options (bot.py passes its base_opts so cookies/PO-token are used).
    """
    import yt_dlp

    diag = DiagLogger()
    try:
        opts = {**make_opts(), "skip_download": True, "noplaylist": True, "socket_timeout": 15,
                "retries": 0, "fragment_retries": 0, "extractor_retries": 0, "logger": diag}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        playable = [f for f in info.get("formats") or []
                    if f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none")]
        if not playable:
            return False, "YouTube ne jawab diya, par koi playable format nahi mila."
        top = max((f.get("height") or 0 for f in playable), default=0)
        return True, f"{len(playable)} playable formats mile (max {top}p)."
    except Exception as exc:
        err = f"{exc} | {diag.context()}"
        if is_rate_limit_error(err):
            return False, "Is server ka IP YouTube ne rate-limit kar diya hai (HTTP 429)."
        if is_bot_detection_error(err):
            return False, "YouTube ka bot/challenge protection abhi bhi is server ko block kar raha hai."
        return False, f"Live check fail: {str(exc)[:240]}"
