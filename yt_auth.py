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


# ---------------------------------------------------------------------
# Cookie account pool — several YouTube accounts, rotated.
#   * every valid cookies.txt is one account: the classic `cookies.txt` is "main",
#     uploads via /cookies land in <data dir>/cookies/acc_N.txt
#   * acquire() hands out the least-recently-used account that is not resting
#   * an account that hits a bot-check / 429 rests for a while (only when another
#     account is available to take over) and the request is retried on the next one
# ---------------------------------------------------------------------
COOKIE_MAX_ACCOUNTS = max(1, int(os.getenv("COOKIE_MAX_ACCOUNTS", "10") or 10))
COOKIE_429_COOLDOWN_MIN = max(1, int(os.getenv("COOKIE_429_COOLDOWN_MINUTES", "") or YOUTUBE_COOLDOWN_MINUTES))
COOKIE_BOTCHECK_COOLDOWN_MIN = max(1, int(os.getenv("COOKIE_BOTCHECK_COOLDOWN_MINUTES", "60") or 60))

_FP_NAMES = ("__SECURE-3PSID", "__SECURE-1PSID", "SID", "SAPISID")


@dataclass(frozen=True)
class Account:
    name: str   # "main", "acc_1", "env_2" ...
    path: Path


_pool_lock = threading.RLock()
_acc_state: dict[str, dict] = {}   # name -> cool_until / reason / fails / uses / last_used
_suppressed: set[str] = set()      # env-seeded accounts the admin deleted (do not re-seed on restart)
_loaded = False
_valid_cache: dict[str, tuple] = {}


def _pool_dir() -> Path:
    return _data_dir() / "cookies"


def _state_path() -> Path:
    return _data_dir() / "cookie_pool.json"


def _default_cookie_path() -> Path:
    return (_data_dir() / "cookies.txt").resolve()


def _is_valid(p: Path) -> bool:
    """inspect_cookies_file() with a (mtime, size) cache — base_opts runs this on every request."""
    try:
        st = p.stat()
    except OSError:
        return False
    sig = (st.st_mtime_ns, st.st_size)
    hit = _valid_cache.get(str(p))
    if hit and hit[0] == sig:
        return hit[1]
    ok = inspect_cookies_file(p)[0] is not None
    _valid_cache[str(p)] = (sig, ok)
    return ok


def list_accounts() -> list[Account]:
    out: list[Account] = []
    candidates = [configured_cookie_path()]
    default = _default_cookie_path()
    if default not in candidates:
        candidates.append(default)
    for p in candidates:
        if p.is_file() and _is_valid(p):
            out.append(Account("main", p))
            break
    pd = _pool_dir()
    if pd.is_dir():
        for p in sorted(pd.glob("*.txt")):
            if p.is_file() and _is_valid(p):
                out.append(Account(p.stem, p))
    return out


def _ensure_loaded() -> None:
    global _loaded
    with _pool_lock:
        if _loaded:
            return
        _loaded = True
        try:
            import json
            data = json.loads(_state_path().read_text(encoding="utf-8"))
            for name, st in (data.get("accounts") or {}).items():
                _acc_state[name] = {"cool_until": float(st.get("cool_until") or 0), "reason": st.get("reason") or "",
                                    "fails": int(st.get("fails") or 0), "uses": 0, "last_used": 0.0}
            _suppressed.update(data.get("suppressed") or [])
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning(f"cookie pool state unreadable ({exc}) - starting fresh")


def _save() -> None:
    import json
    with _pool_lock:
        data = {"accounts": {n: {"cool_until": st["cool_until"], "reason": st["reason"], "fails": st["fails"]}
                             for n, st in _acc_state.items() if st["cool_until"] > time.time() or st["fails"]},
                "suppressed": sorted(_suppressed)}
        try:
            tmp = _state_path().with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, _state_path())
        except OSError as exc:
            logger.debug(f"cookie pool state not saved: {exc}")


def _st(name: str) -> dict:
    return _acc_state.setdefault(name, {"cool_until": 0.0, "reason": "", "fails": 0, "uses": 0, "last_used": 0.0})


def acquire(exclude=()) -> Optional[Account]:
    """Least-recently-used account that is not resting. None = no cookies (none set, or all resting)."""
    accts = list_accounts()
    now = time.time()
    with _pool_lock:
        _ensure_loaded()
        ready = [a for a in accts if a.name not in exclude and _st(a.name)["cool_until"] <= now]
        if not ready:
            return None
        acc = min(ready, key=lambda a: _st(a.name)["last_used"])
        st = _st(acc.name)
        st["last_used"], st["uses"] = now, st["uses"] + 1
        return acc


def is_cooling(name: str) -> bool:
    with _pool_lock:
        _ensure_loaded()
        return _st(name)["cool_until"] > time.time()


def mark_account(name: str, reason: str, minutes: float) -> tuple[bool, bool]:
    """An account just hit a 429 / bot-check. Returns (rotated, newly_resting).

    rotated=True  -> the account now rests and at least one other account can take over.
    rotated=False -> nobody to rotate to (single account, or the others rest too): the account keeps
                     being used and the caller falls back to the classic behaviour."""
    accts = list_accounts()
    now = time.time()
    with _pool_lock:
        _ensure_loaded()
        st = _st(name)
        st["fails"] += 1
        known = any(a.name == name for a in accts)
        others = [a for a in accts if a.name != name and _st(a.name)["cool_until"] <= now]
        if not known or not others:
            _save()
            return False, False
        newly = st["cool_until"] <= now
        st["cool_until"] = max(st["cool_until"], now + minutes * 60)
        st["reason"] = reason
        _save()
        return True, newly


def reset_account(name: str) -> None:
    with _pool_lock:
        _ensure_loaded()
        st = _st(name)
        st["cool_until"], st["reason"], st["fails"] = 0.0, "", 0
        _save()


def reset_all_accounts() -> int:
    """Wake every resting account. Returns how many were resting."""
    now = time.time()
    with _pool_lock:
        _ensure_loaded()
        n = 0
        for st in _acc_state.values():
            if st["cool_until"] > now:
                n += 1
            st["cool_until"], st["reason"], st["fails"] = 0.0, "", 0
        _save()
        return n


def account_name_for_path(path) -> str:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return Path(str(path)).stem
    if p in (configured_cookie_path(), _default_cookie_path()):
        return "main"
    return p.stem


def account_from_error(err: str) -> Optional[str]:
    """bot.py tags yt-dlp errors with [ck:<account>] so a failure can be blamed on the right account."""
    import re
    m = re.search(r"\[ck:([A-Za-z0-9_\-]+)\]", err or "")
    return m.group(1) if m else None


def active_cookie_path() -> Optional[Path]:
    """Path of the first usable cookie account (compat helper), or None."""
    accts = list_accounts()
    return accts[0].path if accts else None


def _fingerprint(path: Path) -> Optional[str]:
    """Stable id of the Google account behind a cookies file (hash of its session cookie; never shown/stored)."""
    import hashlib
    vals: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#") or not line.strip():
                continue
            f = line.split("\t")
            if len(f) >= 7 and f[5].upper() in _FP_NAMES and f[6].strip():
                vals.setdefault(f[5].upper(), f[6].strip())
    except OSError:
        return None
    for n in _FP_NAMES:
        if n in vals:
            return hashlib.sha256(vals[n].encode()).hexdigest()[:16]
    return None


def add_account(source: Path) -> tuple[CookieFileInfo, str, str]:
    """Validate *source* and add it to the pool. Same Google account already in the pool -> refresh that
    file instead of adding a duplicate. Returns (info, account_name, "added" | "refreshed").
    Raises ValueError on bad content / pool full; existing files are never touched on failure.
    Keep *source* inside the pool dir so os.replace stays atomic."""
    info, error = inspect_cookies_file(source)
    if info is None:
        raise ValueError(error)
    accts = list_accounts()
    fp = _fingerprint(source)
    target, name, action = None, "", "added"
    if fp:
        for a in accts:
            if _fingerprint(a.path) == fp:
                target, name, action = a.path, a.name, "refreshed"
                break
    if target is None:
        if len(accts) >= COOKIE_MAX_ACCOUNTS:
            raise ValueError(f"Pool full ({COOKIE_MAX_ACCOUNTS} accounts). Pehle /delcookies se koi purana hatao.")
        pd = _pool_dir()
        pd.mkdir(parents=True, exist_ok=True)
        taken = {p.stem for p in pd.glob("*.txt")}
        n = 1
        while f"acc_{n}" in taken:
            n += 1
        name = f"acc_{n}"
        target = pd / f"{name}.txt"
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
    with _pool_lock:
        _ensure_loaded()
        _suppressed.discard(name)
        st = _st(name)
        st["cool_until"], st["reason"], st["fails"] = 0.0, "", 0
        _save()
    return (CookieFileInfo(target, info.cookie_count, info.youtube_cookie_count, info.auth_cookie_count,
                           info.expired_cookie_count, info.domain_count, info.size_bytes), name, action)


def delete_cookies(name: Optional[str] = None) -> list[str]:
    """Delete one account (by name) or all of them. Returns the names that were removed."""
    removed: list[str] = []
    for a in list_accounts():
        if name not in (None, a.name):
            continue
        paths = {a.path}
        if a.name == "main":
            paths |= {configured_cookie_path(), _default_cookie_path()}
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                logger.error(f"could not delete cookie file {p}: {exc}")
        removed.append(a.name)
    with _pool_lock:
        _ensure_loaded()
        for n in removed:
            _suppressed.add(n)   # env-seeded accounts must not come back on the next restart
            _acc_state.pop(n, None)
        _save()
    return removed


def _env_seed_sources() -> list[tuple[str, str, Path]]:
    """(account name, netscape text, target file) for COOKIES_CONTENT and COOKIES_CONTENT_2 .. _N."""
    out = [("main", COOKIES_CONTENT, configured_cookie_path())]
    for n in range(2, COOKIE_MAX_ACCOUNTS + 1):
        out.append((f"env_{n}", os.getenv(f"COOKIES_CONTENT_{n}", ""), _pool_dir() / f"env_{n}.txt"))
    return out


def bootstrap_cookies_from_env() -> Optional[str]:
    """Seed accounts from COOKIES_CONTENT / COOKIES_CONTENT_2.. — only for accounts that do not exist yet
    and were not deleted by the admin. Returns a short summary or None."""
    with _pool_lock:
        _ensure_loaded()
    have = {a.name for a in list_accounts()}
    seeded = []
    for name, content, target in _env_seed_sources():
        if not content.strip() or name in have or name in _suppressed:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.envseed.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            info = install_cookies_file(tmp, target)
            logger.info(f"Cookies seeded from env -> {name} ({info.path})")
            seeded.append(name)
        except (ValueError, OSError) as exc:
            logger.error(f"cookie env seed for {name} rejected: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return ", ".join(seeded) or None


def pool_status() -> list[dict]:
    now = time.time()
    out = []
    accts = list_accounts()
    with _pool_lock:
        _ensure_loaded()
        for a in accts:
            st = _st(a.name)
            info, _ = inspect_cookies_file(a.path)
            out.append({"name": a.name, "path": a.path, "info": info,
                        "left": max(0, int(st["cool_until"] - now)), "reason": st["reason"],
                        "fails": st["fails"], "uses": st["uses"]})
    return out


def auth_status_html() -> str:
    """Zero-network, structural status for the admin panel (HTML parse mode)."""
    rows = pool_status()
    if rows:
        ready = sum(1 for r in rows if not r["left"])
        lines = [f"✅ <b>YouTube cookies: {len(rows)} account{'s' if len(rows) != 1 else ''}</b> · {ready} ready"]
        for i, r in enumerate(rows):
            branch = "└" if i == len(rows) - 1 else "├"
            info = r["info"]
            login = "login ✓" if info and info.has_login_cookies else "⚠️ no login marker"
            rows_n = info.youtube_cookie_count if info else 0
            if r["left"]:
                state = f"🧊 {html.escape(r['reason'] or 'resting')} · {human_time_short(r['left'])} left"
            else:
                state = "✅ ready"
            lines.append(f"{branch} <code>{html.escape(r['name'])}</code> · {state} · {rows_n} rows · {login}"
                         + (f" · fails {r['fails']}" if r["fails"] else ""))
        if len(rows) == 1:
            lines.append("💡 Rotation ke liye 2+ accounts upload karo (<code>/cookies</code>).")
        return "\n".join(lines)
    target = configured_cookie_path()
    if target.is_file():
        _, err = inspect_cookies_file(target)
        return ("❌ <b>Cookie file mili par invalid hai</b>\n" + html.escape(err or "")
                + f"\n📁 <code>{html.escape(str(target))}</code>")
    return ("⚠️ <b>Koi YouTube cookies set nahi hain</b>\n"
            f"📁 Upload target: <code>{html.escape(str(_pool_dir()))}</code>")


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
